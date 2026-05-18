from http import HTTPStatus

from fastapi import Depends, FastAPI
from sqlalchemy import text
from sqlalchemy.orm import Session

from app_prontocardio.database import get_session_prod
from app_prontocardio.schema import VersaoOracle

app = FastAPI(title='API Hospital Prontocardio')


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
