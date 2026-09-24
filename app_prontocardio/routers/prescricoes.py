from datetime import datetime
from http import HTTPStatus

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app_prontocardio.database import get_session_oracle
from app_prontocardio.prescricao_schema import PrescricoesPaciente
from app_prontocardio.routers.paciente_auth import (
    PrincipalPaciente,
    validar_paciente_atual,
)

router = APIRouter(prefix='/paciente', tags=['paciente'])


CONSULTA_ITENS_PRESCRITOS = text(
    """
    WITH itens_prescritos AS (
        SELECT a.CD_ATENDIMENTO AS cd_atendimento,
               a.DT_ATENDIMENTO AS data_atendimento,
               pm.CD_PRE_MED AS cd_pre_med,
               pm.HR_PRE_MED AS data_prescricao,
               pm.SN_FECHADO AS prescricao_fechada,
               pm.CD_PRESTADOR AS cd_prestador,
               prest.NM_PRESTADOR AS medico_solicitante,
               ipm.CD_ITPRE_MED AS cd_itpre_med,
               ipm.CD_TIP_PRESC AS cd_tip_presc,
               tp.DS_TIP_PRESC AS descricao_prescrita,
               tp.CD_EXA_RX AS cd_exa_rx,
               tp.CD_EXA_LAB AS cd_exa_lab,
               COALESCE(
                   rx.EXA_RX_CD_PRO_FAT,
                   lab.CD_PRO_FAT,
                   tp.CD_PRO_FAT
               ) AS codigo_tuss
          FROM DBAMV.ATENDIME a
          JOIN DBAMV.PRE_MED pm
            ON pm.CD_ATENDIMENTO = a.CD_ATENDIMENTO
          JOIN DBAMV.ITPRE_MED ipm
            ON ipm.CD_PRE_MED = pm.CD_PRE_MED
          JOIN DBAMV.TIP_PRESC tp
            ON tp.CD_TIP_PRESC = ipm.CD_TIP_PRESC
          LEFT JOIN DBAMV.EXA_RX rx
            ON rx.CD_EXA_RX = tp.CD_EXA_RX
          LEFT JOIN DBAMV.EXA_LAB lab
            ON lab.CD_EXA_LAB = tp.CD_EXA_LAB
          LEFT JOIN DBAMV.PRESTADOR prest
            ON prest.CD_PRESTADOR = pm.CD_PRESTADOR
         WHERE a.CD_PACIENTE = :cd_paciente
           AND a.DT_ATENDIMENTO >= ADD_MONTHS(TRUNC(SYSDATE), -12)
           AND pm.CD_PRE_MED = (
                SELECT MAX(pm2.CD_PRE_MED)
                  FROM DBAMV.PRE_MED pm2
                 WHERE pm2.CD_ATENDIMENTO = a.CD_ATENDIMENTO
                   AND EXISTS (
                        SELECT 1
                          FROM DBAMV.ITPRE_MED ipm2
                          JOIN DBAMV.TIP_PRESC tp2
                            ON tp2.CD_TIP_PRESC = ipm2.CD_TIP_PRESC
                         WHERE ipm2.CD_PRE_MED = pm2.CD_PRE_MED
                           AND NVL(ipm2.SN_CANCELADO, 'N') = 'N'
                           AND (
                                tp2.SN_SOLICITACAO = 'S'
                                OR tp2.CD_EXA_RX IS NOT NULL
                                OR tp2.CD_EXA_LAB IS NOT NULL
                           )
                   )
           )
           AND NVL(ipm.SN_CANCELADO, 'N') = 'N'
           AND (
                tp.SN_SOLICITACAO = 'S'
                OR tp.CD_EXA_RX IS NOT NULL
                OR tp.CD_EXA_LAB IS NOT NULL
           )
    ),
    itens_mapeados AS (
        SELECT prescrito.*,
               ia.CD_ITEM_AGENDAMENTO AS cd_item_agendamento,
               ia.DS_ITEM_AGENDAMENTO AS ds_item_agendamento,
               ia.HR_REALIZACAO AS duracao,
               ia.TP_ITEM AS tipo_item
          FROM itens_prescritos prescrito
          LEFT JOIN DBAMV.ITEM_AGENDAMENTO ia
            ON NVL(ia.SN_ATIVO, 'S') = 'S'
           AND (
                (
                    prescrito.CD_EXA_RX IS NOT NULL
                    AND ia.CD_EXA_RX = prescrito.CD_EXA_RX
                )
                OR (
                    prescrito.CD_EXA_LAB IS NOT NULL
                    AND ia.CD_EXA_LAB = prescrito.CD_EXA_LAB
                )
                OR (
                    prescrito.CODIGO_TUSS IS NOT NULL
                    AND ia.CD_PRO_FAT = prescrito.CODIGO_TUSS
                )
           )
    ),
    itens_avaliados AS (
        SELECT mapeado.*,
               CASE
                   WHEN mapeado.CD_ITEM_AGENDAMENTO IS NOT NULL
                    AND EXISTS (
                        SELECT 1
                          FROM DBAMV.IT_AGENDA_CENTRAL iac
                          JOIN DBAMV.AGENDA_CENTRAL ac
                            ON ac.CD_AGENDA_CENTRAL =
                               iac.CD_AGENDA_CENTRAL
                          LEFT JOIN DBAMV.AGENDA_CENTRAL_ITEM_AGENDA acia
                            ON acia.CD_AGENDA_CENTRAL =
                               ac.CD_AGENDA_CENTRAL
                           AND acia.CD_ITEM_AGENDAMENTO =
                               mapeado.CD_ITEM_AGENDAMENTO
                          LEFT JOIN DBAMV.RECURSO_CENTRAL recurso
                            ON recurso.CD_RECURSO_CENTRAL =
                               ac.CD_RECURSO_CENTRAL
                          LEFT JOIN DBAMV.SETOR setor
                            ON setor.CD_SETOR = ac.CD_SETOR
                         WHERE (
                                acia.CD_ITEM_AGENDAMENTO IS NOT NULL
                                OR (
                                    mapeado.TIPO_ITEM = 'L'
                                    AND (
                                        ac.TP_AGENDA = 'L'
                                        OR UPPER(
                                            NVL(
                                                recurso.DS_RECURSO_CENTRAL,
                                                ''
                                            )
                                        ) LIKE '%LAB%'
                                        OR UPPER(
                                            NVL(setor.NM_SETOR, '')
                                        ) LIKE '%LAB%'
                                    )
                                )
                           )
                           AND ac.DT_AGENDA >= TRUNC(SYSDATE)
                           AND iac.HR_AGENDA >= SYSDATE
                           AND iac.CD_PACIENTE IS NULL
                           AND iac.DT_GRAVACAO IS NULL
                           AND NVL(iac.SN_BLOQUEADO, 'N') <> 'S'
                           AND NVL(iac.SN_ENCAIXE, 'N') <> 'S'
                           AND NVL(iac.TP_SITUACAO, 'M') <> 'C'
                           AND ac.DT_LIBERACAO < SYSDATE
                           AND NVL(ac.SN_ATIVO, 'S') <> 'N'
                           AND NVL(ac.SN_FALTA, 'N') <> 'S'
                           AND NVL(ac.QT_MARCADOS, 0) <
                               ac.QT_ATENDIMENTO
                    )
                   THEN 0
                   ELSE 1
               END AS ordem_disponibilidade
          FROM itens_mapeados mapeado
    ),
    candidatos AS (
        SELECT avaliado.*,
               ROW_NUMBER() OVER (
                   PARTITION BY avaliado.CD_ITPRE_MED
                   ORDER BY avaliado.ORDEM_DISPONIBILIDADE,
                            avaliado.CD_ITEM_AGENDAMENTO
               ) AS ordem_item
          FROM itens_avaliados avaliado
    )
    SELECT cd_atendimento,
           data_atendimento,
           cd_pre_med,
           data_prescricao,
           prescricao_fechada,
           medico_solicitante,
           cd_itpre_med,
           cd_tip_presc,
           descricao_prescrita,
           codigo_tuss,
           cd_item_agendamento,
           ds_item_agendamento,
           duracao,
           ordem_disponibilidade
      FROM candidatos
     WHERE ordem_item = 1
     ORDER BY data_prescricao DESC NULLS LAST,
              cd_pre_med DESC,
              cd_itpre_med
    """
)


