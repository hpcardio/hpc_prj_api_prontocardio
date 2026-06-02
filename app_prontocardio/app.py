from http import HTTPStatus
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Path
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app_prontocardio.database import (
    ensure_postgres_schema,
    get_session_oracle,
    get_session_postgres,
)
from app_prontocardio.models import ModelContaAtendimento, Usuario
from app_prontocardio.schema import (
    Atendimento,
    Atendimentos,
    Message,
    UserPublic,
    UserSchema,
    VersaoOracle,
)
from app_prontocardio.security import gera_hash_senha

app = FastAPI(title='API Hospital Prontocardio 💙')

SessionPostgres = Annotated[Session, Depends(get_session_postgres)]


def get_usuario_or_404(user_id: int, session: SessionPostgres) -> Usuario:
    usuario = session.get(Usuario, user_id)

    if usuario is None:
        raise HTTPException(
            status_code=HTTPStatus.NOT_FOUND,
            detail='Usuário não encontrado.',
        )

    return usuario


UsuarioAtual = Annotated[Usuario, Depends(get_usuario_or_404)]


@app.on_event('startup')
def startup() -> None:
    ensure_postgres_schema()


@app.get('/', status_code=HTTPStatus.OK)
def read_root():
    return {'message': 'Olá Mundo! API Hospital Prontocardio'}


@app.get(
    '/versao_oracle/',
    status_code=HTTPStatus.OK,
    response_model=list[VersaoOracle],
)
def versao_oracle(session: Session = Depends(get_session_oracle)):

    rows = session.execute(text('SELECT * FROM v$version')).mappings().all()

    versoes = [VersaoOracle(**row) for row in rows]

    return versoes


@app.post(
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
        detail='Já temos um usuário cadastrado nome/email!',
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


@app.put(
    '/usuarios/{user_id}',
    status_code=HTTPStatus.OK,
    response_model=UserPublic
)
def alterar_usuario(
    user_id: int,
    usuario_input: UserSchema,
    usuario_atual: UsuarioAtual,
    session: Session = Depends(get_session_postgres),
):

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
            detail='Nome ou e-mail já cadastrado.',
        )


@app.delete(
    '/users/{user_id}',
    status_code=HTTPStatus.OK,
    response_model=Message
)
def deletar_usuario(
    user_id: int,
    usuario_atual: UsuarioAtual,
    session: Session = Depends(get_session_postgres),
):
    session.delete(usuario_atual)
    session.commit()

    return {'message': 'Usuário Excluído!'}


@app.get(
    '/conta_atendimento/{cd_atendimento}',
    status_code=HTTPStatus.OK,
    response_model=Atendimentos,
)
def conta_atendimento(
    cd_atendimento: int = Path(..., gt=0),
    session: Session = Depends(get_session_oracle),
):
    try:
        result = select(ModelContaAtendimento).where(
            ModelContaAtendimento.cd_atendimento == cd_atendimento
        )
        rows = session.execute(result).scalars().all()

    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=HTTPStatus.INTERNAL_SERVER_ERROR,
            detail='Erro ao consultar conta_atendimento.',
        ) from exc

    if not rows:
        raise HTTPException(
            status_code=HTTPStatus.NOT_FOUND,
            detail=(
                f'cd_atendimento: {cd_atendimento} não encontrado. '
                'Forneça um código de atendimento válido'
            ),
        )

    atendimentos_list = [
        Atendimento.model_validate(
            row,
            from_attributes=True,
        )
        for row in rows
    ]
    return {'atendimentos': atendimentos_list}
