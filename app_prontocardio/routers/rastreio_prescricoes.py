from datetime import date, datetime, timedelta
from http import HTTPStatus
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app_prontocardio.agenda_medica_models import (
    AgendaMedicaRastreioPrescricao,
)
from app_prontocardio.database import get_session_oracle, get_session_postgres
from app_prontocardio.models import Usuario
from app_prontocardio.security import valida_token_usuario_atual

router = APIRouter(
    prefix='/operacional/rastreio-prescricoes', tags=['operacional']
)
Oracle = Annotated[Session, Depends(get_session_oracle)]
Postgres = Annotated[Session, Depends(get_session_postgres)]
UsuarioAtual = Annotated[Usuario, Depends(valida_token_usuario_atual)]
FUSO_BRASIL = ZoneInfo('America/Sao_Paulo')
TELAS = {'agenda_medica_atendente', 'agenda_medica_admin'}
DIAS_ESPERA = 12


class DescartarInput(BaseModel):
    motivo: str = Field(min_length=5, max_length=1000)


def data_elegivel(data_prescricao: datetime) -> date:
    return data_prescricao.date() + timedelta(days=DIAS_ESPERA)


def _exigir_acesso(usuario: Usuario) -> None:
    if usuario.perfil not in {
        'ti',
        'administrador',
    } and not TELAS.intersection(usuario.telas_permitidas or []):
        raise HTTPException(
            status_code=HTTPStatus.FORBIDDEN,
            detail='Usuário sem acesso ao rastreio de prescrições.',
        )


CONSULTA_RASTREIO = text("""
WITH prescritos AS (
    SELECT a.CD_ATENDIMENTO cd_atendimento,
           a.CD_PACIENTE cd_paciente,
           a.DT_ATENDIMENTO data_atendimento,
           pm.CD_PRE_MED cd_pre_med,
           pm.HR_PRE_MED data_prescricao,
           pm.CD_PRESTADOR cd_prestador,
           ipm.CD_ITPRE_MED cd_itpre_med,
           ipm.CD_TIP_PRESC cd_tip_presc,
           COALESCE(rx.EXA_RX_CD_PRO_FAT, lab.CD_PRO_FAT, tp.CD_PRO_FAT)
               codigo_tuss,
           tp.CD_EXA_RX,
           tp.CD_EXA_LAB
      FROM DBAMV.ATENDIME a
      JOIN DBAMV.PRE_MED pm ON pm.CD_ATENDIMENTO = a.CD_ATENDIMENTO
      JOIN DBAMV.ITPRE_MED ipm ON ipm.CD_PRE_MED = pm.CD_PRE_MED
      JOIN DBAMV.TIP_PRESC tp ON tp.CD_TIP_PRESC = ipm.CD_TIP_PRESC
      LEFT JOIN DBAMV.EXA_RX rx ON rx.CD_EXA_RX = tp.CD_EXA_RX
      LEFT JOIN DBAMV.EXA_LAB lab ON lab.CD_EXA_LAB = tp.CD_EXA_LAB
     WHERE a.TP_ATENDIMENTO = 'A'
       AND a.DT_ATENDIMENTO >= SYSDATE - 2
       AND NVL(ipm.SN_CANCELADO, 'N') = 'N'
       AND (tp.SN_SOLICITACAO = 'S' OR tp.CD_EXA_RX IS NOT NULL
            OR tp.CD_EXA_LAB IS NOT NULL)
), mapeados AS (
    SELECT p.*, ia.CD_ITEM_AGENDAMENTO,
           ROW_NUMBER() OVER (
               PARTITION BY p.CD_ITPRE_MED
               ORDER BY ia.CD_ITEM_AGENDAMENTO
           ) ordem
      FROM prescritos p
      JOIN DBAMV.ITEM_AGENDAMENTO ia
        ON NVL(ia.SN_ATIVO, 'S') = 'S'
       AND ((p.CD_EXA_RX IS NOT NULL AND ia.CD_EXA_RX = p.CD_EXA_RX)
         OR (p.CD_EXA_LAB IS NOT NULL AND ia.CD_EXA_LAB = p.CD_EXA_LAB)
         OR (p.CODIGO_TUSS IS NOT NULL AND ia.CD_PRO_FAT = p.CODIGO_TUSS))
     WHERE EXISTS (
         SELECT 1
           FROM DBAMV.AGENDA_CENTRAL ac
           JOIN DBAMV.AGENDA_CENTRAL_ITEM_AGENDA acia
             ON acia.CD_AGENDA_CENTRAL = ac.CD_AGENDA_CENTRAL
            AND acia.CD_ITEM_AGENDAMENTO = ia.CD_ITEM_AGENDAMENTO
          WHERE ac.DT_AGENDA >= TRUNC(p.DATA_PRESCRICAO) + :dias_espera
            AND NVL(ac.SN_ATIVO, 'S') <> 'N'
     )
)
SELECT * FROM mapeados WHERE ordem = 1
""")


