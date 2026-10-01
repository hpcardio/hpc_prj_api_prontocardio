import os
from datetime import date, datetime, time, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app_prontocardio.database import get_session_oracle
from app_prontocardio.models import Usuario
from app_prontocardio.security import valida_token_usuario_atual

router = APIRouter()

CONSULTA = text("""
    SELECT i.CD_IT_AGENDA_CENTRAL AS "id",
           i.CD_PACIENTE AS "paciente_id",
           COALESCE(p.NM_PACIENTE, i.NM_PACIENTE) AS "paciente",
           i.CD_CONVENIO AS "convenio_id",
           ia.DS_ITEM_AGENDAMENTO AS "procedimento",
           pr.NM_PRESTADOR AS "prestador",
           TO_CHAR(i.HR_AGENDA, 'HH24:MI') AS "horario"
    FROM DBAMV.IT_AGENDA_CENTRAL i
    JOIN DBAMV.AGENDA_CENTRAL ac ON ac.CD_AGENDA_CENTRAL = i.CD_AGENDA_CENTRAL
    LEFT JOIN DBAMV.PRESTADOR pr ON pr.CD_PRESTADOR = ac.CD_PRESTADOR
    LEFT JOIN DBAMV.PACIENTE p ON p.CD_PACIENTE = i.CD_PACIENTE
    LEFT JOIN DBAMV.ITEM_AGENDAMENTO ia ON ia.CD_ITEM_AGENDAMENTO = i.CD_ITEM_AGENDAMENTO
    LEFT JOIN DBAMV.IT_MOVIMENTO_AGENDA_CENTRAL m
      ON m.CD_IT_AGENDA_CENTRAL = i.CD_IT_AGENDA_CENTRAL
     AND m.CD_IT_MOVIMENTO_AGENDA_CENTRAL = (
        SELECT MAX(mx.CD_IT_MOVIMENTO_AGENDA_CENTRAL)
        FROM DBAMV.IT_MOVIMENTO_AGENDA_CENTRAL mx
        WHERE mx.CD_IT_AGENDA_CENTRAL = i.CD_IT_AGENDA_CENTRAL
     )
    WHERE i.HR_AGENDA >= :inicio AND i.HR_AGENDA < :fim
      AND i.CD_CONVENIO IN (59, 63)
      AND NVL(i.SN_BLOQUEADO, 'N') = 'N'
      AND (m.TP_STATUS IS NULL OR m.TP_STATUS NOT IN ('E', 'C', 'P', 'T'))
      AND COALESCE(p.NM_PACIENTE, i.NM_PACIENTE) IS NOT NULL
    ORDER BY i.CD_CONVENIO, i.HR_AGENDA, i.CD_IT_AGENDA_CENTRAL
""")


def exigir_acesso(usuario):
    # Only named integration accounts or an explicit permission may read patient lists.
    allowed = {name.strip().casefold() for name in os.getenv('WHATSAPP_AGENDA_USUARIOS', '').split(',') if name.strip()}
    if not getattr(usuario, 'ativo', False) or (
        str(getattr(usuario, 'email', '')).strip().casefold() not in allowed
        and 'whatsapp_agenda_rede_checkup' not in (getattr(usuario, 'telas_permitidas', None) or [])
    ):
        raise HTTPException(status_code=403, detail='Sem permissao para consultar esta agenda.')


@router.get('/agenda-rede-checkup')
def agenda_rede_checkup(
    response: Response,
    usuario: Annotated[Usuario, Depends(valida_token_usuario_atual)],
    dia: Annotated[date, Query()],
    oracle: Session = Depends(get_session_oracle),
):
    exigir_acesso(usuario)
    response.headers['Cache-Control'] = 'no-store, private'
    inicio = datetime.combine(dia, time.min)
    try:
        rows = oracle.execute(CONSULTA, {'inicio': inicio, 'fim': inicio + timedelta(days=1)}).mappings().fetchmany(5001)
    except SQLAlchemyError as exc:
        raise HTTPException(status_code=503, detail='Nao foi possivel consultar a agenda no MV.') from exc
    if len(rows) > 5000:
        raise HTTPException(status_code=422, detail='Agenda excede o limite; nenhum dado foi truncado.')
    return {'dia': dia.isoformat(), 'agendamentos': [dict(row) for row in rows]}
