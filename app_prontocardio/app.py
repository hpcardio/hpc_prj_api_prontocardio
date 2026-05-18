from http import HTTPStatus

from fastapi import Depends, FastAPI, HTTPException, Path
from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app_prontocardio.database import get_session_prod
from app_prontocardio.models import ModelContaAtendimento
from app_prontocardio.schema import Atendimento, Atendimentos, VersaoOracle

app = FastAPI(title='API Hospital Prontocardio 💙')


@app.get('/', status_code=HTTPStatus.OK)
def read_root():
    return {'message': 'Olá Mundo! API Hospital Prontocardio'}


@app.get(
    '/versao_oracle/',
    status_code=HTTPStatus.OK,
    response_model=list[VersaoOracle],
)
def versao_oracle(session: Session = Depends(get_session_prod)):

    rows = session.execute(
        text('SELECT * FROM v$version')
    ).mappings().all()

    versoes = [VersaoOracle(**row) for row in rows]

    return versoes


@app.get(
    '/conta_atendimento/{cd_atendimento}',
    status_code=HTTPStatus.OK,
    response_model=Atendimentos,
)
def conta_atendimento(
    cd_atendimento: int = Path(..., gt=0),
    session: Session = Depends(get_session_prod),
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
