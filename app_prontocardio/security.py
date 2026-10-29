import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from typing import Annotated

from fastapi import Depends, HTTPException
from fastapi.security import OAuth2PasswordBearer
from jwt import InvalidTokenError, decode, encode
from pwdlib import PasswordHash
from sqlalchemy import select
from sqlalchemy.orm import Session

from app_prontocardio.database import get_session_postgres, postgres_engine
from app_prontocardio.models import Usuario
from app_prontocardio.settings import Settings

settings = Settings()
pwd_context = PasswordHash.recommended()
oauth2_scheme = OAuth2PasswordBearer(tokenUrl='/autenticacao/token')
painel_medicos_oauth2_scheme = OAuth2PasswordBearer(
    tokenUrl='/autenticacao/token-painel-medicos'
)

SessionPostgres = Annotated[Session, Depends(get_session_postgres)]


def gera_hash_senha(senha_cru: str):
    return pwd_context.hash(senha_cru)


def valida_senha_cru_x_senha_hash_db(senha_cru: str, senha_hash_db: str):
    return pwd_context.verify(senha_cru, senha_hash_db)


def criar_token(claim: dict):
    # Also used by the separate, audience-scoped clinical panel contract.
    return encode(claim, settings.SECRET_KEY, settings.ALGORITHM)


def _versao_credencial(usuario: Usuario) -> str:
    message = json.dumps(
        [usuario.id, usuario.email, usuario.senha], separators=(',', ':')
    )
    return hmac.new(
        settings.SECRET_KEY.encode(), message.encode(), hashlib.sha256
    ).hexdigest()


def criar_token_usuario(usuario: Usuario) -> str:
    agora = datetime.now(timezone.utc)
    return criar_token({
        'sub': usuario.email,
        'iat': agora,
        'exp': agora + timedelta(hours=8),
        'sv': _versao_credencial(usuario),
    })


def valida_token_usuario_atual(
    session: SessionPostgres, token: str = Depends(oauth2_scheme)
):
    erro = HTTPException(
        status_code=HTTPStatus.UNAUTHORIZED,
        detail='Could not validate credentials',
        headers={'WWW-Authenticate': 'Bearer'},
    )
    try:
        payload = decode(
            token,
            settings.SECRET_KEY,
            algorithms=[settings.ALGORITHM],
            options={'require': ['sub', 'iat', 'exp', 'sv']},
        )
        if (
            not isinstance(payload['sub'], str)
            or not payload['sub']
            or not isinstance(payload['sv'], str)
            or not isinstance(payload['iat'], (int, float))
            or not isinstance(payload['exp'], (int, float))
            or not 0 < payload['exp'] - payload['iat'] <= 8 * 3600
        ):
            raise erro
    except InvalidTokenError:
        raise erro
    usuario = session.scalar(
        select(Usuario).where(Usuario.email == payload['sub'])
    )
    if (
        not usuario
        or not usuario.ativo
        or not hmac.compare_digest(
            payload['sv'].encode(), _versao_credencial(usuario).encode()
        )
    ):
        raise erro
    return usuario


def usuario_token_somente_leitura(token: str) -> bool:
    if postgres_engine is None:
        return False
    try:
        with Session(postgres_engine) as session:
            usuario = valida_token_usuario_atual(session, token)
            return usuario.perfil.casefold() == 'leitura'
    except HTTPException:
        return False


def valida_usuario_ti(usuario: Usuario = Depends(valida_token_usuario_atual)):
    if usuario.perfil != 'ti':
        raise HTTPException(
            status_code=HTTPStatus.FORBIDDEN,
            detail='Acesso restrito à equipe de TI.',
        )
    return usuario


def valida_acesso_painel_leitos_medicos(
    session: SessionPostgres,
    token: str = Depends(painel_medicos_oauth2_scheme),
) -> Usuario:
    """Valida token de curta duração exclusivo do painel clínico externo."""
    excessao_autenticacao = HTTPException(
        status_code=HTTPStatus.UNAUTHORIZED,
        detail='Sessão inválida ou expirada.',
        headers={'WWW-Authenticate': 'Bearer'},
    )
    try:
        payload = decode(
            token,
            settings.SECRET_KEY,
            algorithms=settings.ALGORITHM,
            audience='painel_leitos_medicos',
        )
        subject_email = payload.get('sub')
        if not subject_email:
            raise excessao_autenticacao
    except InvalidTokenError:
        raise excessao_autenticacao

    usuario = session.scalar(
        select(Usuario).where(Usuario.email == subject_email)
    )
    if not usuario or not usuario.ativo:
        raise excessao_autenticacao
    if usuario.perfil not in {'leitura', 'ti'}:
        raise HTTPException(
            status_code=HTTPStatus.FORBIDDEN,
            detail=(
                'Acesso restrito aos usuários autorizados do painel médico.'
            ),
        )
    return usuario


def valida_acesso_ecg_worklist(
    usuario: Usuario = Depends(valida_token_usuario_atual),
) -> Usuario:
    if 'ecg_worklist' not in usuario.telas_permitidas:
        raise HTTPException(
            status_code=HTTPStatus.FORBIDDEN,
            detail='Permissão insuficiente.',
        )
    return usuario


def valida_acesso_financeiro_mv(
    usuario: Usuario = Depends(valida_token_usuario_atual),
) -> Usuario:
    autorizado_por_perfil = usuario.perfil in {'ti', 'administrador'}
    autorizado_por_tela = 'financeiro_mv' in usuario.telas_permitidas
    if not (autorizado_por_perfil or autorizado_por_tela):
        raise HTTPException(
            status_code=HTTPStatus.FORBIDDEN,
            detail='Permissão insuficiente para o Financeiro MV.',
        )
    return usuario