CONSULTA_ITENS_JA_AGENDADOS = text(
    """
    SELECT DISTINCT i.CD_ITEM_AGENDAMENTO AS cd_item_agendamento
      FROM DBAMV.IT_AGENDA_CENTRAL i
     WHERE i.CD_PACIENTE = :cd_paciente
       AND i.HR_AGENDA >= SYSDATE
       AND i.CD_IT_AGENDA_PAI IS NULL
       AND NVL(i.TP_SITUACAO, 'M') <> 'C'
    """
)


def _duracao_minutos(valor: object) -> int | None:
    if isinstance(valor, datetime):
        return valor.hour * 60 + valor.minute
    return None


@router.get(
    '/prescricoes',
    status_code=HTTPStatus.OK,
    response_model=PrescricoesPaciente,
)
def consultar_prescricoes_paciente(
    paciente_atual: PrincipalPaciente = Depends(validar_paciente_atual),
    session: Session = Depends(get_session_oracle),
):
    try:
        rows = (
            session.execute(
                CONSULTA_ITENS_PRESCRITOS,
                {'cd_paciente': paciente_atual.cd_paciente},
            )
            .mappings()
            .all()
        )
        agendados = {
            int(row['cd_item_agendamento'])
            for row in (
                session.execute(
                    CONSULTA_ITENS_JA_AGENDADOS,
                    {'cd_paciente': paciente_atual.cd_paciente},
                )
                .mappings()
                .all()
            )
            if row.get('cd_item_agendamento') is not None
        }
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=HTTPStatus.SERVICE_UNAVAILABLE,
            detail='Não foi possível consultar os itens prescritos no MV.',
        ) from exc

    itens = []
    for row in rows:
        registro = dict(row)
        item_agendamento = registro.get('cd_item_agendamento')
        item_agendamento = (
            int(item_agendamento) if item_agendamento is not None else None
        )
        agendado = item_agendamento in agendados
        possui_agenda = registro.get('ordem_disponibilidade') == 0
        agendavel = item_agendamento is not None and possui_agenda
        motivo = None
        if item_agendamento is None:
            motivo = (
                'Este item ainda não está relacionado ao catálogo de '
                'agendamento digital.'
            )
        elif not possui_agenda:
            motivo = 'Ainda não há agenda futura liberada para este item.'

        codigo_tuss = registro.get('codigo_tuss')
        itens.append(
            {
                'cd_atendimento': int(registro['cd_atendimento']),
                'data_atendimento': registro['data_atendimento'],
                'cd_pre_med': int(registro['cd_pre_med']),
                'data_prescricao': registro.get('data_prescricao'),
                'prescricao_fechada': (
                    registro.get('prescricao_fechada') == 'S'
                ),
                'cd_itpre_med': int(registro['cd_itpre_med']),
                'cd_tip_presc': int(registro['cd_tip_presc']),
                'descricao_prescrita': (
                    registro.get('descricao_prescrita')
                    or registro.get('ds_item_agendamento')
                    or 'Item prescrito'
                ),
                'codigo_tuss': (
                    str(codigo_tuss) if codigo_tuss is not None else None
                ),
                'cd_item_agendamento': item_agendamento,
                'ds_item_agendamento': registro.get('ds_item_agendamento'),
                'duracao_minutos': _duracao_minutos(
                    registro.get('duracao')
                ),
                'medico_solicitante': registro.get('medico_solicitante'),
                'agendavel': agendavel,
                'agendado': agendado,
                'motivo_indisponibilidade': motivo,
            }
        )

    total_agendados = sum(1 for item in itens if item['agendado'])
    total_pendentes = sum(
        1 for item in itens if item['agendavel'] and not item['agendado']
    )
    total_nao_mapeados = sum(
        1 for item in itens if item['cd_item_agendamento'] is None
    )
    recomendacao = (
        'Para facilitar sua jornada, procure agendar todos os itens '
        'pendentes no mesmo dia.'
        if total_pendentes > 1
        else 'Confira os itens prescritos e escolha o melhor horário.'
    )
    return {
        'itens': itens,
        'total': len(itens),
        'total_pendentes': total_pendentes,
        'total_agendados': total_agendados,
        'total_nao_mapeados': total_nao_mapeados,
        'recomendacao': recomendacao,
    }
