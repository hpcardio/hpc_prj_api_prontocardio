from datetime import date, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session


def _age_at(birth: Any, reference: Any) -> int | None:
    if isinstance(birth, datetime):
        birth = birth.date()
    if isinstance(reference, datetime):
        reference = reference.date()
    if not isinstance(birth, date):
        return None
    if not isinstance(reference, date):
        reference = date.today()
    return reference.year - birth.year - ((reference.month, reference.day) < (birth.month, birth.day))


def patient_identification(session: Session, cd_atendimento: int) -> dict[str, Any]:
    row = session.execute(
        text(
            '''
            SELECT P.NM_PACIENTE AS nome_paciente,
                   P.DT_NASCIMENTO AS data_nascimento,
                   P.TP_SEXO AS sexo,
                   P.CD_PACIENTE AS prontuario,
                   A.DT_ATENDIMENTO AS data_atendimento
              FROM DBAMV.ATENDIME A
              JOIN DBAMV.PACIENTE P
                ON P.CD_PACIENTE = A.CD_PACIENTE
             WHERE A.CD_ATENDIMENTO = :cd_atendimento
            '''
        ),
        {'cd_atendimento': cd_atendimento},
    ).mappings().first()
    if not row:
        return {}
    name = str(row.get('nome_paciente') or '').strip() or None
    sex = str(row.get('sexo') or '').strip() or None
    record = str(row.get('prontuario') or '').strip() or None
    attendance_date = row.get('data_atendimento')
    return {
        'nome_paciente': name,
        'idade': _age_at(row.get('data_nascimento'), attendance_date),
        'sexo': sex,
        'prontuario': record,
        'data_atendimento': attendance_date,
    }
