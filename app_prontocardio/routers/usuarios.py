from http import HTTPStatus
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app_prontocardio.database import (
    ensure_postgres_schema,
    get_session_postgres,
)
from app_prontocardio.models import Usuario
from app_prontocardio.schema import (
    FilterPage,
    Message,
    UserList,
    UserPublic,
    UserSchema,
)
from app_prontocardio.security import (
    gera_hash_senha,
    valida_token_usuario_atual,
)

router = APIRouter(prefix='/usuarios', tags=['usuarios'])

SessionPostgres = Annotated[Session, Depends(get_session_postgres)]
Filter_Users = Annotated[FilterPage, Query()]
ValidaUsuarioAtual = Annotated[Usuario, Depends(valida_token_usuario_atual)]


@router.on_event('startup')
def startup() -> None:
    ensure_postgres_schema()


# def get_usuario_or_404(
#         user_id: int,
#         session: SessionPostgres
# ) -> Usuario:

#     usuario = session.get(Usuario, user_id)

#     if usuario is None:
#         raise HTTPException(
#             status_code=HTTPStatus.NOT_FOUND,
#             detail='Usuário não encontrado.',
#         )

#     return usuario


@router.get('/', status_code=HTTPStatus.OK, response_model=UserList)
def consultar_usuario(
    filter_usuario: Filter_Users,
    usuario_atual: ValidaUsuarioAtual,
    session: SessionPostgres,
):
    usuarios_banco = session.scalars(
        select(Usuario)
        .limit(filter_usuario.limit)
        .offset(filter_usuario.offset)
    ).all()

    return {'usuarios': usuarios_banco}


@router.post(
    '/usuarios/', status_code=HTTPStatus.CREATED, response_model=UserPublic
)
def criar_usuario(
    usuario_input: UserSchema,
    session: Session = Depends(get_session_postgres),
):
    """Verifica se usuário está cadastrado na API;
    Valida a senha-cru VS senha-hash"""

    usuario_banco = session.scalar(
        select(Usuario).where(
            (Usuario.nome == usuario_input.nome)
            | (Usuario.email == usuario_input.email)
        )
    )

    excessao_conflito_usuario = HTTPException(
        status_code=HTTPStatus.CONFLICT,
        detail='Já temos um usuário cadastrado com o mesmo nome/email!',
    )

    if usuario_banco:
        if usuario_banco.nome == usuario_input.nome:
            raise excessao_conflito_usuario
        elif usuario_banco.email == usuario_input.email:
            raise excessao_conflito_usuario

    senha_hasheada = gera_hash_senha(usuario_input.senha)

    usuario_banco = Usuario(
        **usuario_input.model_dump(exclude={'senha'}), senha=senha_hasheada
    )

    session.add(usuario_banco)
    session.commit()
    session.refresh(usuario_banco)

    return usuario_banco


@router.put(
    '/usuarios/{user_id}', status_code=HTTPStatus.OK, response_model=UserPublic
)
def alterar_usuario(
    user_id: int,
    usuario_input: UserSchema,
    usuario_atual: ValidaUsuarioAtual,
    session: Session = Depends(get_session_postgres),
):

    if usuario_atual.id != user_id:
        raise HTTPException(
            status_code=HTTPStatus.FORBIDDEN, detail='Usuário sem permissão!!'
        )

    try:
        usuario_atual.nome = usuario_input.nome
        usuario_atual.email = usuario_input.email
        usuario_atual.senha = gera_hash_senha(usuario_input.senha)

        session.commit()
        session.refresh(usuario_atual)

        return usuario_atual

    except IntegrityError:
        raise HTTPException(
            status_code=HTTPStatus.CONFLICT,
            detail='Nome ou e-mail já cadastrado',
        )


@router.delete(
    '/usuarios/{user_id}', status_code=HTTPStatus.OK, response_model=Message
)
def deletar_usuario(
    user_id: int,
    usuario_atual: ValidaUsuarioAtual,
    session: Session = Depends(get_session_postgres),
):

    if usuario_atual.id != user_id:
        raise HTTPException(
            status_code=HTTPStatus.FORBIDDEN, detail='Usuário sem permissão!!'
        )

    session.delete(usuario_atual)
    session.commit()

    return {'message': 'Usuário Excluído!'}
