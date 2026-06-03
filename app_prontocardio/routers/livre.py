from http import HTTPStatus

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.orm import Session

from app_prontocardio.database import get_session_oracle
from app_prontocardio.schema import VersaoOracle

router = APIRouter(prefix='/livre', tags=['livre'])


@router.get('/', status_code=HTTPStatus.OK)
def read_root():
    return {'message': 'Bem-vindo à API Hospital Prontocardio'}


@router.get(
    '/consultar_versao_oracle/',
    status_code=HTTPStatus.OK,
    response_model=list[VersaoOracle],
)
def versao_oracle(session: Session = Depends(get_session_oracle)):

    rows = session.execute(text('SELECT * FROM v$version')).mappings().all()

    versoes = [VersaoOracle(**row) for row in rows]

    return versoes
