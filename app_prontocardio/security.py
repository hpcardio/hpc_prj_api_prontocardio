from http import HTTPStatus
from typing import Annotated

from fastapi import Depends, HTTPException
from fastapi.security import OAuth2PasswordBearer
from jwt import DecodeError, InvalidTokenError, decode, encode
from pwdlib import PasswordHash
from sqlalchemy import select
from sqlalchemy.orm import Session

from app_prontocardio.database import get_session_postgres, postgres_engine
from app_prontocardio.models import Usuario
from app_prontocardio.settings import Settings

settings = Settings()
pwd_context = PasswordHash.recommended()
oauth2_scheme = OAuth2PasswordBearer(tokenUrl='/autenticacao/token')

SessionPostgres = Annotated[Session, Depends(get_session_postgres)]


def gera_hash_senha(senha_cru: str):
    return pwd_context.hash(senha_cru)


def valida_senha_cru_x_senha_hash_db(senha_cru: str, senha_hash_db: str):
    return pwd_context.verify(senha_cru, senha_hash_db)


def usuario_token_somente_leitura(token: str) -> bool:
    try:
        payload = decode(
            token, settings.SECRET_KEY, algorithms=settings.ALGORITHM
        )
    except InvalidTokenError:
        return False

    subject_email = payload.get('sub')
    if not subject_email or postgres_engine is None:
        return False

    with Session(postgres_engine) as session:
        usuario = session.scalar(
            select(Usuario).where(Usuario.email == subject_email)
        )

    return bool(
        usuario
        and usuario.ativo
        and usuario.perfil.casefold() == 'leitura'
    )


def criar_token(claim: dict):

    encoded_jwt = encode(claim, settings.SECRET_KEY, settings.ALGORITHM)

    return encoded_jwt


def valida_token_usuario_atual(
    session: SessionPostgres, token: str = Depends(oauth2_scheme)
):
    excessao_autenticacao = HTTPException(
        status_code=HTTPStatus.UNAUTHORIZED,
        detail='Could not validate credentials',
        headers={'WWW-Authenticate': 'Bearer'},
    )

    try:
        payload = decode(
            token, settings.SECRET_KEY, algorithms=settings.ALGORITHM
        )
        subject_email = payload.get('sub')
        if not subject_email:
            raise excessao_autenticacao
    except DecodeError:
        raise excessao_autenticacao

    usuario_banco = session.scalar(
        select(Usuario).where(Usuario.email == subject_email)
    )

    if not usuario_banco or not usuario_banco.ativo:
        raise excessao_autenticacao

    return usuario_banco


def valida_usuario_ti(usuario: Usuario = Depends(valida_token_usuario_atual)):
    if usuario.perfil != 'ti':
        raise HTTPException(
            status_code=HTTPStatus.FORBIDDEN,
            detail='Acesso restrito à equipe de TI.',
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
