import secrets
import smtplib
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from hashlib import sha256
from http import HTTPStatus
from typing import Annotated
from urllib.parse import quote, urlencode

from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy import column, func, or_, select, table, update
from sqlalchemy.orm import Session

from app_prontocardio.database import get_session_postgres
from app_prontocardio.models import TokenRedefinicaoSenha, Usuario
from app_prontocardio.schema import (
    Message,
    FirstAccessPasswordChange,
    PasswordResetConfirm,
    PasswordResetRequest,
    Token,
)
from app_prontocardio.security import (
    criar_token,
    criar_token_usuario,
    gera_hash_senha,
    valida_senha_cru_x_senha_hash_db,
)
from app_prontocardio.settings import Settings

router = APIRouter(prefix='/autenticacao', tags=['autenticacao'])

SessionPostgres = Annotated[Session, Depends(get_session_postgres)]
OAuthForm = Annotated[OAuth2PasswordRequestForm, Depends()]
settings = Settings()


TAMANHO_CPF = 11


def _cpf_do_login(login: str) -> str | None:
    """CPF digitado com ou sem pontuação (000.000.000-00)."""
    if not login or any(ch not in '0123456789.- ' for ch in login):
        return None
    digitos = ''.join(ch for ch in login if ch.isdigit())
    return digitos if len(digitos) == TAMANHO_CPF else None


def _usuario_por_login(session: Session, login: str) -> Usuario | None:
    """Login por email, nome de usuário (ex.: login MV RH000938) ou CPF."""
    login_normalizado = login.strip().lower()
    condicoes = [
        func.lower(Usuario.email) == login_normalizado,
        func.lower(Usuario.nome_usuario) == login_normalizado,
    ]
    cpf = _cpf_do_login(login.strip())
    if cpf:
        condicoes.append(Usuario.cpf == cpf)
    usuario = session.scalar(select(Usuario).where(or_(*condicoes)))
    if usuario is None and '@' not in login_normalizado and not cpf:
        usuario = _usuario_pelo_login_mv(session, login_normalizado)
    return usuario


def _usuario_pelo_login_mv(session: Session, login: str) -> Usuario | None:
    """Usuário antigo (nome de usuário próprio) com login MV vinculado."""
    vinculos = table(
        'usuarios_mv',
        column('usuario_id'),
        column('cd_usuario_mv'),
        schema=Usuario.__table__.schema,
    )
    return session.scalar(
        select(Usuario).where(
            Usuario.id.in_(
                select(vinculos.c.usuario_id).where(
                    func.lower(vinculos.c.cd_usuario_mv) == login
                )
            )
        )
    )


@router.post('/token', status_code=HTTPStatus.OK, response_model=Token)
def autenticar_usuario_para_acesso(
    form_data: OAuthForm, session: SessionPostgres
):
    """Gera token para usuário logado"""

    usuario_banco = _usuario_por_login(session, form_data.username)

    excessao_autenticacao = HTTPException(
        status_code=HTTPStatus.UNAUTHORIZED, detail='Email ou Senha incorretos'
    )

    if not usuario_banco or not usuario_banco.ativo:
        raise excessao_autenticacao

    if not valida_senha_cru_x_senha_hash_db(
        form_data.password, usuario_banco.senha
    ):
        raise excessao_autenticacao

    if usuario_banco.troca_senha_obrigatoria:
        raise HTTPException(
            status_code=HTTPStatus.FORBIDDEN,
            detail={
                'codigo': 'TROCA_SENHA_OBRIGATORIA',
                'mensagem': 'Defina uma nova senha para continuar.',
            },
        )

    token = criar_token_usuario(usuario_banco)

    return {'access_token': token, 'token_type': 'Bearer'}


@router.post('/primeiro-acesso', status_code=HTTPStatus.OK, response_model=Message)
def concluir_primeiro_acesso(
    payload: FirstAccessPasswordChange, session: SessionPostgres
):
    usuario = _usuario_por_login(session, payload.usuario)
    if (
        not usuario
        or not usuario.ativo
        or not usuario.troca_senha_obrigatoria
        or not valida_senha_cru_x_senha_hash_db(
            payload.senha_temporaria, usuario.senha
        )
    ):
        raise HTTPException(
            HTTPStatus.UNAUTHORIZED, 'Usuário ou senha temporária incorretos.'
        )
    if payload.nova_senha == payload.senha_temporaria:
        raise HTTPException(
            HTTPStatus.BAD_REQUEST,
            'A nova senha deve ser diferente da senha temporária.',
        )
    usuario.senha = gera_hash_senha(payload.nova_senha)
    usuario.troca_senha_obrigatoria = False
    session.commit()
    return {'message': 'Senha definida com sucesso. Entre com a nova senha.'}


