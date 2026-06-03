from http import HTTPStatus
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy import select
from sqlalchemy.orm import Session

from app_prontocardio.database import get_session_postgres
from app_prontocardio.models import Usuario
from app_prontocardio.schema import Token
from app_prontocardio.security import (
    criar_token,
    valida_senha_cru_x_senha_hash_db,
)

router = APIRouter(prefix='/autenticacao', tags=['autenticacao'])

SessionPostgres = Annotated[Session, Depends(get_session_postgres)]
OAuthForm = Annotated[OAuth2PasswordRequestForm, Depends()]


@router.post('/token', status_code=HTTPStatus.OK, response_model=Token)
def autenticar_usuario_para_acesso(
    form_data: OAuthForm, session: SessionPostgres
):
    """Gera token para usuário logado"""
    usuario_banco = session.scalar(
        select(Usuario).where(Usuario.email == form_data.username)
    )

    excessao_autenticacao = HTTPException(
        status_code=HTTPStatus.UNAUTHORIZED, detail='Email ou Senha incorretos'
    )

    if not usuario_banco:
        raise excessao_autenticacao

    if not valida_senha_cru_x_senha_hash_db(
        form_data.password, usuario_banco.senha
    ):
        raise excessao_autenticacao

    token = criar_token(claim={'sub': usuario_banco.email})

    return {'access_token': token, 'token_type': 'Bearer'}
