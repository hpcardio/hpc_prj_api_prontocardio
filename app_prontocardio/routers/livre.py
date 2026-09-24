from datetime import date, datetime
from http import HTTPStatus

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app_prontocardio.agendamento_schema import PacienteResumo
from app_prontocardio.database import get_session_oracle
from app_prontocardio.schema import VersaoOracle

router = APIRouter(prefix='/livre', tags=['livre'])
TAMANHO_CPF = 11


class ValidacaoPacienteInput(BaseModel):
    cpf: str = Field(min_length=11, max_length=14)
    dt_nascimento: date


class PacienteValidado(BaseModel):
    paciente: PacienteResumo


def _somente_digitos(valor: str | None) -> str | None:
    digitos = ''.join(ch for ch in str(valor or '') if ch.isdigit())
    return digitos or None


def _cpf_final(cpf: str | None) -> str | None:
    digitos = _somente_digitos(cpf)
    if not digitos or len(digitos) < 4:
        return None
    return digitos[-4:]


CONSULTA_PACIENTE_POR_CPF_NASCIMENTO = text(
    """
    SELECT *
      FROM (
        SELECT p.CD_PACIENTE AS cd_paciente,
               p.NM_PACIENTE AS nm_paciente,
               p.DT_NASCIMENTO AS dt_nascimento,
               p.TP_SEXO AS tp_sexo,
               p.NR_CPF AS nr_cpf,
               p.EMAIL AS email,
               TO_CHAR(p.NR_DDD_CELULAR) AS nr_ddd_celular,
               TO_CHAR(p.NR_CELULAR) AS nr_celular
          FROM DBAMV.PACIENTE p
         WHERE REGEXP_REPLACE(p.NR_CPF, '[^0-9]', '') = :cpf
           AND TRUNC(p.DT_NASCIMENTO) = TO_DATE(:dt_nascimento, 'YYYY-MM-DD')
         ORDER BY p.CD_PACIENTE DESC
      )
     WHERE ROWNUM = 1
    """
)


@router.get('/', status_code=HTTPStatus.OK)
def read_root():
    return {'message': 'Bem-vindo à API Hospital Prontocardio'}


@router.post(
    '/paciente/validar',
    status_code=HTTPStatus.OK,
    response_model=PacienteValidado,
)
def validar_paciente(
    payload: ValidacaoPacienteInput,
    session: Session = Depends(get_session_oracle),
):
    """Valida acesso do paciente pelo CPF e nascimento sem senha interna."""

    cpf = _somente_digitos(payload.cpf) or ''
    if len(cpf) != TAMANHO_CPF:
        raise HTTPException(
            status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
            detail='CPF invalido.',
        )

    try:
        row = (
            session
            .execute(
                CONSULTA_PACIENTE_POR_CPF_NASCIMENTO,
                {
                    'cpf': cpf,
                    'dt_nascimento': payload.dt_nascimento.isoformat(),
                },
            )
            .mappings()
            .first()
        )
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=HTTPStatus.SERVICE_UNAVAILABLE,
            detail='Nao foi possivel consultar o cadastro do paciente no MV.',
        ) from exc

    if not row:
        raise HTTPException(
            status_code=HTTPStatus.NOT_FOUND,
            detail='Paciente nao localizado para o CPF e nascimento informados.',
        )

    paciente = dict(row)
    nascimento = paciente.get('dt_nascimento')
    if isinstance(nascimento, datetime):
        paciente['dt_nascimento'] = nascimento.date()
    paciente['cpf_final'] = _cpf_final(paciente.pop('nr_cpf', None))
    return {'paciente': paciente}


@router.get(
    '/consultar_versao_oracle/',
    status_code=HTTPStatus.OK,
    response_model=list[VersaoOracle],
)
def versao_oracle(session: Session = Depends(get_session_oracle)):

    rows = session.execute(text('SELECT * FROM v$version')).mappings().all()

    versoes = [VersaoOracle(**row) for row in rows]

    return versoes