@router.post(
    '/token-painel-medicos',
    status_code=HTTPStatus.OK,
    response_model=Token,
)
def autenticar_usuario_painel_medicos(
    form_data: OAuthForm, session: SessionPostgres
):
    """Gera uma sessão de oito horas restrita ao painel médico externo."""
    usuario_banco = session.scalar(
        select(Usuario).where(Usuario.email == form_data.username)
    )
    excessao_autenticacao = HTTPException(
        status_code=HTTPStatus.UNAUTHORIZED,
        detail='Email ou senha incorretos',
    )
    if (
        not usuario_banco
        or not usuario_banco.ativo
        or usuario_banco.perfil not in {'leitura', 'ti'}
        or not valida_senha_cru_x_senha_hash_db(
            form_data.password, usuario_banco.senha
        )
    ):
        raise excessao_autenticacao

    token = criar_token(
        claim={
            'sub': usuario_banco.email,
            'aud': 'painel_leitos_medicos',
            'exp': datetime.now(timezone.utc) + timedelta(hours=8),
        }
    )
    return {'access_token': token, 'token_type': 'Bearer'}


def _montar_link_redefinicao(token: str) -> str:
    reset_url = settings.frontend_password_reset_url
    if '{token}' in reset_url:
        return reset_url.replace('{token}', quote(token, safe=''))

    separator = '&' if '?' in reset_url else '?'
    return f'{reset_url}{separator}{urlencode({"token": token})}'


def _enviar_email_redefinicao(destinatario: str, token: str) -> None:
    if not settings.SMTP_HOST:
        return
    reset_url = _montar_link_redefinicao(token)
    mensagem = EmailMessage()
    mensagem['Subject'] = 'Redefinição de senha · Gestão de Glosas'
    mensagem['From'] = settings.smtp_from_email
    mensagem['To'] = destinatario
    mensagem.set_content(
        'Recebemos uma solicitação para redefinir sua senha.\n\n'
        f'Acesse o link abaixo em até 30 minutos:\n{reset_url}\n\n'
        'Se você não fez esta solicitação, ignore esta mensagem.'
    )
    smtp_class = smtplib.SMTP_SSL if settings.smtp_use_ssl else smtplib.SMTP
    with smtp_class(
        settings.SMTP_HOST, settings.SMTP_PORT, timeout=10
    ) as smtp:
        if settings.smtp_use_tls:
            smtp.starttls()
        if settings.smtp_username and settings.SMTP_PASSWORD:
            smtp.login(settings.smtp_username, settings.SMTP_PASSWORD)
        smtp.send_message(mensagem)


@router.post(
    '/esqueci-senha', status_code=HTTPStatus.OK, response_model=Message
)
def solicitar_redefinicao_senha(
    payload: PasswordResetRequest, session: SessionPostgres
):
    usuario = session.scalar(
        select(Usuario).where(Usuario.email == str(payload.email).lower())
    )
    if usuario and usuario.ativo:
        session.execute(
            update(TokenRedefinicaoSenha)
            .where(TokenRedefinicaoSenha.usuario_id == usuario.id)
            .values(utilizado=True)
        )
        token = secrets.token_urlsafe(48)
        session.add(
            TokenRedefinicaoSenha(
                usuario_id=usuario.id,
                token_hash=sha256(token.encode()).hexdigest(),
                expira_em=datetime.now(timezone.utc) + timedelta(minutes=30),
            )
        )
        session.commit()
        try:
            _enviar_email_redefinicao(usuario.email, token)
        except (OSError, smtplib.SMTPException):
            pass
    return {
        'message': 'Se o e-mail estiver cadastrado, enviaremos as instruções.'
    }


@router.post(
    '/redefinir-senha', status_code=HTTPStatus.OK, response_model=Message
)
def confirmar_redefinicao_senha(
    payload: PasswordResetConfirm, session: SessionPostgres
):
    token_hash = sha256(payload.token.encode()).hexdigest()
    registro = session.scalar(
        select(TokenRedefinicaoSenha).where(
            TokenRedefinicaoSenha.token_hash == token_hash,
            TokenRedefinicaoSenha.utilizado.is_(False),
        )
    )
    agora = datetime.now(timezone.utc)
    if registro and registro.expira_em.tzinfo is None:
        registro.expira_em = registro.expira_em.replace(tzinfo=timezone.utc)
    if not registro or registro.expira_em < agora:
        raise HTTPException(
            HTTPStatus.BAD_REQUEST, 'Link inválido ou expirado.'
        )
    usuario = session.get(Usuario, registro.usuario_id)
    if not usuario or not usuario.ativo:
        raise HTTPException(
            HTTPStatus.BAD_REQUEST, 'Link inválido ou expirado.'
        )
    usuario.senha = gera_hash_senha(payload.nova_senha)
    # A senha foi escolhida pelo próprio usuário (link do e-mail): deixa de
    # ser temporária.
    usuario.troca_senha_obrigatoria = False
    session.execute(
        update(TokenRedefinicaoSenha)
        .where(TokenRedefinicaoSenha.usuario_id == usuario.id)
        .values(utilizado=True)
    )
    session.commit()
    return {'message': 'Senha redefinida com sucesso.'}