@router.post('/sincronizar')
def sincronizar(
    usuario: UsuarioAtual,
    oracle: Oracle,
    postgres: Postgres,
):
    _exigir_acesso(usuario)
    rows = (
        oracle
        .execute(CONSULTA_RASTREIO, {'dias_espera': DIAS_ESPERA})
        .mappings()
        .all()
    )
    novos = 0
    for row in rows:
        existente = postgres.scalar(
            select(AgendaMedicaRastreioPrescricao).where(
                AgendaMedicaRastreioPrescricao.cd_itpre_med
                == row['cd_itpre_med']
            )
        )
        if existente is not None:
            continue
        prescricao = row['data_prescricao'] or row['data_atendimento']
        elegivel = data_elegivel(prescricao)
        hoje = datetime.now(FUSO_BRASIL).date()
        postgres.add(
            AgendaMedicaRastreioPrescricao(
                cd_itpre_med=int(row['cd_itpre_med']),
                cd_pre_med=int(row['cd_pre_med']),
                cd_atendimento=int(row['cd_atendimento']),
                cd_paciente=int(row['cd_paciente']),
                cd_tip_presc=int(row['cd_tip_presc']),
                cd_item_agendamento=int(row['cd_item_agendamento']),
                data_atendimento=row['data_atendimento'],
                data_prescricao=prescricao,
                elegivel_em=elegivel,
                status='LIBERADA' if elegivel <= hoje else 'AGUARDANDO_D12',
                codigo_tuss=(
                    str(row['codigo_tuss']) if row['codigo_tuss'] else None
                ),
                cd_prestador=(
                    int(row['cd_prestador']) if row['cd_prestador'] else None
                ),
            )
        )
        novos += 1
    postgres.commit()
    return {'lidos': len(rows), 'novos': novos, 'dias_espera': DIAS_ESPERA}


@router.get('')
def listar(
    usuario: UsuarioAtual,
    oracle: Oracle,
    postgres: Postgres,
    status: Literal['AGUARDANDO_D12', 'LIBERADA', 'AGENDADA', 'DESCARTADA']
    | None = None,
    limite: int = Query(default=200, ge=1, le=500),
):
    _exigir_acesso(usuario)
    hoje = datetime.now(FUSO_BRASIL).date()
    vencidas = postgres.scalars(
        select(AgendaMedicaRastreioPrescricao).where(
            AgendaMedicaRastreioPrescricao.status == 'AGUARDANDO_D12',
            AgendaMedicaRastreioPrescricao.elegivel_em <= hoje,
        )
    ).all()
    for item in vencidas:
        item.status = 'LIBERADA'
    if vencidas:
        postgres.commit()
    consulta = select(AgendaMedicaRastreioPrescricao).order_by(
        AgendaMedicaRastreioPrescricao.data_prescricao.desc()
    )
    if status:
        consulta = consulta.where(
            AgendaMedicaRastreioPrescricao.status == status
        )
    itens = postgres.scalars(consulta.limit(limite)).all()
    detalhes = {}
    if itens:
        binds = ','.join(f':id{i}' for i in range(len(itens)))
        params = {f'id{i}': item.cd_itpre_med for i, item in enumerate(itens)}
        rows = (
            oracle
            .execute(
                text(f"""
            SELECT ipm.CD_ITPRE_MED cd_itpre_med,
                   p.NM_PACIENTE nm_paciente,
                   tp.DS_TIP_PRESC descricao_prescrita,
                   ia.DS_ITEM_AGENDAMENTO ds_item_agendamento,
                   prest.NM_PRESTADOR medico_solicitante
              FROM DBAMV.ITPRE_MED ipm
              JOIN DBAMV.PRE_MED pm ON pm.CD_PRE_MED = ipm.CD_PRE_MED
              JOIN DBAMV.ATENDIME a ON a.CD_ATENDIMENTO = pm.CD_ATENDIMENTO
              JOIN DBAMV.PACIENTE p ON p.CD_PACIENTE = a.CD_PACIENTE
              JOIN DBAMV.TIP_PRESC tp ON tp.CD_TIP_PRESC = ipm.CD_TIP_PRESC
              LEFT JOIN DBAMV.ITEM_AGENDAMENTO ia
                ON ia.CD_ITEM_AGENDAMENTO = :item_dummy
              LEFT JOIN DBAMV.PRESTADOR prest
                ON prest.CD_PRESTADOR = pm.CD_PRESTADOR
             WHERE ipm.CD_ITPRE_MED IN ({binds})
        """),
                {**params, 'item_dummy': -1},
            )
            .mappings()
            .all()
        )
        detalhes = {int(row['cd_itpre_med']): dict(row) for row in rows}
    resultado = []
    for item in itens:
        detalhe = detalhes.get(item.cd_itpre_med, {})
        resultado.append({
            'id': item.id,
            'cd_itpre_med': item.cd_itpre_med,
            'cd_atendimento': item.cd_atendimento,
            'cd_paciente': item.cd_paciente,
            'cd_item_agendamento': item.cd_item_agendamento,
            'paciente': detalhe.get('nm_paciente'),
            'exame': detalhe.get('descricao_prescrita'),
            'medico_solicitante': detalhe.get('medico_solicitante'),
            'data_prescricao': item.data_prescricao.isoformat(),
            'elegivel_em': item.elegivel_em.isoformat(),
            'status': item.status,
            'cd_it_agenda_central': item.cd_it_agenda_central,
            'protocolo_mv': item.protocolo_mv,
        })
    return {
        'itens': resultado,
        'total': len(resultado),
        'dias_espera': DIAS_ESPERA,
    }


@router.patch('/{rastreio_id}/descartar')
def descartar(
    rastreio_id: int,
    payload: DescartarInput,
    usuario: UsuarioAtual,
    postgres: Postgres,
):
    _exigir_acesso(usuario)
    item = postgres.get(AgendaMedicaRastreioPrescricao, rastreio_id)
    if item is None:
        raise HTTPException(
            status_code=HTTPStatus.NOT_FOUND, detail='Item não encontrado.'
        )
    if item.status == 'AGENDADA':
        raise HTTPException(
            status_code=HTTPStatus.CONFLICT, detail='Exame já agendado.'
        )
    item.status = 'DESCARTADA'
    item.motivo_descarte = payload.motivo.strip()
    item.atualizado_por_id = usuario.id
    item.atualizado_por_nome = usuario.nome
    postgres.commit()
    return {'id': item.id, 'status': item.status}
