# ruff: noqa: E501
"""Login MV pelo CAS da MV e vínculo com o usuário do Receita Certa."""

from http import HTTPStatus
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from app_prontocardio.database import get_session_oracle, get_session_postgres
from app_prontocardio.models import Usuario
from app_prontocardio.security import valida_token_usuario_atual
from app_prontocardio.services import mv_identidade as mv
from app_prontocardio.settings import Settings

router = APIRouter(prefix='/mv', tags=['mv-identidade'])
SessionPostgres = Annotated[Session, Depends(get_session_postgres)]
SessionOracle = Annotated[Session, Depends(get_session_oracle)]
UsuarioAtual = Annotated[Usuario, Depends(valida_token_usuario_atual)]


def configuracao() -> Settings:
    return Settings()


Config = Annotated[Settings, Depends(configuracao)]

SQL_VINCULO = text("""
SELECT usuario_id, cd_usuario_mv, nm_usuario_mv, origem, validado_cas_em,
       proxy_concedido_em, vinculado_em
  FROM api_prontocardio.usuarios_mv
 WHERE usuario_id = :usuario_id
""")

SQL_OUTRO_DONO = text("""
SELECT usuario_id FROM api_prontocardio.usuarios_mv
 WHERE cd_usuario_mv = :login AND usuario_id <> :usuario_id
""")

SQL_GRAVAR_VINCULO_CAS = text("""
INSERT INTO api_prontocardio.usuarios_mv
    (usuario_id, cd_usuario_mv, nm_usuario_mv, vinculado_por, origem, validado_cas_em, proxy_concedido_em)
VALUES (:usuario_id, :login, :nome, :usuario_id, 'cas',
        timezone('America/Sao_Paulo', now()),
        CASE WHEN :proxy_concedido THEN timezone('America/Sao_Paulo', now()) END)
ON CONFLICT (usuario_id) DO UPDATE SET
    cd_usuario_mv = EXCLUDED.cd_usuario_mv,
    nm_usuario_mv = EXCLUDED.nm_usuario_mv,
    vinculado_por = EXCLUDED.vinculado_por,
    origem = 'cas',
    validado_cas_em = EXCLUDED.validado_cas_em,
    -- Mantém a concessão anterior só se for o mesmo login MV.
    proxy_concedido_em = COALESCE(EXCLUDED.proxy_concedido_em,
        CASE WHEN api_prontocardio.usuarios_mv.cd_usuario_mv = EXCLUDED.cd_usuario_mv
             THEN api_prontocardio.usuarios_mv.proxy_concedido_em END),
    vinculado_em = timezone('America/Sao_Paulo', now())
""")


class TicketCasInput(BaseModel):
    ticket: str = Field(min_length=4, max_length=256)
    service: str = Field(min_length=10, max_length=500)


def _vinculo(session: Session, usuario_id: int) -> dict:
    linha = session.execute(SQL_VINCULO, {'usuario_id': usuario_id}).mappings().first()
    if linha is None:
        return {'usuario_id': usuario_id, 'cd_usuario_mv': None, 'origem': None, 'pode_gravar_no_mv': False}
    vinculo = dict(linha)
    vinculo['pode_gravar_no_mv'] = vinculo['origem'] == 'cas' and vinculo['proxy_concedido_em'] is not None
    return vinculo


@router.get('/vinculo')
def meu_vinculo(usuario: UsuarioAtual, session: SessionPostgres):
    return _vinculo(session, usuario.id)


@router.post('/cas/validar')
def validar_login_cas(
    dados: TicketCasInput,
    usuario: UsuarioAtual,
    session: SessionPostgres,
    oracle: SessionOracle,
    config: Config,
):
    """Valida o ticket emitido pelo CAS da MV e vincula o login MV ao usuário."""
    if not config.MV_CAS_SERVICE_URL or not dados.service.startswith(config.MV_CAS_SERVICE_URL):
        raise HTTPException(HTTPStatus.BAD_REQUEST, 'URL de retorno do CAS não autorizada.')
    try:
        login = mv.validar_ticket_cas(
            config.MV_CAS_BASE_URL, dados.ticket, dados.service, config.MV_CAS_TIMEOUT_SECONDS
        )
    except (mv.CasInvalido, mv.LoginMvInvalido) as exc:
        raise HTTPException(HTTPStatus.UNAUTHORIZED, str(exc)) from exc

    usuario_mv = mv.usuario_mv_ativo(oracle, login)
    if usuario_mv is None:
        raise HTTPException(HTTPStatus.FORBIDDEN, f'O usuário MV {login} não está ativo.')
    outro = session.execute(SQL_OUTRO_DONO, {'login': login, 'usuario_id': usuario.id}).scalar_one_or_none()
    if outro is not None:
        raise HTTPException(
            HTTPStatus.CONFLICT,
            f'O login MV {login} já está vinculado a outro usuário do Receita Certa. Peça ao financeiro para desfazer o vínculo.',
        )

    proxy_concedido = False
    if config.MV_PROXY_PASSWORD:
        # DDL no Oracle faz commit implícito; a concessão é idempotente.
        mv.conceder_proxy(oracle, login, config.MV_PROXY_USER)
        proxy_concedido = True
    session.execute(SQL_GRAVAR_VINCULO_CAS, {
        'usuario_id': usuario.id,
        'login': login,
        'nome': usuario_mv['nm_usuario'],
        'proxy_concedido': proxy_concedido,
    })
    session.commit()
    return _vinculo(session, usuario.id)


@router.post('/sessao/teste')
def testar_sessao_mv(usuario: UsuarioAtual, session: SessionPostgres, config: Config):
    """Abre a sessão Oracle como o usuário MV vinculado e confirma a identidade (somente leitura)."""
    vinculo = _vinculo(session, usuario.id)
    if not vinculo['pode_gravar_no_mv']:
        raise HTTPException(HTTPStatus.PRECONDITION_FAILED, 'Entre com o seu login MV antes.')
    with mv.sessao_mv(
        vinculo['cd_usuario_mv'],
        proxy_user=config.MV_PROXY_USER,
        proxy_password=config.MV_PROXY_PASSWORD,
        dsn=mv.dsn_do_oracle(config.ORACLE_DATABASE_URL),
        usuario_id=usuario.id,
    ) as conexao:
        cursor = conexao.cursor()
        cursor.execute("SELECT USER, SYS_CONTEXT('USERENV', 'PROXY_USER') FROM dual")
        usuario_oracle, proxy = cursor.fetchone()
    return {'usuario_oracle': usuario_oracle, 'proxy': proxy, 'login_mv': vinculo['cd_usuario_mv'],
            'confere': usuario_oracle == vinculo['cd_usuario_mv']}
