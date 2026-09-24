import os
import re
from datetime import date, datetime, timedelta
from decimal import Decimal
from http import HTTPStatus
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app_prontocardio.biq_hemodinamica import consultar_receita_hemodinamica
from app_prontocardio.database import get_session_oracle
from app_prontocardio.models import Usuario
from app_prontocardio.security import valida_token_usuario_atual

router = APIRouter(prefix="/biq", tags=["biq"])

ValidaUsuarioAtual = Annotated[Usuario, Depends(valida_token_usuario_atual)]
MAX_CODIGOS_REDE = 500
USUARIO_MV_PATTERN = re.compile(r'^[A-Z0-9_.-]{2,30}$')


CONSULTA_BIQ_CONSULTAS_AMBULATORIAIS_REALIZADAS = text(
    """
    SELECT a.CD_PRESTADOR AS "cd_prestador",
           pr.NM_PRESTADOR AS "nm_prestador",
           pr.DS_CODIGO_CONSELHO AS "crm",
           COUNT(CASE
               WHEN TRUNC(a.HR_ATENDIMENTO) -
                    TRUNC(a.HR_ATENDIMENTO, 'IW') BETWEEN 0 AND 4
               THEN 1
           END) AS "consultas_semana",
           COUNT(CASE
               WHEN TRUNC(a.HR_ATENDIMENTO) -
                    TRUNC(a.HR_ATENDIMENTO, 'IW') NOT BETWEEN 0 AND 4
               THEN 1
           END) AS "consultas_fds",
           COUNT(*) AS "consultas_total"
      FROM DBAMV.ATENDIME a
      JOIN DBAMV.PRESTADOR pr
        ON pr.CD_PRESTADOR = a.CD_PRESTADOR
     WHERE a.TP_ATENDIMENTO = 'A'
       AND a.HR_ATENDIMENTO >= :data_inicio
       AND a.HR_ATENDIMENTO < :data_fim_exclusiva
       AND a.CD_PRESTADOR IS NOT NULL
     GROUP BY a.CD_PRESTADOR,
              pr.NM_PRESTADOR,
              pr.DS_CODIGO_CONSELHO
     ORDER BY pr.NM_PRESTADOR
    """
)



def _normalizar_convenios(cd_convenio: str | None):
    if not cd_convenio:
        return None
    codigos = [item.strip() for item in str(cd_convenio).split(',') if item.strip()]
    if not codigos:
        return None
    if any(not item.isdigit() for item in codigos):
        raise HTTPException(
            status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
            detail="cd_convenio deve conter apenas números separados por vírgula.",
        )
    return ','.join(dict.fromkeys(codigos))


def _normalizar_codigos_rede(valor: str | None, campo: str):
    if not valor:
        return None
    codigos = [
        item.strip() for item in str(valor).split(',') if item.strip()
    ]
    if not codigos:
        return None
    if len(codigos) > MAX_CODIGOS_REDE or any(
        not item.isdigit() for item in codigos
    ):
        raise HTTPException(
            status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
            detail=(
                f'{campo} deve conter até {MAX_CODIGOS_REDE} códigos '
                'numéricos separados por vírgula.'
            ),
        )
    return ','.join(dict.fromkeys(codigos))


def _periodo_inclusivo(
    data_inicio: date,
    data_fim: date,
    cd_convenio: str | None = None,
    procedimento: str | None = None,
):
    if data_fim < data_inicio:
        raise HTTPException(
            status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
            detail="data_fim deve ser igual ou posterior a data_inicio.",
        )
    data_fim_exclusiva = data_fim + timedelta(days=1)
    return {
        "data_inicio": data_inicio,
        "data_fim_exclusiva": data_fim_exclusiva,
        "data_inicio_receita": max(data_inicio, data_fim_exclusiva - timedelta(days=7)),
        "cd_convenio": _normalizar_convenios(cd_convenio),
        "procedimento": procedimento,
    }


def _json_value(value):
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


def _rows_to_dict(rows):
    return [
        {key: _json_value(value) for key, value in dict(row).items()}
        for row in rows
    ]


@router.get(
    '/consultas-ambulatoriais-realizadas',
    status_code=HTTPStatus.OK,
)
def consultar_consultas_ambulatoriais_realizadas(
    usuario_atual: ValidaUsuarioAtual,
    data_inicio: date,
    data_fim: date,
    session: Session = Depends(get_session_oracle),
):
    """Resume atendimentos ambulatoriais realizados por prestador."""
    del usuario_atual
    params = _periodo_inclusivo(data_inicio, data_fim)
    try:
        rows = session.execute(
            CONSULTA_BIQ_CONSULTAS_AMBULATORIAIS_REALIZADAS,
            params,
        ).mappings().all()
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=HTTPStatus.SERVICE_UNAVAILABLE,
            detail=(
                'Não foi possível totalizar as consultas ambulatoriais '
                'realizadas no MV.'
            ),
        ) from exc

    consultas = _rows_to_dict(rows)
    return {
        'periodo': {
            'data_inicio': data_inicio.isoformat(),
            'data_fim': data_fim.isoformat(),
        },
        'consultas': consultas,
        'total_consultas': sum(
            int(item.get('consultas_total') or 0) for item in consultas
        ),
        'total_prestadores': len(consultas),
    }

def _normalizar_usuario_mv(valor: str) -> str:
    usuario = str(valor or '').strip().upper()
    if not USUARIO_MV_PATTERN.fullmatch(usuario):
        raise HTTPException(
            status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
            detail='Usuário MV inválido.',
        )
    return usuario


def _usuarios_mv_permitidos() -> set[str]:
    return {
        usuario.strip().upper()
        for usuario in os.getenv('PRONTOREDE_MV_ALLOWED_USERS', '').split(',')
        if usuario.strip()
    }


def _inteiro_positivo(valor: object) -> int | None:
    if isinstance(valor, bool) or not isinstance(valor, (int, Decimal)):
        return None
    inteiro = int(valor)
    if inteiro <= 0 or Decimal(str(valor)) != Decimal(inteiro):
        return None
    return inteiro


def _texto_linha(valor: object) -> str:
    return valor.strip() if isinstance(valor, str) else ''

CONSULTA_TESTE_ERGOMETRICO_AGENDADOS = text(
    """
    SELECT i.CD_IT_AGENDA_CENTRAL AS cd_it_agenda_central,
           a.CD_ATENDIMENTO AS cd_atendimento,
           i.HR_AGENDA AS data_agenda,
           a.HR_ATENDIMENTO AS dt_atendimento,
           i.CD_PACIENTE AS cd_paciente,
           pac.NM_PACIENTE AS nm_paciente,
           a.CD_PRESTADOR AS cd_prestador_atendimento,
           p.NM_PRESTADOR AS nm_prestador_atendimento,
           p.DS_CODIGO_CONSELHO AS crm_atendimento,
           ia.CD_ITEM_AGENDAMENTO AS cd_item_agendamento,
           ia.DS_ITEM_AGENDAMENTO AS ds_item_agendamento,
           i.DS_OBSERVACAO AS ds_observacao,
           i.DS_OBSERVACAO_GERAL AS ds_observacao_geral,
           a.TP_ATENDIMENTO AS tp_atendimento
      FROM DBAMV.IT_AGENDA_CENTRAL i
      JOIN DBAMV.ITEM_AGENDAMENTO ia
        ON ia.CD_ITEM_AGENDAMENTO = i.CD_ITEM_AGENDAMENTO
      LEFT JOIN DBAMV.PACIENTE pac
        ON pac.CD_PACIENTE = i.CD_PACIENTE
      LEFT JOIN DBAMV.ATENDIME a
        ON a.CD_PACIENTE = i.CD_PACIENTE
       AND a.TP_ATENDIMENTO = 'E'
       AND TRUNC(a.HR_ATENDIMENTO) = TRUNC(i.HR_AGENDA)
      LEFT JOIN DBAMV.PRESTADOR p
        ON p.CD_PRESTADOR = a.CD_PRESTADOR
     WHERE i.HR_AGENDA >= :data_inicio
       AND i.HR_AGENDA < :data_fim
       AND UPPER(ia.DS_ITEM_AGENDAMENTO) LIKE '%ERGOMETR%'
       AND (
            UPPER(NVL(i.DS_OBSERVACAO, '')) LIKE '%CONFIRMADO%'
         OR UPPER(NVL(i.DS_OBSERVACAO_GERAL, '')) LIKE '%CONFIRMADO%'
       )
     ORDER BY i.HR_AGENDA, pac.NM_PACIENTE, a.CD_ATENDIMENTO
    """
)

CONSULTA_TESTE_ERGOMETRICO_ATENDIMENTOS = text(
    """
    SELECT DISTINCT
           a.CD_ATENDIMENTO AS cd_atendimento,
           a.HR_ATENDIMENTO AS dt_atendimento,
           a.CD_PACIENTE AS cd_paciente,
           pac.NM_PACIENTE AS nm_paciente,
           ira.CD_PRO_FAT AS cd_pro_fat,
           pf.DS_PRO_FAT AS ds_pro_fat,
           a.CD_PRESTADOR AS cd_prestador_atendimento,
           p.NM_PRESTADOR AS nm_prestador_atendimento,
           p.DS_CODIGO_CONSELHO AS crm_atendimento,
           a.TP_ATENDIMENTO AS tp_atendimento
      FROM DBAMV.ATENDIME a
      JOIN DBAMV.ITREG_AMB ira
        ON ira.CD_ATENDIMENTO = a.CD_ATENDIMENTO
       AND ira.CD_PRO_FAT IN (
            40101037, 40101045, 20101056, 18000002,
            70051021, 22051014, 10210004, 20010028
       )
      LEFT JOIN DBAMV.PRO_FAT pf
        ON pf.CD_PRO_FAT = ira.CD_PRO_FAT
      LEFT JOIN DBAMV.PACIENTE pac
        ON pac.CD_PACIENTE = a.CD_PACIENTE
      LEFT JOIN DBAMV.PRESTADOR p
        ON p.CD_PRESTADOR = a.CD_PRESTADOR
     WHERE a.TP_ATENDIMENTO = 'E'
       AND a.HR_ATENDIMENTO >= :data_inicio
       AND a.HR_ATENDIMENTO < :data_fim
     ORDER BY a.HR_ATENDIMENTO, pac.NM_PACIENTE, a.CD_ATENDIMENTO
    """
)

CONSULTA_TESTE_ERGOMETRICO_ATENDIMENTOS_CHECKUP = text(
    """
    SELECT DISTINCT
           a.CD_ATENDIMENTO AS cd_atendimento,
           ped.HR_PEDIDO AS dt_atendimento,
           a.CD_PACIENTE AS cd_paciente,
           pac.NM_PACIENTE AS nm_paciente,
           exa.EXA_RX_CD_PRO_FAT AS cd_pro_fat,
           pf.DS_PRO_FAT AS ds_pro_fat,
           a.CD_PRESTADOR AS cd_prestador_atendimento,
           p.NM_PRESTADOR AS nm_prestador_atendimento,
           p.DS_CODIGO_CONSELHO AS crm_atendimento,
           a.TP_ATENDIMENTO AS tp_atendimento
      FROM DBAMV.ATENDIME a
      JOIN DBAMV.PED_RX ped
        ON ped.CD_ATENDIMENTO = a.CD_ATENDIMENTO
      JOIN DBAMV.ITPED_RX ipr
        ON ipr.CD_PED_RX = ped.CD_PED_RX
      JOIN DBAMV.EXA_RX exa
        ON exa.CD_EXA_RX = ipr.CD_EXA_RX
       AND exa.EXA_RX_CD_PRO_FAT IN (
            40101037, 40101045, 20101056, 18000002,
            70051021, 22051014, 10210004, 20010028
       )
      LEFT JOIN DBAMV.PRO_FAT pf
        ON pf.CD_PRO_FAT = exa.EXA_RX_CD_PRO_FAT
      LEFT JOIN DBAMV.PACIENTE pac
        ON pac.CD_PACIENTE = a.CD_PACIENTE
      LEFT JOIN DBAMV.PRESTADOR p
        ON p.CD_PRESTADOR = a.CD_PRESTADOR
     WHERE a.TP_ATENDIMENTO = 'I'
       AND a.CD_CONVENIO = 59
       AND ped.HR_PEDIDO >= :data_inicio
       AND ped.HR_PEDIDO < :data_fim
     ORDER BY ped.HR_PEDIDO, pac.NM_PACIENTE, a.CD_ATENDIMENTO
    """
)

CONSULTA_TESTE_ERGOMETRICO_LAUDOS = text(
    """
    SELECT DISTINCT
           a.CD_ATENDIMENTO AS cd_atendimento,
           a.HR_ATENDIMENTO AS dt_atendimento,
           a.CD_PACIENTE AS cd_paciente,
           pac.NM_PACIENTE AS nm_paciente,
           ira.CD_PRO_FAT AS cd_pro_fat,
           pf.DS_PRO_FAT AS ds_pro_fat,
           ped.CD_PED_RX AS cd_ped_rx,
           ipr.CD_ITPED_RX AS cd_itped_rx,
           lr.CD_LAUDO AS cd_laudo,
           lr.DT_LAUDO AS dt_laudo,
           NVL(lr.CD_PRESTADOR_ASSINATURA, lr.CD_PRESTADOR) AS cd_prestador,
           p.NM_PRESTADOR AS nm_prestador,
           p.DS_CODIGO_CONSELHO AS crm,
           a.CD_PRESTADOR AS cd_prestador_atendimento,
           pa.NM_PRESTADOR AS nm_prestador_atendimento,
           pa.DS_CODIGO_CONSELHO AS crm_atendimento,
           a.TP_ATENDIMENTO AS tp_atendimento
      FROM DBAMV.ATENDIME a
      JOIN DBAMV.ITREG_AMB ira
        ON ira.CD_ATENDIMENTO = a.CD_ATENDIMENTO
       AND ira.CD_PRO_FAT IN (
            40101037, 40101045, 20101056, 18000002,
            70051021, 22051014, 10210004, 20010028
       )
      LEFT JOIN DBAMV.PRO_FAT pf
        ON pf.CD_PRO_FAT = ira.CD_PRO_FAT
      LEFT JOIN DBAMV.PACIENTE pac
        ON pac.CD_PACIENTE = a.CD_PACIENTE
      JOIN DBAMV.PED_RX ped
        ON ped.CD_ATENDIMENTO = a.CD_ATENDIMENTO
      JOIN DBAMV.ITPED_RX ipr
        ON ipr.CD_PED_RX = ped.CD_PED_RX
      JOIN DBAMV.LAUDO_RX lr
        ON lr.CD_LAUDO = ipr.CD_LAUDO
      LEFT JOIN DBAMV.PRESTADOR p
        ON p.CD_PRESTADOR = NVL(lr.CD_PRESTADOR_ASSINATURA, lr.CD_PRESTADOR)
      LEFT JOIN DBAMV.PRESTADOR pa
        ON pa.CD_PRESTADOR = a.CD_PRESTADOR
     WHERE a.TP_ATENDIMENTO = 'E'
       AND a.HR_ATENDIMENTO >= :data_inicio
       AND a.HR_ATENDIMENTO < :data_fim
       AND lr.DT_LAUDO >= :data_inicio
       AND lr.DT_LAUDO < :data_fim
       AND NVL(lr.CD_PRESTADOR_ASSINATURA, lr.CD_PRESTADOR) IS NOT NULL
     ORDER BY lr.DT_LAUDO, p.NM_PRESTADOR, a.CD_ATENDIMENTO, lr.CD_LAUDO
    """
)

CONSULTA_TESTE_ERGOMETRICO_LAUDOS_CHECKUP = text(
    """
    SELECT DISTINCT
           a.CD_ATENDIMENTO AS cd_atendimento,
           ped.HR_PEDIDO AS dt_atendimento,
           a.CD_PACIENTE AS cd_paciente,
           pac.NM_PACIENTE AS nm_paciente,
           exa.EXA_RX_CD_PRO_FAT AS cd_pro_fat,
           pf.DS_PRO_FAT AS ds_pro_fat,
           ped.CD_PED_RX AS cd_ped_rx,
           ipr.CD_ITPED_RX AS cd_itped_rx,
           lr.CD_LAUDO AS cd_laudo,
           lr.DT_LAUDO AS dt_laudo,
           NVL(lr.CD_PRESTADOR_ASSINATURA, lr.CD_PRESTADOR) AS cd_prestador,
           p.NM_PRESTADOR AS nm_prestador,
           p.DS_CODIGO_CONSELHO AS crm,
           a.CD_PRESTADOR AS cd_prestador_atendimento,
           pa.NM_PRESTADOR AS nm_prestador_atendimento,
           pa.DS_CODIGO_CONSELHO AS crm_atendimento,
           a.TP_ATENDIMENTO AS tp_atendimento
      FROM DBAMV.ATENDIME a
      JOIN DBAMV.PED_RX ped
        ON ped.CD_ATENDIMENTO = a.CD_ATENDIMENTO
      JOIN DBAMV.ITPED_RX ipr
        ON ipr.CD_PED_RX = ped.CD_PED_RX
      JOIN DBAMV.EXA_RX exa
        ON exa.CD_EXA_RX = ipr.CD_EXA_RX
       AND exa.EXA_RX_CD_PRO_FAT IN (
            40101037, 40101045, 20101056, 18000002,
            70051021, 22051014, 10210004, 20010028
       )
      LEFT JOIN DBAMV.PRO_FAT pf
        ON pf.CD_PRO_FAT = exa.EXA_RX_CD_PRO_FAT
      LEFT JOIN DBAMV.PACIENTE pac
        ON pac.CD_PACIENTE = a.CD_PACIENTE
      JOIN DBAMV.LAUDO_RX lr
        ON lr.CD_LAUDO = ipr.CD_LAUDO
      LEFT JOIN DBAMV.PRESTADOR p
        ON p.CD_PRESTADOR = NVL(lr.CD_PRESTADOR_ASSINATURA, lr.CD_PRESTADOR)
      LEFT JOIN DBAMV.PRESTADOR pa
        ON pa.CD_PRESTADOR = a.CD_PRESTADOR
     WHERE a.TP_ATENDIMENTO = 'I'
       AND a.CD_CONVENIO = 59
       AND ped.HR_PEDIDO >= :data_inicio
       AND ped.HR_PEDIDO < :data_fim
       AND lr.DT_LAUDO >= :data_inicio
       AND lr.DT_LAUDO < :data_fim
       AND NVL(lr.CD_PRESTADOR_ASSINATURA, lr.CD_PRESTADOR) IS NOT NULL
     ORDER BY lr.DT_LAUDO, p.NM_PRESTADOR, a.CD_ATENDIMENTO, lr.CD_LAUDO
    """
)

CONSULTA_INDICADORES_HOSPITALARES_RESUMO = text(
    """
    WITH leitos AS (
        SELECT l.cd_leito
        FROM dbamv.leito l
        WHERE NVL(l.tp_situacao, 'A') <> 'I'
          AND NVL(l.dt_ativacao, TRUNC(SYSDATE)) <= TRUNC(SYSDATE)
          AND NVL(l.dt_desativacao, TRUNC(SYSDATE) + 1) > TRUNC(SYSDATE)
    ),
    ocupacao AS (
        SELECT
            COUNT(DISTINCT l.cd_leito) AS total_leitos,
            COUNT(DISTINCT CASE
                WHEN a.cd_atendimento IS NOT NULL THEN l.cd_leito
            END) AS leitos_ocupados
        FROM leitos l
        LEFT JOIN dbamv.atendime a
          ON a.cd_leito = l.cd_leito
         AND a.tp_atendimento = 'I'
         AND TRUNC(a.dt_atendimento) <= TRUNC(SYSDATE)
         AND NVL(TRUNC(a.dt_alta), TRUNC(SYSDATE) + 1) > TRUNC(SYSDATE)
         AND (:cd_convenio IS NULL OR INSTR(',' || :cd_convenio || ',', ',' || TO_CHAR(a.cd_convenio) || ',') > 0)
    )
    SELECT
        (SELECT NVL(SUM(valor_item), 0)
           FROM (
                SELECT
                    CASE
                        WHEN NVL(irf.sn_pertence_pacote, 'N') = 'S' THEN 0
                        ELSE NVL(irf.vl_total_conta, 0)
                    END AS valor_item
                FROM dbamv.reg_fat rf
                JOIN dbamv.itreg_fat irf
                  ON irf.cd_reg_fat = rf.cd_reg_fat
                WHERE irf.dt_lancamento >= :data_inicio_receita
                  AND irf.dt_lancamento <  :data_fim_exclusiva
                  AND (:cd_convenio IS NULL OR INSTR(',' || :cd_convenio || ',', ',' || TO_CHAR(rf.cd_convenio) || ',') > 0)
                UNION ALL
                SELECT
                    CASE
                        WHEN NVL(ira.sn_pertence_pacote, 'N') = 'S' THEN 0
                        ELSE NVL(ira.vl_total_conta, 0)
                    END AS valor_item
                FROM dbamv.reg_amb ra
                JOIN dbamv.itreg_amb ira
                  ON ira.cd_reg_amb = ra.cd_reg_amb
                WHERE ra.dt_lancamento >= :data_inicio_receita
                  AND ra.dt_lancamento <  :data_fim_exclusiva
                  AND (:cd_convenio IS NULL OR INSTR(',' || :cd_convenio || ',', ',' || TO_CHAR(NVL(ira.cd_convenio, ra.cd_convenio)) || ',') > 0)
           )
        ) AS receita_periodo,
        (SELECT COUNT(*)
          FROM dbamv.atendime a
          WHERE a.dt_atendimento >= :data_inicio
            AND a.dt_atendimento <  :data_fim_exclusiva
            AND (:cd_convenio IS NULL OR INSTR(',' || :cd_convenio || ',', ',' || TO_CHAR(a.cd_convenio) || ',') > 0)) AS atendimentos,
        (SELECT COUNT(*)
           FROM dbamv.atendime a
          WHERE a.tp_atendimento = 'I'
            AND a.dt_atendimento >= :data_inicio
            AND a.dt_atendimento <  :data_fim_exclusiva
            AND (:cd_convenio IS NULL OR INSTR(',' || :cd_convenio || ',', ',' || TO_CHAR(a.cd_convenio) || ',') > 0)) AS internacoes,
        (SELECT COUNT(*)
           FROM dbamv.atendime a
          WHERE a.tp_atendimento = 'A'
            AND a.dt_atendimento >= :data_inicio
            AND a.dt_atendimento <  :data_fim_exclusiva
            AND (:cd_convenio IS NULL OR INSTR(',' || :cd_convenio || ',', ',' || TO_CHAR(a.cd_convenio) || ',') > 0)) AS consultas_ambulatoriais,
        (SELECT COUNT(*)
           FROM dbamv.atendime a
          WHERE a.tp_atendimento = 'U'
            AND a.dt_atendimento >= :data_inicio
            AND a.dt_atendimento <  :data_fim_exclusiva
            AND (:cd_convenio IS NULL OR INSTR(',' || :cd_convenio || ',', ',' || TO_CHAR(a.cd_convenio) || ',') > 0)) AS consultas_emergencia,
        (SELECT COUNT(*)
           FROM (
                SELECT il.cd_itped_lab AS cd_item_exame
                  FROM dbamv.ped_lab pl
                  JOIN dbamv.itped_lab il
                    ON il.cd_ped_lab = pl.cd_ped_lab
                  LEFT JOIN dbamv.atendime a
                    ON a.cd_atendimento = pl.cd_atendimento
                 WHERE pl.cd_atendimento IS NOT NULL
                   AND NVL(pl.hr_ped_lab, pl.dt_pedido) >= :data_inicio
                   AND NVL(pl.hr_ped_lab, pl.dt_pedido) <  :data_fim_exclusiva
                   AND (:cd_convenio IS NULL OR INSTR(',' || :cd_convenio || ',', ',' || TO_CHAR(NVL(pl.cd_convenio, a.cd_convenio)) || ',') > 0)
                UNION ALL
                SELECT irx.cd_itped_rx AS cd_item_exame
                  FROM dbamv.ped_rx prx
                  JOIN dbamv.itped_rx irx
                    ON irx.cd_ped_rx = prx.cd_ped_rx
                  LEFT JOIN dbamv.atendime a
                    ON a.cd_atendimento = prx.cd_atendimento
                 WHERE prx.cd_atendimento IS NOT NULL
                   AND NVL(prx.hr_pedido, prx.dt_pedido) >= :data_inicio
                   AND NVL(prx.hr_pedido, prx.dt_pedido) <  :data_fim_exclusiva
                   AND (:cd_convenio IS NULL OR INSTR(',' || :cd_convenio || ',', ',' || TO_CHAR(NVL(prx.cd_convenio, a.cd_convenio)) || ',') > 0)
           )) AS exames_totais,
        (SELECT COUNT(*)
           FROM dbamv.ped_rx prx
           JOIN dbamv.itped_rx irx
             ON irx.cd_ped_rx = prx.cd_ped_rx
           JOIN dbamv.exa_rx erx
             ON erx.cd_exa_rx = irx.cd_exa_rx
           LEFT JOIN dbamv.atendime a
             ON a.cd_atendimento = prx.cd_atendimento
          WHERE prx.cd_atendimento IS NOT NULL
            AND NVL(prx.hr_pedido, prx.dt_pedido) >= :data_inicio
            AND NVL(prx.hr_pedido, prx.dt_pedido) <  :data_fim_exclusiva
            AND (
                irx.cd_exa_rx = 14375
                OR UPPER(erx.ds_exa_rx) LIKE '%ANGIOTOMOGRAFIA CORONARIANA%'
            )
            AND (:cd_convenio IS NULL OR INSTR(',' || :cd_convenio || ',', ',' || TO_CHAR(NVL(prx.cd_convenio, a.cd_convenio)) || ',') > 0)) AS exames_angiotc,
        (SELECT COUNT(*)
           FROM dbamv.atendime a
          WHERE a.tp_atendimento = 'I'
            AND a.dt_atendimento >= TRUNC(SYSDATE) - 6
            AND a.dt_atendimento <  TRUNC(SYSDATE) + 1
            AND (:cd_convenio IS NULL OR INSTR(',' || :cd_convenio || ',', ',' || TO_CHAR(a.cd_convenio) || ',') > 0)) AS internacoes_7d,
        (SELECT ROUND(
                    leitos_ocupados / NULLIF(total_leitos, 0),
                    4
                )
           FROM ocupacao) AS taxa_ocupacao_atual,
        (SELECT ROUND(
                    AVG(TRUNC(a.dt_alta) - TRUNC(a.dt_atendimento)),
                    2
                )
           FROM dbamv.atendime a
          WHERE a.tp_atendimento = 'I'
            AND a.dt_alta IS NOT NULL
            AND a.dt_alta >= :data_inicio
            AND a.dt_alta <  :data_fim_exclusiva
            AND (:cd_convenio IS NULL OR INSTR(',' || :cd_convenio || ',', ',' || TO_CHAR(a.cd_convenio) || ',') > 0)) AS permanencia_media,
        (SELECT COUNT(*)
          FROM dbamv.atendime a
          WHERE a.tp_atendimento = 'I'
            AND a.dt_alta IS NULL
            AND (:cd_convenio IS NULL OR INSTR(',' || :cd_convenio || ',', ',' || TO_CHAR(a.cd_convenio) || ',') > 0)
        ) AS internados_atual,
        (SELECT COUNT(*)
           FROM dbamv.atendime a
          WHERE a.tp_atendimento = 'I'
            AND a.dt_alta IS NULL
            AND TRUNC(SYSDATE) - TRUNC(a.dt_atendimento) > 5
            AND (:cd_convenio IS NULL OR INSTR(',' || :cd_convenio || ',', ',' || TO_CHAR(a.cd_convenio) || ',') > 0)
        ) AS internados_mais_5_dias,
        (SELECT NVL(SUM(
                    GREATEST(
                        LEAST(NVL(TRUNC(a.dt_alta) + 1, :data_fim_exclusiva), :data_fim_exclusiva) -
                        GREATEST(TRUNC(a.dt_atendimento), :data_inicio),
                        0
                    )
                ), 0)
           FROM dbamv.atendime a
          WHERE a.tp_atendimento = 'I'
            AND a.dt_atendimento < :data_fim_exclusiva
            AND NVL(a.dt_alta, :data_fim_exclusiva) >= :data_inicio
            AND (:cd_convenio IS NULL OR INSTR(',' || :cd_convenio || ',', ',' || TO_CHAR(a.cd_convenio) || ',') > 0)
        ) AS paciente_dia_periodo
    FROM dual
    """
)


CONSULTA_INDICADORES_HOSPITALARES_SERIES = text(
    """
    WITH dias AS (
        SELECT :data_inicio + LEVEL - 1 AS dt_ref
        FROM dual
        CONNECT BY :data_inicio + LEVEL - 1 < :data_fim_exclusiva
    ),
    agenda AS (
        SELECT
            TRUNC(i.hr_agenda) AS dt_ref,
            COUNT(*) AS agendamentos,
            SUM(CASE WHEN i.cd_atendimento IS NOT NULL THEN 1 ELSE 0 END)
                AS atendidos
        FROM dbamv.it_agenda_central i
        WHERE i.hr_agenda >= :data_inicio
          AND i.hr_agenda <  :data_fim_exclusiva
          AND NVL(i.sn_bloqueado, 'N') = 'N'
          AND (:cd_convenio IS NULL OR INSTR(',' || :cd_convenio || ',', ',' || TO_CHAR(i.cd_convenio) || ',') > 0)
        GROUP BY TRUNC(i.hr_agenda)
    ),
    internacao AS (
        SELECT
            TRUNC(a.dt_atendimento) AS dt_ref,
            COUNT(*) AS internacoes
        FROM dbamv.atendime a
        WHERE a.tp_atendimento = 'I'
          AND a.dt_atendimento >= :data_inicio
          AND a.dt_atendimento <  :data_fim_exclusiva
          AND (:cd_convenio IS NULL OR INSTR(',' || :cd_convenio || ',', ',' || TO_CHAR(a.cd_convenio) || ',') > 0)
        GROUP BY TRUNC(a.dt_atendimento)
    ),
    altas AS (
        SELECT
            TRUNC(a.dt_alta) AS dt_ref,
            COUNT(*) AS altas
        FROM dbamv.atendime a
        WHERE a.tp_atendimento = 'I'
          AND a.dt_alta >= :data_inicio
          AND a.dt_alta <  :data_fim_exclusiva
          AND (:cd_convenio IS NULL OR INSTR(',' || :cd_convenio || ',', ',' || TO_CHAR(a.cd_convenio) || ',') > 0)
        GROUP BY TRUNC(a.dt_alta)
    ),
    receita AS (
        SELECT
            dt_ref,
            SUM(valor_item) AS receita
        FROM (
            SELECT
                TRUNC(irf.dt_lancamento) AS dt_ref,
                CASE
                    WHEN NVL(irf.sn_pertence_pacote, 'N') = 'S' THEN 0
                    ELSE NVL(irf.vl_total_conta, 0)
                END AS valor_item
            FROM dbamv.reg_fat rf
            JOIN dbamv.itreg_fat irf
              ON irf.cd_reg_fat = rf.cd_reg_fat
            WHERE irf.dt_lancamento >= :data_inicio_receita
              AND irf.dt_lancamento <  :data_fim_exclusiva
              AND (:cd_convenio IS NULL OR INSTR(',' || :cd_convenio || ',', ',' || TO_CHAR(rf.cd_convenio) || ',') > 0)
            UNION ALL
            SELECT
                TRUNC(ra.dt_lancamento) AS dt_ref,
                CASE
                    WHEN NVL(ira.sn_pertence_pacote, 'N') = 'S' THEN 0
                    ELSE NVL(ira.vl_total_conta, 0)
                END AS valor_item
            FROM dbamv.reg_amb ra
            JOIN dbamv.itreg_amb ira
              ON ira.cd_reg_amb = ra.cd_reg_amb
            WHERE ra.dt_lancamento >= :data_inicio_receita
              AND ra.dt_lancamento <  :data_fim_exclusiva
              AND (:cd_convenio IS NULL OR INSTR(',' || :cd_convenio || ',', ',' || TO_CHAR(NVL(ira.cd_convenio, ra.cd_convenio)) || ',') > 0)
        )
        GROUP BY dt_ref
    )
    SELECT
        d.dt_ref,
        NVL(a.agendamentos, 0) AS agendamentos,
        NVL(a.atendidos, 0) AS atendidos,
        NVL(i.internacoes, 0) AS internacoes,
        NVL(al.altas, 0) AS altas,
        NVL(r.receita, 0) AS receita
    FROM dias d
    LEFT JOIN agenda a
      ON a.dt_ref = d.dt_ref
    LEFT JOIN internacao i
      ON i.dt_ref = d.dt_ref
    LEFT JOIN altas al
      ON al.dt_ref = d.dt_ref
    LEFT JOIN receita r
      ON r.dt_ref = d.dt_ref
    ORDER BY d.dt_ref
    """
)


CONSULTA_AGENDA_AMBULATORIAL = text(
    """
    WITH itens AS (
        SELECT
            i.cd_agenda_central,
            COUNT(*) AS itens_agenda_registrados,
            SUM(CASE WHEN i.cd_paciente IS NOT NULL THEN 1 ELSE 0 END)
                AS agendamentos_marcados,
            SUM(CASE WHEN i.cd_atendimento IS NOT NULL THEN 1 ELSE 0 END)
                AS agendamentos_com_atendimento,
            SUM(CASE
                WHEN UPPER(NVL(i.ds_observacao, '') || ' ' ||
                           NVL(i.ds_observacao_geral, '')) LIKE '%CONFIRMADO%'
                    THEN 1
                ELSE 0
            END) AS agendamentos_confirmados
            ,
            SUM(CASE
                WHEN UPPER(NVL(i.ds_observacao, '') || ' ' ||
                           NVL(i.ds_observacao_geral, '')) LIKE '%CONFIRMADO%'
                 AND i.cd_atendimento IS NULL
                    THEN 1
                ELSE 0
            END) AS confirmados_sem_atendimento
        FROM dbamv.it_agenda_central i
        WHERE i.hr_agenda >= :data_inicio
          AND i.hr_agenda <  :data_fim_exclusiva
          AND NVL(i.sn_bloqueado, 'N') = 'N'
          AND (:cd_convenio IS NULL OR INSTR(',' || :cd_convenio || ',', ',' || TO_CHAR(i.cd_convenio) || ',') > 0)
        GROUP BY i.cd_agenda_central
    )
    SELECT
        TRUNC(ac.dt_agenda) AS dt_agenda,
        CASE
            WHEN TRUNC(ac.dt_agenda) - TRUNC(ac.dt_agenda, 'IW')
                 BETWEEN 0 AND 4
                THEN 'SEMANA'
            ELSE 'FINAL_SEMANA'
        END AS tipo_dia,
        ac.cd_prestador,
        pr.nm_prestador,
        ac.cd_setor,
        s.nm_setor,
        ac.cd_recurso_central,
        rc.ds_recurso_central,
        CASE
            WHEN TO_NUMBER(TO_CHAR(ac.hr_inicio, 'HH24')) < 12
                THEN 'MANHA'
            WHEN TO_NUMBER(TO_CHAR(ac.hr_inicio, 'HH24')) < 18
                THEN 'TARDE'
            ELSE 'NOITE'
        END AS turno,
        TO_CHAR(ac.hr_inicio, 'HH24:MI') AS hora_inicio_turno,
        TO_CHAR(ac.hr_fim, 'HH24:MI') AS hora_fim_turno,
        ac.qt_atendimento AS capacidade_agenda,
        NVL(it.agendamentos_marcados, 0) AS agendamentos_marcados,
        NVL(it.itens_agenda_registrados, 0) AS itens_agenda_registrados,
        NVL(it.agendamentos_com_atendimento, 0)
            AS agendamentos_com_atendimento,
        NVL(it.agendamentos_confirmados, 0) AS agendamentos_confirmados,
        NVL(it.confirmados_sem_atendimento, 0) AS confirmados_sem_atendimento,
        GREATEST(
            NVL(it.agendamentos_marcados, 0) -
            NVL(it.agendamentos_com_atendimento, 0),
            0
        ) AS marcados_sem_atendimento,
        GREATEST(
            NVL(ac.qt_atendimento, 0) - NVL(it.agendamentos_marcados, 0),
            0
        ) AS vagas_nao_marcadas,
        ROUND(
            NVL(it.agendamentos_marcados, 0) /
                NULLIF(ac.qt_atendimento, 0),
            4
        ) AS perc_agenda_marcada,
        ROUND(
            NVL(it.agendamentos_com_atendimento, 0) /
                NULLIF(it.agendamentos_marcados, 0),
            4
        ) AS perc_comparecimento_marcados,
        ROUND(
            GREATEST(
                NVL(it.agendamentos_marcados, 0) -
                NVL(it.agendamentos_com_atendimento, 0),
                0
            ) / NULLIF(it.agendamentos_marcados, 0),
            4
        ) AS perc_absenteismo_marcados,
        ROUND(
            NVL(it.confirmados_sem_atendimento, 0) /
                NULLIF(it.agendamentos_confirmados, 0),
            4
        ) AS perc_absenteismo_confirmados
    FROM dbamv.agenda_central ac
    LEFT JOIN itens it
      ON it.cd_agenda_central = ac.cd_agenda_central
    LEFT JOIN dbamv.prestador pr
      ON pr.cd_prestador = ac.cd_prestador
    LEFT JOIN dbamv.setor s
      ON s.cd_setor = ac.cd_setor
    LEFT JOIN dbamv.recurso_central rc
      ON rc.cd_recurso_central = ac.cd_recurso_central
    WHERE ac.tp_agenda = 'A'
      AND NVL(ac.sn_ativo, 'S') = 'S'
      AND ac.dt_agenda >= :data_inicio
      AND ac.dt_agenda <  :data_fim_exclusiva
      AND (:cd_convenio IS NULL OR it.cd_agenda_central IS NOT NULL)
    ORDER BY dt_agenda, nm_prestador, turno
    """
)


CONSULTA_FLUXO_AMBULATORIO_EXAMES = text(
    """
    WITH eventos AS (
        SELECT
            stp.cd_atendimento,
            stp.cd_triagem_atendimento,
            MIN(CASE WHEN stp.cd_tipo_tempo_processo = 1  THEN stp.dh_processo END) AS dh_totem,
            MIN(CASE WHEN stp.cd_tipo_tempo_processo = 20 THEN stp.dh_processo END) AS dh_cha_atd_adm,
            MIN(CASE WHEN stp.cd_tipo_tempo_processo = 21 THEN stp.dh_processo END) AS dh_cadastro_ini,
            MIN(CASE WHEN stp.cd_tipo_tempo_processo = 22 THEN stp.dh_processo END) AS dh_cadastro_fim,
            MIN(CASE WHEN stp.cd_tipo_tempo_processo = 30 THEN stp.dh_processo END) AS dh_cham_atd_med,
            MIN(CASE WHEN stp.cd_tipo_tempo_processo = 31 THEN stp.dh_processo END) AS dh_consulta_ini,
            MIN(CASE WHEN stp.cd_tipo_tempo_processo = 32 THEN stp.dh_processo END) AS dh_consulta_fim,
            MIN(CASE WHEN stp.cd_tipo_tempo_processo = 90 THEN stp.dh_processo END) AS dh_alta
        FROM dbamv.sacr_tempo_processo stp
        WHERE stp.dh_processo >= :data_inicio
          AND stp.dh_processo <  :data_fim_exclusiva
          AND stp.cd_atendimento IS NOT NULL
        GROUP BY
            stp.cd_atendimento,
            stp.cd_triagem_atendimento
    ),
    base AS (
        SELECT
            e.cd_atendimento,
            NVL(ta.cd_paciente, a.cd_paciente) AS cd_paciente,
            NVL(ta.nm_paciente, p.nm_paciente) AS nm_paciente,
            ta.ds_senha,
            a.tp_atendimento,
            CASE a.tp_atendimento
                WHEN 'A' THEN 'AMBULATORIO'
                WHEN 'E' THEN 'EXAMES'
                ELSE a.tp_atendimento
            END AS tipo_fluxo,
            a.cd_convenio,
            c.nm_convenio,
            pr.nm_prestador,
            oa.ds_ori_ate,
            e.dh_totem,
            e.dh_cha_atd_adm,
            e.dh_cadastro_ini,
            e.dh_cadastro_fim,
            e.dh_cham_atd_med,
            e.dh_consulta_ini,
            e.dh_consulta_fim,
            e.dh_alta,
            ROUND((NVL(e.dh_cadastro_ini, e.dh_cha_atd_adm) - e.dh_totem) * 1440, 2)
                AS min_senha_ate_guiche,
            ROUND((NVL(e.dh_consulta_ini, e.dh_cham_atd_med) - NVL(e.dh_cadastro_fim, e.dh_cadastro_ini)) * 1440, 2)
                AS min_guiche_ate_medico,
            ROUND((e.dh_alta - NVL(e.dh_consulta_fim, e.dh_consulta_ini)) * 1440, 2)
                AS min_medico_ate_alta,
            ROUND((NVL(e.dh_alta, e.dh_consulta_fim) - e.dh_totem) * 1440, 2)
                AS min_total_fluxo
        FROM eventos e
        LEFT JOIN dbamv.triagem_atendimento ta
          ON ta.cd_triagem_atendimento = e.cd_triagem_atendimento
        LEFT JOIN dbamv.atendime a
          ON a.cd_atendimento = e.cd_atendimento
        LEFT JOIN dbamv.paciente p
          ON p.cd_paciente = a.cd_paciente
        LEFT JOIN dbamv.convenio c
          ON c.cd_convenio = a.cd_convenio
        LEFT JOIN dbamv.prestador pr
          ON pr.cd_prestador = a.cd_prestador
        LEFT JOIN dbamv.ori_ate oa
          ON oa.cd_ori_ate = a.cd_ori_ate
        WHERE a.tp_atendimento IN ('A', 'E')
          AND (:cd_convenio IS NULL OR INSTR(',' || :cd_convenio || ',', ',' || TO_CHAR(a.cd_convenio) || ',') > 0)
    )
    SELECT *
    FROM (
        SELECT *
        FROM base
        WHERE dh_totem IS NOT NULL
           OR dh_cha_atd_adm IS NOT NULL
           OR dh_cham_atd_med IS NOT NULL
        ORDER BY NVL(dh_totem, NVL(dh_cha_atd_adm, dh_cham_atd_med)) DESC
    )
    WHERE ROWNUM <= :limite
    """
)


CONSULTA_EXAMES_PROCEDIMENTOS = text(
    """
    WITH exames AS (
        SELECT
            'LABORATORIO' AS tipo_exame_real,
            pl.cd_atendimento,
            a.cd_paciente,
            p.nm_paciente,
            NVL(pl.cd_convenio, a.cd_convenio) AS cd_convenio,
            c.nm_convenio,
            NVL(pl.cd_prestador, a.cd_prestador) AS cd_prestador,
            pr.nm_prestador,
            a.cd_ori_ate,
            oa.ds_ori_ate,
            pl.cd_ped_lab AS cd_pedido_exame,
            il.cd_itped_lab AS cd_item_exame,
            il.cd_exa_lab AS cd_exame,
            el.nm_exa_lab AS procedimento_exame,
            el.nm_mnemonico AS mnemonico_exame,
            el.cd_pro_fat,
            NVL(pl.hr_ped_lab, pl.dt_pedido) AS dh_exame
        FROM dbamv.ped_lab pl
        JOIN dbamv.itped_lab il
          ON il.cd_ped_lab = pl.cd_ped_lab
        JOIN dbamv.exa_lab el
          ON el.cd_exa_lab = il.cd_exa_lab
        LEFT JOIN dbamv.atendime a
          ON a.cd_atendimento = pl.cd_atendimento
        LEFT JOIN dbamv.paciente p
          ON p.cd_paciente = a.cd_paciente
        LEFT JOIN dbamv.convenio c
          ON c.cd_convenio = NVL(pl.cd_convenio, a.cd_convenio)
        LEFT JOIN dbamv.prestador pr
          ON pr.cd_prestador = NVL(pl.cd_prestador, a.cd_prestador)
        LEFT JOIN dbamv.ori_ate oa
          ON oa.cd_ori_ate = a.cd_ori_ate
        WHERE pl.cd_atendimento IS NOT NULL
          AND NVL(pl.hr_ped_lab, pl.dt_pedido) >= :data_inicio
          AND NVL(pl.hr_ped_lab, pl.dt_pedido) <  :data_fim_exclusiva
          AND (:cd_convenio IS NULL OR INSTR(',' || :cd_convenio || ',', ',' || TO_CHAR(NVL(pl.cd_convenio, a.cd_convenio)) || ',') > 0)

        UNION ALL

        SELECT
            'IMAGEM' AS tipo_exame_real,
            prx.cd_atendimento,
            a.cd_paciente,
            p.nm_paciente,
            NVL(prx.cd_convenio, a.cd_convenio) AS cd_convenio,
            c.nm_convenio,
            NVL(prx.cd_prestador, a.cd_prestador) AS cd_prestador,
            pr.nm_prestador,
            a.cd_ori_ate,
            oa.ds_ori_ate,
            prx.cd_ped_rx AS cd_pedido_exame,
            irx.cd_itped_rx AS cd_item_exame,
            irx.cd_exa_rx AS cd_exame,
            erx.ds_exa_rx AS procedimento_exame,
            erx.nm_mnemonico AS mnemonico_exame,
            erx.exa_rx_cd_pro_fat AS cd_pro_fat,
            NVL(prx.hr_pedido, prx.dt_pedido) AS dh_exame
        FROM dbamv.ped_rx prx
        JOIN dbamv.itped_rx irx
          ON irx.cd_ped_rx = prx.cd_ped_rx
        JOIN dbamv.exa_rx erx
          ON erx.cd_exa_rx = irx.cd_exa_rx
        LEFT JOIN dbamv.atendime a
          ON a.cd_atendimento = prx.cd_atendimento
        LEFT JOIN dbamv.paciente p
          ON p.cd_paciente = a.cd_paciente
        LEFT JOIN dbamv.convenio c
          ON c.cd_convenio = NVL(prx.cd_convenio, a.cd_convenio)
        LEFT JOIN dbamv.prestador pr
          ON pr.cd_prestador = NVL(prx.cd_prestador, a.cd_prestador)
        LEFT JOIN dbamv.ori_ate oa
          ON oa.cd_ori_ate = a.cd_ori_ate
        WHERE prx.cd_atendimento IS NOT NULL
          AND NVL(prx.hr_pedido, prx.dt_pedido) >= :data_inicio
          AND NVL(prx.hr_pedido, prx.dt_pedido) <  :data_fim_exclusiva
          AND (:cd_convenio IS NULL OR INSTR(',' || :cd_convenio || ',', ',' || TO_CHAR(NVL(prx.cd_convenio, a.cd_convenio)) || ',') > 0)
    )
    SELECT *
    FROM (
        SELECT *
        FROM exames
        ORDER BY dh_exame DESC, procedimento_exame
    )
    WHERE ROWNUM <= :limite
    """
)


CONSULTA_INTERNACOES_DETALHADAS = text(
    """
    SELECT
        a.cd_atendimento,
        a.cd_paciente,
        p.nm_paciente,
        a.cd_prestador,
        pr.nm_prestador,
        TRUNC(a.dt_atendimento) +
            (NVL(a.hr_atendimento, a.dt_atendimento) -
             TRUNC(NVL(a.hr_atendimento, a.dt_atendimento)))
            AS dh_atendimento,
        CASE
            WHEN a.hr_alta IS NOT NULL THEN
                TRUNC(a.dt_alta) + (a.hr_alta - TRUNC(a.hr_alta))
            ELSE a.dt_alta
        END AS dh_alta,
        a.cd_convenio,
        c.nm_convenio,
        a.cd_ori_ate,
        oa.ds_ori_ate,
        CASE
            WHEN TRUNC(a.dt_atendimento) -
                 TRUNC(a.dt_atendimento, 'IW') BETWEEN 0 AND 4
                THEN 'SEMANA'
            ELSE 'FINAL_SEMANA'
        END AS tipo_dia
    FROM dbamv.atendime a
    LEFT JOIN dbamv.paciente p
      ON p.cd_paciente = a.cd_paciente
    LEFT JOIN dbamv.prestador pr
      ON pr.cd_prestador = a.cd_prestador
    LEFT JOIN dbamv.convenio c
      ON c.cd_convenio = a.cd_convenio
    LEFT JOIN dbamv.ori_ate oa
      ON oa.cd_ori_ate = a.cd_ori_ate
    WHERE a.tp_atendimento = 'I'
      AND pr.cd_tip_presta = 8
      AND (
            TRUNC(a.dt_atendimento) +
            (NVL(a.hr_atendimento, a.dt_atendimento) -
             TRUNC(NVL(a.hr_atendimento, a.dt_atendimento)))
          ) >= :data_inicio
      AND (
            TRUNC(a.dt_atendimento) +
            (NVL(a.hr_atendimento, a.dt_atendimento) -
             TRUNC(NVL(a.hr_atendimento, a.dt_atendimento)))
          ) < :data_fim_exclusiva
      AND (:cd_convenio IS NULL OR INSTR(',' || :cd_convenio || ',', ',' || TO_CHAR(a.cd_convenio) || ',') > 0)
    ORDER BY dh_atendimento DESC, pr.nm_prestador, p.nm_paciente
    """
)


CONSULTA_INTERNACOES_ATIVAS_REDE = text(
    """
    WITH ult_mov AS (
        SELECT cd_atendimento, cd_leito
        FROM (
            SELECT
                mi.cd_atendimento,
                mi.cd_leito,
                ROW_NUMBER() OVER (
                    PARTITION BY mi.cd_atendimento
                    ORDER BY NVL(mi.hr_mov_int, mi.dt_mov_int) DESC,
                             mi.cd_mov_int DESC
                ) AS rn
            FROM dbamv.mov_int mi
            WHERE mi.cd_leito IS NOT NULL
        )
        WHERE rn = 1
    )
    SELECT
        a.cd_atendimento,
        a.cd_paciente,
        p.nm_paciente,
        a.cd_prestador,
        pr.nm_prestador,
        TRUNC(a.dt_atendimento) +
            (NVL(a.hr_atendimento, a.dt_atendimento) -
             TRUNC(NVL(a.hr_atendimento, a.dt_atendimento)))
            AS dh_atendimento,
        a.cd_convenio,
        c.nm_convenio,
        a.cd_ori_ate,
        oa.ds_ori_ate,
        NVL(
            lei.ds_leito,
            TO_CHAR(COALESCE(a.cd_leito, um.cd_leito))
        ) AS leito,
        ui.ds_unid_int AS unidade_internacao,
        sti.nm_setor AS setor_internacao
    FROM dbamv.atendime a
    LEFT JOIN dbamv.paciente p ON p.cd_paciente = a.cd_paciente
    LEFT JOIN dbamv.prestador pr ON pr.cd_prestador = a.cd_prestador
    LEFT JOIN dbamv.convenio c ON c.cd_convenio = a.cd_convenio
    LEFT JOIN dbamv.ori_ate oa ON oa.cd_ori_ate = a.cd_ori_ate
    LEFT JOIN ult_mov um ON um.cd_atendimento = a.cd_atendimento
    LEFT JOIN dbamv.leito lei ON lei.cd_leito = COALESCE(a.cd_leito, um.cd_leito)
    LEFT JOIN dbamv.unid_int ui ON ui.cd_unid_int = lei.cd_unid_int
    LEFT JOIN dbamv.setor sti ON sti.cd_setor = ui.cd_setor
    WHERE a.tp_atendimento = 'I'
      AND a.dt_alta IS NULL
      AND (
            (
                :origens IS NOT NULL
                AND INSTR(
                    ',' || :origens || ',',
                    ',' || TO_CHAR(a.cd_ori_ate) || ','
                ) > 0
            )
         OR (
                :pacientes IS NOT NULL
                AND INSTR(
                    ',' || :pacientes || ',',
                    ',' || TO_CHAR(a.cd_paciente) || ','
                ) > 0
            )
      )
    ORDER BY dh_atendimento DESC, p.nm_paciente
    """
)


CONSULTA_CONTEXTO_MEDICO_INTERNACAO_REDE = text(
    """
    WITH ult_mov AS (
        SELECT cd_atendimento, cd_leito
        FROM (
            SELECT
                mi.cd_atendimento,
                mi.cd_leito,
                ROW_NUMBER() OVER (
                    PARTITION BY mi.cd_atendimento
                    ORDER BY NVL(mi.hr_mov_int, mi.dt_mov_int) DESC,
                             mi.cd_mov_int DESC
                ) AS rn
            FROM dbamv.mov_int mi
            WHERE mi.cd_leito IS NOT NULL
        )
        WHERE rn = 1
    )
    SELECT
        a.cd_atendimento,
        a.cd_paciente,
        pac.nm_paciente,
        TRUNC(a.dt_atendimento) +
            (NVL(a.hr_atendimento, a.dt_atendimento) -
             TRUNC(NVL(a.hr_atendimento, a.dt_atendimento)))
            AS dh_atendimento,
        NVL(
            lei.ds_leito,
            NVL(TO_CHAR(COALESCE(a.cd_leito, um.cd_leito)), 'NAO INFORMADO')
        ) AS leito,
        NVL(ui.ds_unid_int, 'NAO INFORMADO') AS unidade_internacao,
        NVL(sti.nm_setor, 'NAO INFORMADO') AS setor_internacao,
        u.cd_usuario AS usuario_mv,
        pmv.cd_prestador,
        pmv.nm_prestador,
        co.ds_conselho,
        pmv.ds_codigo_conselho,
        pmv.cd_uf_orgao_emissor
    FROM dbamv.atendime a
    JOIN dbamv.paciente pac ON pac.cd_paciente = a.cd_paciente
    JOIN dbasgu.usuarios u ON u.cd_usuario = :usuario_mv
    JOIN dbamv.prestador pmv ON pmv.cd_prestador = u.cd_prestador
    LEFT JOIN dbamv.conselho co ON co.cd_conselho = pmv.cd_conselho
    LEFT JOIN ult_mov um ON um.cd_atendimento = a.cd_atendimento
    LEFT JOIN dbamv.leito lei
      ON lei.cd_leito = COALESCE(a.cd_leito, um.cd_leito)
    LEFT JOIN dbamv.unid_int ui ON ui.cd_unid_int = lei.cd_unid_int
    LEFT JOIN dbamv.setor sti ON sti.cd_setor = ui.cd_setor
    WHERE a.cd_atendimento = :cd_atendimento
      AND a.tp_atendimento = 'I'
      AND a.dt_alta IS NULL
      AND NVL(pmv.tp_situacao, 'A') = 'A'
    """
)

CONSULTA_CATALOGO_ASSISTENCIAL_INTERNACAO_REDE = text(
    """
    SELECT id, label, tipo
    FROM (
        SELECT DISTINCT
            'TIP:' || TO_CHAR(tp.cd_tip_presc) AS id,
            TRIM(tp.ds_tip_presc) AS label,
            CASE
                WHEN tp.cd_exa_rx IS NOT NULL
                  OR tp.cd_exa_lab IS NOT NULL
                    THEN 'EXAME'
                ELSE 'PROCEDIMENTO'
            END AS tipo
        FROM dbamv.pre_med pm
        JOIN dbamv.itpre_med ipm
          ON ipm.cd_pre_med = pm.cd_pre_med
        JOIN dbamv.tip_presc tp
          ON tp.cd_tip_presc = ipm.cd_tip_presc
        WHERE pm.cd_atendimento = :cd_atendimento
          AND NVL(ipm.sn_cancelado, 'N') = 'N'
          AND TRIM(tp.ds_tip_presc) IS NOT NULL
          AND (
                tp.sn_solicitacao = 'S'
             OR tp.cd_exa_rx IS NOT NULL
             OR tp.cd_exa_lab IS NOT NULL
          )
        ORDER BY label, id
    )
    WHERE ROWNUM <= 100
    """
)

CONSULTA_FLUXO_PA_TEMPOS = text(
    """
    WITH eventos AS (
        SELECT
            stp.cd_atendimento,
            stp.cd_triagem_atendimento,
            MIN(CASE WHEN stp.cd_tipo_tempo_processo = 1  THEN stp.dh_processo END) AS dh_totem,
            MIN(CASE WHEN stp.cd_tipo_tempo_processo = 20 THEN stp.dh_processo END) AS dh_cha_atd_adm,
            MIN(CASE WHEN stp.cd_tipo_tempo_processo = 21 THEN stp.dh_processo END) AS dh_cadastro_ini,
            MIN(CASE WHEN stp.cd_tipo_tempo_processo = 22 THEN stp.dh_processo END) AS dh_cadastro_fim,
            MIN(CASE WHEN stp.cd_tipo_tempo_processo = 10 THEN stp.dh_processo END) AS dh_cha_class,
            MIN(CASE WHEN stp.cd_tipo_tempo_processo = 11 THEN stp.dh_processo END) AS dh_classificacao_ini,
            MIN(CASE WHEN stp.cd_tipo_tempo_processo = 12 THEN stp.dh_processo END) AS dh_classificacao_fim,
            MIN(CASE WHEN stp.cd_tipo_tempo_processo = 30 THEN stp.dh_processo END) AS dh_cham_atd_med,
            MIN(CASE WHEN stp.cd_tipo_tempo_processo = 31 THEN stp.dh_processo END) AS dh_consulta_ini,
            MIN(CASE WHEN stp.cd_tipo_tempo_processo = 32 THEN stp.dh_processo END) AS dh_consulta_fim,
            MIN(CASE WHEN stp.cd_tipo_tempo_processo = 90 THEN stp.dh_processo END) AS dh_alta
        FROM dbamv.sacr_tempo_processo stp
        WHERE stp.dh_processo >= :data_inicio
          AND stp.dh_processo <  :data_fim_exclusiva
          AND stp.cd_atendimento IS NOT NULL
        GROUP BY
            stp.cd_atendimento,
            stp.cd_triagem_atendimento
    ),
    base AS (
        SELECT
            e.cd_atendimento,
            NVL(ta.cd_paciente, a.cd_paciente) AS cd_paciente,
            NVL(ta.nm_paciente, p.nm_paciente) AS nm_paciente,
            ta.ds_senha,
            a.tp_atendimento,
            a.cd_convenio,
            sc.ds_tipo_risco,
            pr.nm_prestador,
            e.dh_totem,
            e.dh_cha_atd_adm,
            e.dh_cadastro_ini,
            e.dh_cadastro_fim,
            e.dh_cha_class,
            e.dh_classificacao_ini,
            e.dh_classificacao_fim,
            e.dh_cham_atd_med,
            e.dh_consulta_ini,
            e.dh_consulta_fim,
            e.dh_alta,
            ROUND((NVL(e.dh_cadastro_ini, e.dh_cha_atd_adm) - e.dh_totem) * 1440, 2)
                AS min_senha_ate_guiche,
            ROUND((NVL(e.dh_consulta_ini, e.dh_cham_atd_med) - NVL(e.dh_cadastro_fim, e.dh_cadastro_ini)) * 1440, 2)
                AS min_guiche_ate_medico,
            ROUND((e.dh_alta - NVL(e.dh_consulta_fim, e.dh_consulta_ini)) * 1440, 2)
                AS min_medico_ate_alta,
            ROUND((NVL(e.dh_alta, e.dh_consulta_fim) - e.dh_totem) * 1440, 2)
                AS min_total_pa
        FROM eventos e
        LEFT JOIN dbamv.triagem_atendimento ta
          ON ta.cd_triagem_atendimento = e.cd_triagem_atendimento
        LEFT JOIN dbamv.sacr_classificacao sc
          ON sc.cd_classificacao = ta.cd_classificacao
        LEFT JOIN dbamv.atendime a
          ON a.cd_atendimento = e.cd_atendimento
        LEFT JOIN dbamv.paciente p
          ON p.cd_paciente = a.cd_paciente
        LEFT JOIN dbamv.prestador pr
          ON pr.cd_prestador = a.cd_prestador
        WHERE :cd_convenio IS NULL OR INSTR(',' || :cd_convenio || ',', ',' || TO_CHAR(a.cd_convenio) || ',') > 0
    )
    SELECT *
    FROM (
        SELECT *
        FROM base
        WHERE dh_totem IS NOT NULL
           OR dh_cha_atd_adm IS NOT NULL
           OR dh_cham_atd_med IS NOT NULL
        ORDER BY NVL(dh_totem, NVL(dh_cha_atd_adm, dh_cham_atd_med)) DESC
    )
    WHERE ROWNUM <= :limite
    """
)


CONSULTA_CONVENIOS_INDICADORES = text(
    """
    SELECT
        cd_convenio,
        nm_convenio
    FROM (
        SELECT DISTINCT
            c.cd_convenio,
            c.nm_convenio
        FROM dbamv.atendime a
        JOIN dbamv.convenio c
          ON c.cd_convenio = a.cd_convenio
        WHERE a.dt_atendimento >= :data_inicio
          AND a.dt_atendimento <  :data_fim_exclusiva
        UNION
        SELECT DISTINCT
            c.cd_convenio,
            c.nm_convenio
        FROM dbamv.it_agenda_central i
        JOIN dbamv.convenio c
          ON c.cd_convenio = i.cd_convenio
        WHERE i.hr_agenda >= :data_inicio
          AND i.hr_agenda <  :data_fim_exclusiva
          AND NVL(i.sn_bloqueado, 'N') = 'N'
    )
    ORDER BY nm_convenio
    """
)


CONSULTA_PRODUCAO_CIRURGICA = text(
    """
    WITH base AS (
        SELECT
            TRUNC(ac.dt_aviso_cirurgia) AS dt_aviso_cirurgia,
            ca.cd_convenio,
            co.nm_convenio,
            ca.cd_cirurgia,
            c.ds_cirurgia,
            pr.cd_prestador,
            pr.nm_prestador,
            am.ds_ati_med,
            COUNT(pa.cd_aviso_cirurgia) AS qtd,
            CASE
                WHEN c.ds_cirurgia IN (
                    'ANGIOPLASTIA TRANSLUMINAL PERCUTANEA DE BIFURCACAO E DE TRONCO COM IMPLANTE DE STENT',
                    'ANGIOPLASTIA TRANSLUMINAL PERCUTANEA DE MULTIPLOS VASOS, COM IMPLANTE DE STENT',
                    'ANGIOPLASTIA TRANSLUMINAL PERCUTANEA POR BALAO (1 VASO)',
                    'CATETERISMO CARDIACO D E/OU E C/ ESTUDO CINEANGIOGRAFICO E DE REVASC. CIRURGICA DO MIOCARDIO',
                    'CATETERISMO CARDIACO E E/OU D COM CINEANGIOCORONARIOGRAFIA E VENTRICULOGRAFIA',
                    'IMPLANTE DE STENT CORONARIO DE VASO UNICO',
                    'RECANALIZACAO ARTERIAL NO IAM - ANGIOPLAST PRIMA - C/ OU S/ SUPORTE CIRCULATORIO (BALAO INTRAORTICO)',
                    'RECANALIZACAO MECANICA DO IAM (ANGIOPLASTIA PRIMARIA COM BALAO)',
                    'CATETERISMO CARDIACO D E/OU E COM  OU  SEM  CINECORONARIOGRAFIA / CINEANGIOGRAFIA C/ AVAL. DE REAT.',
                    'ANGIOPLASTIA CORONARIANA COM IMPLANTE DE STENT',
                    'ANGIOPLASTIA CORONARIANA C/ IMPLANTE DE DOIS STENTS'
                ) THEN 'HEMODINAMICA'
                WHEN c.ds_cirurgia IN (
                    'COLOCACAO DE PROTESE DE MAMA',
                    'IMPLANTE DE CATETER VENOSO CENTRAL P/ PUNCAO, P/ NPP, QT, HEMODEPURACAO OU P/ INFUSAO DE SORO/DROGA',
                    'LIPOASPIRACAO',
                    'MASTOPEXIA',
                    'PUNCAO LIQUORICA',
                    'REVASCULARIZACAO DO MIOCARDIO'
                ) THEN 'CENTRO_CIRURGICO'
                WHEN c.ds_cirurgia = 'INSTALACAO DE MARCA-PASSO EPIMIOCARDIO TEMPORARIO'
                    THEN 'ELETROFISIOLOGIA'
                WHEN c.ds_cirurgia IN (
                    'ANGIOGRAFIA POR CATETERISMO NAO SELETIVO DE GRANDE VASO',
                    'ANGIOGRAFIA POR CATETERISMO SELETIVO DE RAMO PRIMARIO - POR VASO',
                    'ANGIOGRAFIA POR CATETERISMO SUPERSELETIVO DE RAMO SECUNDARIO OU DISTAL - POR VASO',
                    'BLOQUEIO DE NERVO PERIFERICO - BLOQUEIOS ANESTESICOS DE NERVOS E ESTIMULOS NEUROVASCULARES',
                    'BLOQUEIO DO SISTEMA NERVOSO AUTONOMO',
                    'FISTULA ARTERIOVENOSA DOS MEMBROS'
                ) THEN 'VASCULAR'
                ELSE 'OUTROS'
            END AS area,
            CASE
                WHEN c.ds_cirurgia IN (
                    'ANGIOPLASTIA TRANSLUMINAL PERCUTANEA DE BIFURCACAO E DE TRONCO COM IMPLANTE DE STENT',
                    'ANGIOPLASTIA TRANSLUMINAL PERCUTANEA DE MULTIPLOS VASOS, COM IMPLANTE DE STENT',
                    'ANGIOPLASTIA TRANSLUMINAL PERCUTANEA POR BALAO (1 VASO)',
                    'IMPLANTE DE CATETER VENOSO CENTRAL P/ PUNCAO, P/ NPP, QT, HEMODEPURACAO OU P/ INFUSAO DE SORO/DROGA',
                    'IMPLANTE DE STENT CORONARIO DE VASO UNICO',
                    'ANGIOPLASTIA CORONARIANA COM IMPLANTE DE STENT',
                    'RECANALIZACAO ARTERIAL NO IAM - ANGIOPLAST PRIMA - C/ OU S/ SUPORTE CIRCULATORIO (BALAO INTRAORTICO)',
                    'RECANALIZACAO MECANICA DO IAM (ANGIOPLASTIA PRIMARIA COM BALAO)',
                    'ANGIOPLASTIA CORONARIANA C/ IMPLANTE DE DOIS STENTS'
                ) THEN 'ANGIOPLASTIA'
                WHEN c.ds_cirurgia IN (
                    'CATETERISMO CARDIACO D E/OU E C/ ESTUDO CINEANGIOGRAFICO E DE REVASC. CIRURGICA DO MIOCARDIO',
                    'CATETERISMO CARDIACO E E/OU D COM CINEANGIOCORONARIOGRAFIA E VENTRICULOGRAFIA',
                    'CATETERISMO CARDIACO D E/OU E COM  OU  SEM  CINECORONARIOGRAFIA / CINEANGIOGRAFIA C/ AVAL. DE REAT.'
                ) THEN 'CATETERISMO'
                ELSE NVL(am.ds_ati_med, 'OUTROS')
            END AS tipo
        FROM dbamv.prestador_aviso pa
        LEFT JOIN dbamv.aviso_cirurgia ac
          ON ac.cd_aviso_cirurgia = pa.cd_aviso_cirurgia
        LEFT JOIN dbamv.cirurgia_aviso ca
          ON ca.cd_cirurgia_aviso = pa.cd_cirurgia_aviso
        LEFT JOIN dbamv.ati_med am
          ON am.cd_ati_med = pa.cd_ati_med
        LEFT JOIN dbamv.cirurgia c
          ON c.cd_cirurgia = ca.cd_cirurgia
        LEFT JOIN dbamv.convenio co
          ON co.cd_convenio = ca.cd_convenio
        LEFT JOIN dbamv.prestador pr
          ON pr.cd_prestador = pa.cd_prestador
        WHERE pa.sn_principal = 'S'
          AND ac.tp_situacao = 'R'
          AND ac.cd_cen_cir IN (1, 2)
          AND ac.dt_aviso_cirurgia >= :data_inicio
          AND ac.dt_aviso_cirurgia <  :data_fim_exclusiva
          AND (:cd_convenio IS NULL OR INSTR(',' || :cd_convenio || ',', ',' || TO_CHAR(ca.cd_convenio) || ',') > 0)
          AND (:procedimento IS NULL OR c.ds_cirurgia = :procedimento)
        GROUP BY
            TRUNC(ac.dt_aviso_cirurgia),
            ca.cd_convenio,
            co.nm_convenio,
            ca.cd_cirurgia,
            c.ds_cirurgia,
            pr.cd_prestador,
            pr.nm_prestador,
            am.ds_ati_med
    )
    SELECT
        area,
        tipo,
        cd_convenio,
        nm_convenio,
        ds_cirurgia,
        cd_prestador,
        nm_prestador,
        SUM(qtd) AS prod_periodo,
        SUM(CASE
            WHEN dt_aviso_cirurgia >= GREATEST(:data_inicio, :data_fim_exclusiva - 7)
                THEN qtd
            ELSE 0
        END) AS prod_7d
    FROM base
    GROUP BY
        area,
        tipo,
        cd_convenio,
        nm_convenio,
        ds_cirurgia,
        cd_prestador,
        nm_prestador
    ORDER BY area, prod_periodo DESC, nm_convenio, ds_cirurgia, nm_prestador
    """
)


CONSULTA_PROCEDIMENTOS_CIRURGICOS = text(
    """
    SELECT DISTINCT
        CASE
            WHEN c.ds_cirurgia IN (
                'ANGIOPLASTIA TRANSLUMINAL PERCUTANEA DE BIFURCACAO E DE TRONCO COM IMPLANTE DE STENT',
                'ANGIOPLASTIA TRANSLUMINAL PERCUTANEA DE MULTIPLOS VASOS, COM IMPLANTE DE STENT',
                'ANGIOPLASTIA TRANSLUMINAL PERCUTANEA POR BALAO (1 VASO)',
                'CATETERISMO CARDIACO D E/OU E C/ ESTUDO CINEANGIOGRAFICO E DE REVASC. CIRURGICA DO MIOCARDIO',
                'CATETERISMO CARDIACO E E/OU D COM CINEANGIOCORONARIOGRAFIA E VENTRICULOGRAFIA',
                'IMPLANTE DE STENT CORONARIO DE VASO UNICO',
                'RECANALIZACAO ARTERIAL NO IAM - ANGIOPLAST PRIMA - C/ OU S/ SUPORTE CIRCULATORIO (BALAO INTRAORTICO)',
                'RECANALIZACAO MECANICA DO IAM (ANGIOPLASTIA PRIMARIA COM BALAO)',
                'CATETERISMO CARDIACO D E/OU E COM  OU  SEM  CINECORONARIOGRAFIA / CINEANGIOGRAFIA C/ AVAL. DE REAT.',
                'ANGIOPLASTIA CORONARIANA COM IMPLANTE DE STENT',
                'ANGIOPLASTIA CORONARIANA C/ IMPLANTE DE DOIS STENTS'
            ) THEN 'HEMODINAMICA'
            WHEN c.ds_cirurgia IN (
                'COLOCACAO DE PROTESE DE MAMA',
                'IMPLANTE DE CATETER VENOSO CENTRAL P/ PUNCAO, P/ NPP, QT, HEMODEPURACAO OU P/ INFUSAO DE SORO/DROGA',
                'LIPOASPIRACAO',
                'MASTOPEXIA',
                'PUNCAO LIQUORICA',
                'REVASCULARIZACAO DO MIOCARDIO'
            ) THEN 'CENTRO_CIRURGICO'
            WHEN c.ds_cirurgia = 'INSTALACAO DE MARCA-PASSO EPIMIOCARDIO TEMPORARIO'
                THEN 'ELETROFISIOLOGIA'
            WHEN c.ds_cirurgia IN (
                'ANGIOGRAFIA POR CATETERISMO NAO SELETIVO DE GRANDE VASO',
                'ANGIOGRAFIA POR CATETERISMO SELETIVO DE RAMO PRIMARIO - POR VASO',
                'ANGIOGRAFIA POR CATETERISMO SUPERSELETIVO DE RAMO SECUNDARIO OU DISTAL - POR VASO',
                'BLOQUEIO DE NERVO PERIFERICO - BLOQUEIOS ANESTESICOS DE NERVOS E ESTIMULOS NEUROVASCULARES',
                'BLOQUEIO DO SISTEMA NERVOSO AUTONOMO',
                'FISTULA ARTERIOVENOSA DOS MEMBROS'
            ) THEN 'VASCULAR'
            ELSE 'OUTROS'
        END AS area,
        c.ds_cirurgia AS procedimento
    FROM dbamv.prestador_aviso pa
    LEFT JOIN dbamv.aviso_cirurgia ac
      ON ac.cd_aviso_cirurgia = pa.cd_aviso_cirurgia
    LEFT JOIN dbamv.cirurgia_aviso ca
      ON ca.cd_cirurgia_aviso = pa.cd_cirurgia_aviso
    LEFT JOIN dbamv.cirurgia c
      ON c.cd_cirurgia = ca.cd_cirurgia
    WHERE pa.sn_principal = 'S'
      AND ac.tp_situacao = 'R'
      AND ac.cd_cen_cir IN (1, 2)
      AND ac.dt_aviso_cirurgia >= :data_inicio
      AND ac.dt_aviso_cirurgia <  :data_fim_exclusiva
      AND (:cd_convenio IS NULL OR INSTR(',' || :cd_convenio || ',', ',' || TO_CHAR(ca.cd_convenio) || ',') > 0)
      AND c.ds_cirurgia IS NOT NULL
    ORDER BY area, procedimento
    """
)


CONSULTA_FATURAMENTO_BASE = """
    WITH producao_fat AS (
        SELECT
            'HOSPITALAR' AS origem_conta,
            rf.cd_reg_fat AS cd_conta,
            irf.cd_lancamento,
            rf.cd_atendimento,
            a.cd_paciente,
            p.nm_paciente,
            a.cd_prestador,
            pr.nm_prestador,
            NVL(rf.cd_convenio, a.cd_convenio) AS cd_convenio,
            conv.nm_convenio,
            a.cd_ori_ate,
            oa.ds_ori_ate AS origem_atendimento,
            a.tp_atendimento,
            CASE a.tp_atendimento
                WHEN 'I' THEN 'INTERNACAO'
                WHEN 'A' THEN 'AMBULATORIO'
                WHEN 'U' THEN 'EMERGENCIA'
                WHEN 'E' THEN 'EXAMES'
                ELSE a.tp_atendimento
            END AS tipo_atendimento,
            irf.cd_setor,
            s.nm_setor,
            irf.dt_lancamento,
            irf.hr_lancamento,
            irf.cd_gru_fat,
            irf.cd_pro_fat,
            pf.ds_pro_fat,
            CAST(irf.cd_procedimento AS VARCHAR2(30)) AS cd_procedimento,
            irf.qt_lancamento,
            irf.vl_unitario,
            irf.vl_acrescimo,
            irf.vl_desconto,
            NVL(irf.sn_pertence_pacote, 'N') AS sn_pertence_pacote,
            NVL(irf.vl_total_conta, 0) AS vl_total_item_bruto,
            CASE
                WHEN COUNT(CASE WHEN NVL(irf.sn_pertence_pacote, 'N') = 'S' THEN 1 END)
                     OVER (PARTITION BY rf.cd_reg_fat) > 0
                    THEN CASE
                        WHEN ROW_NUMBER() OVER (
                            PARTITION BY rf.cd_reg_fat
                            ORDER BY
                                CASE WHEN NVL(irf.sn_pertence_pacote, 'N') <> 'S' THEN 0 ELSE 1 END,
                                irf.dt_lancamento,
                                irf.cd_lancamento
                        ) = 1
                            THEN NVL(rf.vl_total_conta, 0)
                        ELSE 0
                    END
                WHEN NVL(irf.sn_pertence_pacote, 'N') = 'S'
                    THEN 0
                ELSE NVL(irf.vl_total_conta, 0)
            END AS vl_total_item,
            rf.sn_fechada,
            rf.dt_fechamento,
            CASE
                WHEN NVL(rf.sn_fechada, 'N') = 'S'
                  OR rf.dt_fechamento IS NOT NULL
                    THEN 'SIM'
                ELSE 'NAO'
            END AS faturado,
            rf.cd_remessa,
            rf.dt_remessa,
            CASE
                WHEN rf.cd_remessa IS NOT NULL
                    THEN 'SIM'
                ELSE 'NAO'
            END AS em_remessa,
            CASE
                WHEN (NVL(rf.sn_fechada, 'N') = 'S' OR rf.dt_fechamento IS NOT NULL)
                 AND rf.cd_remessa IS NOT NULL
                    THEN 'SIM'
                ELSE 'NAO'
            END AS faturado_em_remessa
        FROM dbamv.reg_fat rf
        JOIN dbamv.itreg_fat irf
          ON irf.cd_reg_fat = rf.cd_reg_fat
        JOIN dbamv.atendime a
          ON a.cd_atendimento = rf.cd_atendimento
        JOIN dbamv.paciente p
          ON p.cd_paciente = a.cd_paciente
        LEFT JOIN dbamv.prestador pr
          ON pr.cd_prestador = a.cd_prestador
        LEFT JOIN dbamv.convenio conv
          ON conv.cd_convenio = NVL(rf.cd_convenio, a.cd_convenio)
        LEFT JOIN dbamv.ori_ate oa
          ON oa.cd_ori_ate = a.cd_ori_ate
        LEFT JOIN dbamv.setor s
          ON s.cd_setor = irf.cd_setor
        LEFT JOIN dbamv.pro_fat pf
          ON pf.cd_pro_fat = irf.cd_pro_fat
        WHERE irf.dt_lancamento >= :data_inicio
          AND irf.dt_lancamento <  :data_fim_exclusiva
          AND (:cd_convenio IS NULL OR INSTR(',' || :cd_convenio || ',', ',' || TO_CHAR(NVL(rf.cd_convenio, a.cd_convenio)) || ',') > 0)

        UNION ALL

        SELECT
            'AMBULATORIAL' AS origem_conta,
            ra.cd_reg_amb AS cd_conta,
            ira.cd_lancamento,
            ira.cd_atendimento,
            a.cd_paciente,
            p.nm_paciente,
            a.cd_prestador,
            pr.nm_prestador,
            NVL(ira.cd_convenio, NVL(ra.cd_convenio, a.cd_convenio)) AS cd_convenio,
            conv.nm_convenio,
            a.cd_ori_ate,
            oa.ds_ori_ate AS origem_atendimento,
            a.tp_atendimento,
            CASE a.tp_atendimento
                WHEN 'I' THEN 'INTERNACAO'
                WHEN 'A' THEN 'AMBULATORIO'
                WHEN 'U' THEN 'EMERGENCIA'
                WHEN 'E' THEN 'EXAMES'
                ELSE a.tp_atendimento
            END AS tipo_atendimento,
            ira.cd_setor,
            s.nm_setor,
            ra.dt_lancamento,
            ira.hr_lancamento,
            ira.cd_gru_fat,
            ira.cd_pro_fat,
            pf.ds_pro_fat,
            CAST(NULL AS VARCHAR2(30)) AS cd_procedimento,
            ira.qt_lancamento,
            ira.vl_unitario,
            ira.vl_acrescimo,
            ira.vl_desconto,
            NVL(ira.sn_pertence_pacote, 'N') AS sn_pertence_pacote,
            NVL(ira.vl_total_conta, 0) AS vl_total_item_bruto,
            CASE
                WHEN COUNT(CASE WHEN NVL(ira.sn_pertence_pacote, 'N') = 'S' THEN 1 END)
                     OVER (PARTITION BY ra.cd_reg_amb) > 0
                    THEN CASE
                        WHEN ROW_NUMBER() OVER (
                            PARTITION BY ra.cd_reg_amb
                            ORDER BY
                                CASE WHEN NVL(ira.sn_pertence_pacote, 'N') <> 'S' THEN 0 ELSE 1 END,
                                ra.dt_lancamento,
                                ira.cd_lancamento
                        ) = 1
                            THEN NVL(ra.vl_total_conta, 0)
                        ELSE 0
                    END
                WHEN NVL(ira.sn_pertence_pacote, 'N') = 'S'
                    THEN 0
                ELSE NVL(ira.vl_total_conta, 0)
            END AS vl_total_item,
            NVL(ira.sn_fechada, ra.sn_fechada) AS sn_fechada,
            ira.dt_fechamento,
            CASE
                WHEN NVL(NVL(ira.sn_fechada, ra.sn_fechada), 'N') = 'S'
                  OR ira.dt_fechamento IS NOT NULL
                    THEN 'SIM'
                ELSE 'NAO'
            END AS faturado,
            ra.cd_remessa,
            ra.dt_remessa,
            CASE
                WHEN ra.cd_remessa IS NOT NULL
                    THEN 'SIM'
                ELSE 'NAO'
            END AS em_remessa,
            CASE
                WHEN (NVL(NVL(ira.sn_fechada, ra.sn_fechada), 'N') = 'S' OR ira.dt_fechamento IS NOT NULL)
                 AND ra.cd_remessa IS NOT NULL
                    THEN 'SIM'
                ELSE 'NAO'
            END AS faturado_em_remessa
        FROM dbamv.reg_amb ra
        JOIN dbamv.itreg_amb ira
          ON ira.cd_reg_amb = ra.cd_reg_amb
        JOIN dbamv.atendime a
          ON a.cd_atendimento = ira.cd_atendimento
        JOIN dbamv.paciente p
          ON p.cd_paciente = a.cd_paciente
        LEFT JOIN dbamv.prestador pr
          ON pr.cd_prestador = a.cd_prestador
        LEFT JOIN dbamv.convenio conv
          ON conv.cd_convenio = NVL(ira.cd_convenio, NVL(ra.cd_convenio, a.cd_convenio))
        LEFT JOIN dbamv.ori_ate oa
          ON oa.cd_ori_ate = a.cd_ori_ate
        LEFT JOIN dbamv.setor s
          ON s.cd_setor = ira.cd_setor
        LEFT JOIN dbamv.pro_fat pf
          ON pf.cd_pro_fat = ira.cd_pro_fat
        WHERE ra.dt_lancamento >= :data_inicio
          AND ra.dt_lancamento <  :data_fim_exclusiva
          AND (:cd_convenio IS NULL OR INSTR(',' || :cd_convenio || ',', ',' || TO_CHAR(NVL(ira.cd_convenio, NVL(ra.cd_convenio, a.cd_convenio))) || ',') > 0)
    )
"""

CONSULTA_FATURAMENTO_CONVENIO = text(
    CONSULTA_FATURAMENTO_BASE + """
    SELECT *
    FROM (
        SELECT
            origem_conta,
            cd_conta,
            cd_lancamento,
            cd_atendimento,
            cd_paciente,
            nm_paciente,
            cd_prestador,
            nm_prestador,
            cd_convenio,
            nm_convenio,
            cd_ori_ate,
            origem_atendimento,
            tp_atendimento,
            tipo_atendimento,
            cd_setor,
            nm_setor,
            dt_lancamento,
            hr_lancamento,
            cd_gru_fat,
            cd_pro_fat,
            ds_pro_fat,
            cd_procedimento,
            qt_lancamento,
            vl_unitario,
            vl_acrescimo,
            vl_desconto,
            sn_pertence_pacote,
            vl_total_item_bruto,
            vl_total_item,
            sn_fechada,
            dt_fechamento,
            faturado,
            cd_remessa,
            dt_remessa,
            em_remessa,
            faturado_em_remessa
        FROM producao_fat
        ORDER BY dt_lancamento DESC, origem_conta, cd_conta, cd_lancamento
    )
    WHERE ROWNUM <= :limite
    """
)

CONSULTA_FATURAMENTO_SUS_PBIX = text(
    """
    WITH fatusus AS (
        SELECT
            rf.cd_atendimento,
            SUM(NVL(v.vl_linha, 0)) AS vl_linha,
            CASE
                WHEN ot.cd_ori_ate = '26' AND s.cd_sub_plano = '300' THEN 'ESTADO'
                WHEN ot.cd_ori_ate = '18' AND s.cd_sub_plano = '300' THEN 'ESTADO'
                WHEN ot.cd_ori_ate = '3' AND s.cd_sub_plano = '300' THEN 'ESTADO'
                WHEN ot.cd_ori_ate = '18' AND s.cd_sub_plano = '200' THEN 'MUNICIPIO'
                WHEN ot.cd_ori_ate = '3' AND s.cd_sub_plano = '200' THEN 'MUNICIPIO'
                WHEN ot.cd_ori_ate = '3' AND s.cd_sub_plano IS NULL THEN 'MUNICIPIO'
                WHEN ot.cd_ori_ate = '18' AND s.cd_sub_plano IS NULL THEN 'ESTADO'
                WHEN ot.cd_ori_ate = '1' AND s.cd_sub_plano IS NULL THEN 'ESTADO'
                ELSE NVL(s.ds_sub_plano, c.nm_convenio)
            END AS sub_plano
        FROM dbamv.v_ffis_valor_prestador_aih v
        LEFT JOIN dbamv.reg_fat rf
          ON rf.cd_reg_fat = v.cd_reg_fat
        LEFT JOIN dbamv.atendime a
          ON a.cd_atendimento = rf.cd_atendimento
        LEFT JOIN dbamv.ori_ate ot
          ON ot.cd_ori_ate = a.cd_ori_ate
        LEFT JOIN dbamv.convenio c
          ON c.cd_convenio = a.cd_convenio
        LEFT JOIN dbamv.sub_plano s
          ON a.cd_convenio = s.cd_convenio
         AND s.cd_sub_plano = a.cd_sub_plano
        WHERE v.dt_competencia >= :data_inicio
          AND v.dt_competencia < :data_fim_exclusiva
          AND (:cd_convenio IS NULL OR INSTR(',' || :cd_convenio || ',', ',' || TO_CHAR(a.cd_convenio) || ',') > 0)
        GROUP BY
            rf.cd_atendimento,
            CASE
                WHEN ot.cd_ori_ate = '26' AND s.cd_sub_plano = '300' THEN 'ESTADO'
                WHEN ot.cd_ori_ate = '18' AND s.cd_sub_plano = '300' THEN 'ESTADO'
                WHEN ot.cd_ori_ate = '3' AND s.cd_sub_plano = '300' THEN 'ESTADO'
                WHEN ot.cd_ori_ate = '18' AND s.cd_sub_plano = '200' THEN 'MUNICIPIO'
                WHEN ot.cd_ori_ate = '3' AND s.cd_sub_plano = '200' THEN 'MUNICIPIO'
                WHEN ot.cd_ori_ate = '3' AND s.cd_sub_plano IS NULL THEN 'MUNICIPIO'
                WHEN ot.cd_ori_ate = '18' AND s.cd_sub_plano IS NULL THEN 'ESTADO'
                WHEN ot.cd_ori_ate = '1' AND s.cd_sub_plano IS NULL THEN 'ESTADO'
                ELSE NVL(s.ds_sub_plano, c.nm_convenio)
            END
    ),
    grupos AS (
        SELECT
            CASE WHEN sub_plano IN ('ESTADO', 'MUNICIPIO') THEN sub_plano ELSE 'SUS' END AS grupo,
            cd_atendimento,
            vl_linha
        FROM fatusus
    )
    SELECT grupo, SUM(vl_linha) AS valor, COUNT(DISTINCT cd_atendimento) AS qtd_pacientes
      FROM grupos
     GROUP BY grupo
    UNION ALL
    SELECT 'SUS_TOTAL' AS grupo, SUM(vl_linha) AS valor, COUNT(DISTINCT cd_atendimento) AS qtd_pacientes
      FROM grupos
    """
)

CONSULTA_FATURAMENTO_REMESSA_PBIX = text(
    """
    WITH fatura_remessa AS (
        SELECT DISTINCT
            rf.cd_atendimento AS cod_atend,
            rf.cd_reg_fat AS conta,
            a.cd_paciente AS cod_paciente,
            f.dt_competencia,
            rf.cd_convenio AS cod_convenio,
            c.nm_convenio AS nome_convenio,
            rf.vl_total_conta AS vl_total,
            CASE
                WHEN rf.cd_convenio = '3' THEN 'PARTICULAR'
                WHEN s.cd_sub_plano = '200' OR s.cd_sub_plano = '300' THEN s.ds_sub_plano
                WHEN rf.cd_convenio = '1' AND ot.cd_ori_ate = '3' THEN 'MUNICIPIO'
                WHEN rf.cd_convenio = '1' AND ot.cd_ori_ate = '18' THEN 'ESTADO'
                WHEN s.cd_sub_plano <> '200' OR s.cd_sub_plano <> '300' THEN c.nm_convenio
                ELSE NVL(s.ds_sub_plano, c.nm_convenio)
            END AS sub_plano
        FROM dbamv.reg_fat rf
        LEFT JOIN dbamv.atendime a ON rf.cd_atendimento = a.cd_atendimento
        LEFT JOIN dbamv.ori_ate ot ON ot.cd_ori_ate = a.cd_ori_ate
        LEFT JOIN dbamv.remessa_fatura re ON rf.cd_remessa = re.cd_remessa
        LEFT JOIN dbamv.fatura f ON re.cd_fatura = f.cd_fatura
        LEFT JOIN dbamv.convenio c ON rf.cd_convenio = c.cd_convenio
        LEFT JOIN dbamv.sub_plano s ON s.cd_convenio = c.cd_convenio AND s.cd_sub_plano = a.cd_sub_plano
        WHERE rf.cd_atendimento IS NOT NULL
          AND f.dt_competencia >= :data_inicio
          AND f.dt_competencia < :data_fim_exclusiva
          AND (:cd_convenio IS NULL OR INSTR(',' || :cd_convenio || ',', ',' || TO_CHAR(rf.cd_convenio) || ',') > 0)

        UNION ALL

        SELECT DISTINCT
            ib.cd_atendimento AS cod_atend,
            rb.cd_reg_amb AS conta,
            a.cd_paciente AS cod_paciente,
            f.dt_competencia,
            rb.cd_convenio AS cod_convenio,
            c.nm_convenio AS nome_convenio,
            rb.vl_total_conta AS vl_total,
            CASE
                WHEN rb.cd_convenio = '3' THEN 'PARTICULAR'
                WHEN s.cd_sub_plano = '200' OR s.cd_sub_plano = '300' THEN s.ds_sub_plano
                WHEN rb.cd_convenio = '1' AND ot.cd_ori_ate = '3' THEN 'MUNICIPIO'
                WHEN rb.cd_convenio = '1' AND ot.cd_ori_ate = '18' THEN 'ESTADO'
                WHEN s.cd_sub_plano <> '200' OR s.cd_sub_plano <> '300' THEN c.nm_convenio
                ELSE NVL(s.ds_sub_plano, c.nm_convenio)
            END AS sub_plano
        FROM dbamv.reg_amb rb
        LEFT JOIN dbamv.itreg_amb ib ON rb.cd_reg_amb = ib.cd_reg_amb
        LEFT JOIN dbamv.atendime a ON ib.cd_atendimento = a.cd_atendimento
        LEFT JOIN dbamv.ori_ate ot ON ot.cd_ori_ate = a.cd_ori_ate
        LEFT JOIN dbamv.remessa_fatura re ON rb.cd_remessa = re.cd_remessa
        LEFT JOIN dbamv.fatura f ON re.cd_fatura = f.cd_fatura
        LEFT JOIN dbamv.convenio c ON a.cd_convenio = c.cd_convenio
        LEFT JOIN dbamv.sub_plano s ON a.cd_convenio = s.cd_convenio AND s.cd_sub_plano = a.cd_sub_plano
        WHERE ib.cd_atendimento IS NOT NULL
          AND f.dt_competencia >= :data_inicio
          AND f.dt_competencia < :data_fim_exclusiva
          AND (:cd_convenio IS NULL OR INSTR(',' || :cd_convenio || ',', ',' || TO_CHAR(rb.cd_convenio) || ',') > 0)
    ),
    classificada AS (
        SELECT
            cod_atend,
            conta,
            cod_paciente,
            cod_convenio,
            nome_convenio,
            vl_total,
            CASE
                WHEN sub_plano IN ('SUS - INTERNACAO', 'SUS - AMBULATORIO') THEN 'SUS'
                WHEN sub_plano = 'ESTADO' THEN 'ESTADO'
                WHEN sub_plano = 'MUNICIPIO' THEN 'MUNICIPIO'
                WHEN sub_plano = 'PARTICULAR' THEN 'PARTICULAR'
                ELSE 'CONVENIO'
            END AS tipo_con
        FROM fatura_remessa
    )
    SELECT
        tipo_con AS grupo,
        cod_convenio,
        nome_convenio,
        SUM(NVL(vl_total, 0)) AS valor,
        COUNT(DISTINCT conta) AS qtd_contas,
        COUNT(DISTINCT cod_atend) AS qtd_atendimentos,
        COUNT(DISTINCT cod_paciente) AS qtd_pacientes
    FROM classificada
    WHERE tipo_con IN ('CONVENIO', 'PARTICULAR')
      AND NVL(nome_convenio, '-') <> 'CORTESIA'
    GROUP BY tipo_con, cod_convenio, nome_convenio
    ORDER BY tipo_con, valor DESC
    """
)

CONSULTA_FATURAMENTO_AGREGADO = text(
    CONSULTA_FATURAMENTO_BASE + """
    SELECT
        nivel,
        chave,
        descricao,
        tp_atendimento,
        tipo_atendimento,
        cd_convenio,
        nm_convenio,
        cd_prestador,
        nm_prestador,
        producao_total,
        producao_aberta,
        contas_fechadas,
        em_remessa,
        faturado_em_remessa,
        valor_bruto_original,
        qtd_itens,
        qtd_contas,
        qtd_atendimentos,
        qtd_pacientes,
        qtd_pacientes_em_remessa,
        qtd_itens_pacote,
        ticket_medio_atendimento,
        ticket_medio_conta,
        perc_faturado_remessa
    FROM (
        SELECT
            'GERAL' AS nivel,
            'GERAL' AS chave,
            'Geral' AS descricao,
            CAST(NULL AS VARCHAR2(1)) AS tp_atendimento,
            CAST(NULL AS VARCHAR2(30)) AS tipo_atendimento,
            CAST(NULL AS NUMBER) AS cd_convenio,
            CAST(NULL AS VARCHAR2(200)) AS nm_convenio,
            CAST(NULL AS NUMBER) AS cd_prestador,
            CAST(NULL AS VARCHAR2(200)) AS nm_prestador,
            SUM(vl_total_item) AS producao_total,
            SUM(CASE WHEN faturado = 'NAO' THEN vl_total_item ELSE 0 END) AS producao_aberta,
            SUM(CASE WHEN faturado = 'SIM' THEN vl_total_item ELSE 0 END) AS contas_fechadas,
            SUM(CASE WHEN em_remessa = 'SIM' THEN vl_total_item ELSE 0 END) AS em_remessa,
            SUM(CASE WHEN faturado_em_remessa = 'SIM' THEN vl_total_item ELSE 0 END) AS faturado_em_remessa,
            SUM(vl_total_item_bruto) AS valor_bruto_original,
            COUNT(*) AS qtd_itens,
            COUNT(DISTINCT cd_conta) AS qtd_contas,
            COUNT(DISTINCT cd_atendimento) AS qtd_atendimentos,
            COUNT(DISTINCT cd_paciente) AS qtd_pacientes,
            COUNT(DISTINCT CASE WHEN faturado_em_remessa = 'SIM' THEN cd_paciente END) AS qtd_pacientes_em_remessa,
            SUM(CASE WHEN sn_pertence_pacote = 'S' THEN 1 ELSE 0 END) AS qtd_itens_pacote,
            ROUND(SUM(vl_total_item) / NULLIF(COUNT(DISTINCT cd_atendimento), 0), 2) AS ticket_medio_atendimento,
            ROUND(SUM(vl_total_item) / NULLIF(COUNT(DISTINCT cd_conta), 0), 2) AS ticket_medio_conta,
            ROUND(SUM(CASE WHEN faturado_em_remessa = 'SIM' THEN vl_total_item ELSE 0 END) / NULLIF(SUM(vl_total_item), 0), 4) AS perc_faturado_remessa
        FROM producao_fat

        UNION ALL

        SELECT
            'TIPO_ATENDIMENTO',
            NVL(tp_atendimento, 'N/I'),
            NVL(tipo_atendimento, 'Nao informado'),
            tp_atendimento,
            tipo_atendimento,
            CAST(NULL AS NUMBER),
            CAST(NULL AS VARCHAR2(200)),
            CAST(NULL AS NUMBER),
            CAST(NULL AS VARCHAR2(200)),
            SUM(vl_total_item),
            SUM(CASE WHEN faturado = 'NAO' THEN vl_total_item ELSE 0 END),
            SUM(CASE WHEN faturado = 'SIM' THEN vl_total_item ELSE 0 END),
            SUM(CASE WHEN em_remessa = 'SIM' THEN vl_total_item ELSE 0 END),
            SUM(CASE WHEN faturado_em_remessa = 'SIM' THEN vl_total_item ELSE 0 END),
            SUM(vl_total_item_bruto),
            COUNT(*),
            COUNT(DISTINCT cd_conta),
            COUNT(DISTINCT cd_atendimento),
            COUNT(DISTINCT cd_paciente),
            COUNT(DISTINCT CASE WHEN faturado_em_remessa = 'SIM' THEN cd_paciente END),
            SUM(CASE WHEN sn_pertence_pacote = 'S' THEN 1 ELSE 0 END),
            ROUND(SUM(vl_total_item) / NULLIF(COUNT(DISTINCT cd_atendimento), 0), 2),
            ROUND(SUM(vl_total_item) / NULLIF(COUNT(DISTINCT cd_conta), 0), 2),
            ROUND(SUM(CASE WHEN faturado_em_remessa = 'SIM' THEN vl_total_item ELSE 0 END) / NULLIF(SUM(vl_total_item), 0), 4)
        FROM producao_fat
        GROUP BY tp_atendimento, tipo_atendimento

        UNION ALL

        SELECT
            'CONVENIO',
            TO_CHAR(cd_convenio),
            NVL(nm_convenio, 'Sem convenio'),
            CAST(NULL AS VARCHAR2(1)),
            CAST(NULL AS VARCHAR2(30)),
            cd_convenio,
            nm_convenio,
            CAST(NULL AS NUMBER),
            CAST(NULL AS VARCHAR2(200)),
            SUM(vl_total_item),
            SUM(CASE WHEN faturado = 'NAO' THEN vl_total_item ELSE 0 END),
            SUM(CASE WHEN faturado = 'SIM' THEN vl_total_item ELSE 0 END),
            SUM(CASE WHEN em_remessa = 'SIM' THEN vl_total_item ELSE 0 END),
            SUM(CASE WHEN faturado_em_remessa = 'SIM' THEN vl_total_item ELSE 0 END),
            SUM(vl_total_item_bruto),
            COUNT(*),
            COUNT(DISTINCT cd_conta),
            COUNT(DISTINCT cd_atendimento),
            COUNT(DISTINCT cd_paciente),
            COUNT(DISTINCT CASE WHEN faturado_em_remessa = 'SIM' THEN cd_paciente END),
            SUM(CASE WHEN sn_pertence_pacote = 'S' THEN 1 ELSE 0 END),
            ROUND(SUM(vl_total_item) / NULLIF(COUNT(DISTINCT cd_atendimento), 0), 2),
            ROUND(SUM(vl_total_item) / NULLIF(COUNT(DISTINCT cd_conta), 0), 2),
            ROUND(SUM(CASE WHEN faturado_em_remessa = 'SIM' THEN vl_total_item ELSE 0 END) / NULLIF(SUM(vl_total_item), 0), 4)
        FROM producao_fat
        GROUP BY cd_convenio, nm_convenio

        UNION ALL

        SELECT
            'PRESTADOR',
            TO_CHAR(cd_prestador),
            NVL(nm_prestador, 'Sem prestador'),
            CAST(NULL AS VARCHAR2(1)),
            CAST(NULL AS VARCHAR2(30)),
            CAST(NULL AS NUMBER),
            CAST(NULL AS VARCHAR2(200)),
            cd_prestador,
            nm_prestador,
            SUM(vl_total_item),
            SUM(CASE WHEN faturado = 'NAO' THEN vl_total_item ELSE 0 END),
            SUM(CASE WHEN faturado = 'SIM' THEN vl_total_item ELSE 0 END),
            SUM(CASE WHEN em_remessa = 'SIM' THEN vl_total_item ELSE 0 END),
            SUM(CASE WHEN faturado_em_remessa = 'SIM' THEN vl_total_item ELSE 0 END),
            SUM(vl_total_item_bruto),
            COUNT(*),
            COUNT(DISTINCT cd_conta),
            COUNT(DISTINCT cd_atendimento),
            COUNT(DISTINCT cd_paciente),
            COUNT(DISTINCT CASE WHEN faturado_em_remessa = 'SIM' THEN cd_paciente END),
            SUM(CASE WHEN sn_pertence_pacote = 'S' THEN 1 ELSE 0 END),
            ROUND(SUM(vl_total_item) / NULLIF(COUNT(DISTINCT cd_atendimento), 0), 2),
            ROUND(SUM(vl_total_item) / NULLIF(COUNT(DISTINCT cd_conta), 0), 2),
            ROUND(SUM(CASE WHEN faturado_em_remessa = 'SIM' THEN vl_total_item ELSE 0 END) / NULLIF(SUM(vl_total_item), 0), 4)
        FROM producao_fat
        GROUP BY cd_prestador, nm_prestador

        UNION ALL

        SELECT
            'TIPO_CONVENIO',
            NVL(tp_atendimento, 'N/I') || '|' || TO_CHAR(cd_convenio),
            NVL(tipo_atendimento, 'Nao informado') || ' - ' || NVL(nm_convenio, 'Sem convenio'),
            tp_atendimento,
            tipo_atendimento,
            cd_convenio,
            nm_convenio,
            CAST(NULL AS NUMBER),
            CAST(NULL AS VARCHAR2(200)),
            SUM(vl_total_item),
            SUM(CASE WHEN faturado = 'NAO' THEN vl_total_item ELSE 0 END),
            SUM(CASE WHEN faturado = 'SIM' THEN vl_total_item ELSE 0 END),
            SUM(CASE WHEN em_remessa = 'SIM' THEN vl_total_item ELSE 0 END),
            SUM(CASE WHEN faturado_em_remessa = 'SIM' THEN vl_total_item ELSE 0 END),
            SUM(vl_total_item_bruto),
            COUNT(*),
            COUNT(DISTINCT cd_conta),
            COUNT(DISTINCT cd_atendimento),
            COUNT(DISTINCT cd_paciente),
            COUNT(DISTINCT CASE WHEN faturado_em_remessa = 'SIM' THEN cd_paciente END),
            SUM(CASE WHEN sn_pertence_pacote = 'S' THEN 1 ELSE 0 END),
            ROUND(SUM(vl_total_item) / NULLIF(COUNT(DISTINCT cd_atendimento), 0), 2),
            ROUND(SUM(vl_total_item) / NULLIF(COUNT(DISTINCT cd_conta), 0), 2),
            ROUND(SUM(CASE WHEN faturado_em_remessa = 'SIM' THEN vl_total_item ELSE 0 END) / NULLIF(SUM(vl_total_item), 0), 4)
        FROM producao_fat
        GROUP BY tp_atendimento, tipo_atendimento, cd_convenio, nm_convenio

        UNION ALL

        SELECT
            'TIPO_PRESTADOR',
            NVL(tp_atendimento, 'N/I') || '|' || TO_CHAR(cd_prestador),
            NVL(tipo_atendimento, 'Nao informado') || ' - ' || NVL(nm_prestador, 'Sem prestador'),
            tp_atendimento,
            tipo_atendimento,
            CAST(NULL AS NUMBER),
            CAST(NULL AS VARCHAR2(200)),
            cd_prestador,
            nm_prestador,
            SUM(vl_total_item),
            SUM(CASE WHEN faturado = 'NAO' THEN vl_total_item ELSE 0 END),
            SUM(CASE WHEN faturado = 'SIM' THEN vl_total_item ELSE 0 END),
            SUM(CASE WHEN em_remessa = 'SIM' THEN vl_total_item ELSE 0 END),
            SUM(CASE WHEN faturado_em_remessa = 'SIM' THEN vl_total_item ELSE 0 END),
            SUM(vl_total_item_bruto),
            COUNT(*),
            COUNT(DISTINCT cd_conta),
            COUNT(DISTINCT cd_atendimento),
            COUNT(DISTINCT cd_paciente),
            COUNT(DISTINCT CASE WHEN faturado_em_remessa = 'SIM' THEN cd_paciente END),
            SUM(CASE WHEN sn_pertence_pacote = 'S' THEN 1 ELSE 0 END),
            ROUND(SUM(vl_total_item) / NULLIF(COUNT(DISTINCT cd_atendimento), 0), 2),
            ROUND(SUM(vl_total_item) / NULLIF(COUNT(DISTINCT cd_conta), 0), 2),
            ROUND(SUM(CASE WHEN faturado_em_remessa = 'SIM' THEN vl_total_item ELSE 0 END) / NULLIF(SUM(vl_total_item), 0), 4)
        FROM producao_fat
        GROUP BY tp_atendimento, tipo_atendimento, cd_prestador, nm_prestador
    )
    ORDER BY nivel, producao_total DESC
    """
)



@router.get("/teste-ergometrico", status_code=HTTPStatus.OK)
def consultar_teste_ergometrico(
    usuario_atual: ValidaUsuarioAtual,
    data_inicio: date,
    data_fim: date,
    session: Session = Depends(get_session_oracle),
):
    del usuario_atual
    if data_fim <= data_inicio:
        raise HTTPException(
            status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
            detail="data_fim deve ser posterior a data_inicio.",
        )

    params = {"data_inicio": data_inicio, "data_fim": data_fim}
    try:
        agenda_rows = (
            session.execute(CONSULTA_TESTE_ERGOMETRICO_AGENDADOS, params)
            .mappings()
            .all()
        )
        atendimento_rows = (
            session.execute(CONSULTA_TESTE_ERGOMETRICO_ATENDIMENTOS, params)
            .mappings()
            .all()
        )
        atendimento_checkup_rows = (
            session.execute(
                CONSULTA_TESTE_ERGOMETRICO_ATENDIMENTOS_CHECKUP,
                params,
            )
            .mappings()
            .all()
        )
        laudo_rows = (
            session.execute(CONSULTA_TESTE_ERGOMETRICO_LAUDOS, params)
            .mappings()
            .all()
        )
        laudo_checkup_rows = (
            session.execute(
                CONSULTA_TESTE_ERGOMETRICO_LAUDOS_CHECKUP,
                params,
            )
            .mappings()
            .all()
        )
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=HTTPStatus.SERVICE_UNAVAILABLE,
            detail="Não foi possível consultar os exames de teste ergométrico no MV.",
        ) from exc

    agendados = _rows_to_dict(agenda_rows)
    atendimentos = _rows_to_dict(atendimento_rows)
    atendimentos.extend(_rows_to_dict(atendimento_checkup_rows))
    exames = _rows_to_dict(laudo_rows)
    exames.extend(_rows_to_dict(laudo_checkup_rows))

    atendimentos_ids = {
        str(row.get("cd_atendimento"))
        for row in atendimentos
        if row.get("cd_atendimento") is not None
    }
    pendentes = [
        row for row in agendados
        if not row.get("cd_atendimento") or str(row.get("cd_atendimento")) not in atendimentos_ids
    ]

    prestadores = {}
    for row in exames:
        prestador_key = row.get("cd_prestador") or row.get("nm_prestador") or "sem_prestador"
        current = prestadores.setdefault(
            str(prestador_key),
            {
                "cd_prestador": row.get("cd_prestador"),
                "nm_prestador": row.get("nm_prestador") or "Prestador não informado",
                "crm": row.get("crm") or "-",
                "exames": 0,
            },
        )
        current["exames"] += 1

    return {
        "resumo": {
            "agendados_confirmados": len(agendados),
            "atendimentos_teste": len(atendimentos),
            "atendimentos_realizados": len(atendimentos),
            "laudos_assinados": len(exames),
            "conciliados": len(exames),
            "pendentes": len(pendentes),
            "prestadores": len(prestadores),
        },
        "prestadores": list(prestadores.values()),
        "agendados": agendados,
        "atendimentos": atendimentos,
        "exames": exames,
        "pendentes": pendentes,
        "total": len(exames),
    }


def _executar_consulta_indicador(
    sql_query,
    data_inicio: date,
    data_fim: date,
    session: Session,
    cd_convenio: str | None = None,
    procedimento: str | None = None,
):
    params = _periodo_inclusivo(data_inicio, data_fim, cd_convenio, procedimento)
    try:
        return session.execute(sql_query, params).mappings().all()
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=HTTPStatus.SERVICE_UNAVAILABLE,
            detail="Não foi possível consultar os indicadores hospitalares no MV.",
        ) from exc


@router.get("/indicadores-hospitalares/resumo", status_code=HTTPStatus.OK)
def consultar_indicadores_hospitalares_resumo(
    usuario_atual: ValidaUsuarioAtual,
    data_inicio: date,
    data_fim: date,
    cd_convenio: str | None = Query(default=None),
    session: Session = Depends(get_session_oracle),
):
    del usuario_atual
    rows = _executar_consulta_indicador(
        CONSULTA_INDICADORES_HOSPITALARES_RESUMO,
        data_inicio,
        data_fim,
        session,
        cd_convenio,
    )
    resumo = _rows_to_dict(rows)[0] if rows else {}
    return {
        "periodo": {
            "data_inicio": data_inicio.isoformat(),
            "data_fim": data_fim.isoformat(),
        },
        "resumo": resumo,
    }


@router.get("/indicadores-hospitalares/series", status_code=HTTPStatus.OK)
def consultar_indicadores_hospitalares_series(
    usuario_atual: ValidaUsuarioAtual,
    data_inicio: date,
    data_fim: date,
    cd_convenio: str | None = Query(default=None),
    session: Session = Depends(get_session_oracle),
):
    del usuario_atual
    rows = _executar_consulta_indicador(
        CONSULTA_INDICADORES_HOSPITALARES_SERIES,
        data_inicio,
        data_fim,
        session,
        cd_convenio,
    )
    series = _rows_to_dict(rows)
    return {
        "periodo": {
            "data_inicio": data_inicio.isoformat(),
            "data_fim": data_fim.isoformat(),
        },
        "series": series,
        "total": len(series),
    }


@router.get(
    "/indicadores-hospitalares/agenda-ambulatorial",
    status_code=HTTPStatus.OK,
)
def consultar_indicadores_hospitalares_agenda_ambulatorial(
    usuario_atual: ValidaUsuarioAtual,
    data_inicio: date,
    data_fim: date,
    cd_convenio: str | None = Query(default=None),
    session: Session = Depends(get_session_oracle),
):
    del usuario_atual
    rows = _executar_consulta_indicador(
        CONSULTA_AGENDA_AMBULATORIAL,
        data_inicio,
        data_fim,
        session,
        cd_convenio,
    )
    agenda = _rows_to_dict(rows)
    return {
        "periodo": {
            "data_inicio": data_inicio.isoformat(),
            "data_fim": data_fim.isoformat(),
        },
        "agenda": agenda,
        "total": len(agenda),
    }


@router.get(
    "/indicadores-hospitalares/fluxo-ambulatorio-exames",
    status_code=HTTPStatus.OK,
)
def consultar_indicadores_hospitalares_fluxo_ambulatorio_exames(
    usuario_atual: ValidaUsuarioAtual,
    data_inicio: date,
    data_fim: date,
    cd_convenio: str | None = Query(default=None),
    limite: int = Query(default=1000, ge=1, le=5000),
    session: Session = Depends(get_session_oracle),
):
    del usuario_atual
    params = _periodo_inclusivo(data_inicio, data_fim, cd_convenio)
    params["limite"] = limite
    try:
        rows = session.execute(
            CONSULTA_FLUXO_AMBULATORIO_EXAMES,
            params,
        ).mappings().all()
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=HTTPStatus.SERVICE_UNAVAILABLE,
            detail="Não foi possível consultar os tempos do fluxo ambulatorial e exames no MV.",
        ) from exc
    fluxo = _rows_to_dict(rows)
    return {
        "periodo": {
            "data_inicio": data_inicio.isoformat(),
            "data_fim": data_fim.isoformat(),
        },
        "fluxo_ambulatorio_exames": fluxo,
        "total": len(fluxo),
        "limite": limite,
    }


@router.get(
    "/indicadores-hospitalares/exames-procedimentos",
    status_code=HTTPStatus.OK,
)
def consultar_indicadores_hospitalares_exames_procedimentos(
    usuario_atual: ValidaUsuarioAtual,
    data_inicio: date,
    data_fim: date,
    cd_convenio: str | None = Query(default=None),
    limite: int = Query(default=5000, ge=1, le=10000),
    session: Session = Depends(get_session_oracle),
):
    del usuario_atual
    params = _periodo_inclusivo(data_inicio, data_fim, cd_convenio)
    params["limite"] = limite
    try:
        rows = session.execute(CONSULTA_EXAMES_PROCEDIMENTOS, params).mappings().all()
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=HTTPStatus.SERVICE_UNAVAILABLE,
            detail="Nao foi possivel consultar os procedimentos de exames no MV.",
        ) from exc
    exames = _rows_to_dict(rows)
    return {
        "periodo": {
            "data_inicio": data_inicio.isoformat(),
            "data_fim": data_fim.isoformat(),
        },
        "exames_procedimentos": exames,
        "total": len(exames),
        "limite": limite,
    }




@router.get(
    "/indicadores-hospitalares/fluxo-pa-tempos",
    status_code=HTTPStatus.OK,
)
def consultar_indicadores_hospitalares_fluxo_pa_tempos(
    usuario_atual: ValidaUsuarioAtual,
    data_inicio: date,
    data_fim: date,
    cd_convenio: str | None = Query(default=None),
    limite: int = Query(default=1000, ge=1, le=5000),
    session: Session = Depends(get_session_oracle),
):
    del usuario_atual
    params = _periodo_inclusivo(data_inicio, data_fim, cd_convenio)
    params["limite"] = limite
    try:
        rows = session.execute(CONSULTA_FLUXO_PA_TEMPOS, params).mappings().all()
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=HTTPStatus.SERVICE_UNAVAILABLE,
            detail="Não foi possível consultar os tempos do fluxo PA no MV.",
        ) from exc
    fluxo = _rows_to_dict(rows)
    return {
        "periodo": {
            "data_inicio": data_inicio.isoformat(),
            "data_fim": data_fim.isoformat(),
        },
        "fluxo_pa_tempos": fluxo,
        "total": len(fluxo),
        "limite": limite,
    }

@router.get(
    "/indicadores-hospitalares/internacoes-detalhadas",
    status_code=HTTPStatus.OK,
)
def consultar_indicadores_hospitalares_internacoes_detalhadas(
    usuario_atual: ValidaUsuarioAtual,
    data_inicio: date,
    data_fim: date,
    cd_convenio: str | None = Query(default=None),
    limite: int = Query(default=500, ge=1, le=5000),
    session: Session = Depends(get_session_oracle),
):
    del usuario_atual
    rows = _executar_consulta_indicador(
        CONSULTA_INTERNACOES_DETALHADAS,
        data_inicio,
        data_fim,
        session,
        cd_convenio,
    )
    internacoes = _rows_to_dict(rows)
    return {
        "periodo": {
            "data_inicio": data_inicio.isoformat(),
            "data_fim": data_fim.isoformat(),
        },
        "internacoes": internacoes[:limite],
        "total": len(internacoes),
        "limite": limite,
    }


@router.get(
    "/rede/internacoes-ativas",
    status_code=HTTPStatus.OK,
)
def consultar_internacoes_ativas_rede(
    usuario_atual: ValidaUsuarioAtual,
    origens: str | None = Query(default=None),
    pacientes: str | None = Query(default=None),
    limite: int = Query(default=500, ge=1, le=1000),
    session: Session = Depends(get_session_oracle),
):
    """Lista somente internações ativas pertencentes ao universo informado.

    O consumidor deve enviar códigos de origem MV, códigos de pacientes já
    vinculados à rede, ou ambos. A consulta nunca retorna o censo hospitalar
    completo sem um desses filtros.
    """
    del usuario_atual
    origens_normalizadas = _normalizar_codigos_rede(origens, "origens")
    pacientes_normalizados = _normalizar_codigos_rede(pacientes, "pacientes")
    if not origens_normalizadas and not pacientes_normalizados:
        raise HTTPException(
            status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
            detail=(
                'Informe ao menos uma origem ou um paciente vinculado ao '
                'Prontocardio Rede.'
            ),
        )
    try:
        rows = session.execute(
            CONSULTA_INTERNACOES_ATIVAS_REDE,
            {
                "origens": origens_normalizadas,
                "pacientes": pacientes_normalizados,
            },
        ).mappings().all()
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=HTTPStatus.SERVICE_UNAVAILABLE,
            detail="Não foi possível consultar as internações ativas no MV.",
        ) from exc

    internacoes = _rows_to_dict(rows)[:limite]
    return {
        "internacoes": internacoes,
        "total": len(internacoes),
        "limite": limite,
    }


@router.get(
    '/rede/internacoes/{cd_atendimento}/contexto-medico',
    status_code=HTTPStatus.OK,
)
def consultar_contexto_medico_internacao_rede(
    usuario_atual: ValidaUsuarioAtual,
    cd_atendimento: int,
    usuario_mv: str = Query(...),
    session: Session = Depends(get_session_oracle),
):
    """Retorna o contexto mínimo de uma internação ativa para o ProntoRede."""
    del usuario_atual
    usuario = _normalizar_usuario_mv(usuario_mv)
    if usuario not in _usuarios_mv_permitidos():
        raise HTTPException(
            status_code=HTTPStatus.FORBIDDEN,
            detail='Contexto MV não autorizado.',
        )

    try:
        row = session.execute(
            CONSULTA_CONTEXTO_MEDICO_INTERNACAO_REDE,
            {
                'cd_atendimento': cd_atendimento,
                'usuario_mv': usuario,
            },
        ).mappings().first()
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=HTTPStatus.SERVICE_UNAVAILABLE,
            detail='Não foi possível consultar o contexto médico no MV.',
        ) from exc

    if row is None:
        raise HTTPException(
            status_code=HTTPStatus.NOT_FOUND,
            detail='Internação ativa não encontrada no MV.',
        )

    atendimento = _inteiro_positivo(row.get('cd_atendimento'))
    paciente = _inteiro_positivo(row.get('cd_paciente'))
    prestador = _inteiro_positivo(row.get('cd_prestador'))
    usuario_linha = _texto_linha(row.get('usuario_mv')).upper()
    conselho = _texto_linha(row.get('ds_conselho')).upper()
    numero_conselho = _texto_linha(row.get('ds_codigo_conselho')).upper()
    uf_conselho = _texto_linha(row.get('cd_uf_orgao_emissor')).upper()
    if (
        atendimento != cd_atendimento
        or paciente is None
        or prestador is None
        or usuario_linha != usuario
        or conselho != 'CRM'
        or not numero_conselho
    ):
        raise HTTPException(
            status_code=HTTPStatus.FORBIDDEN,
            detail='Contexto MV não autorizado.',
        )

    crm = (
        f'CRM-{uf_conselho} {numero_conselho}'
        if uf_conselho
        else f'CRM {numero_conselho}'
    )
    try:
        catalogo_rows = session.execute(
            CONSULTA_CATALOGO_ASSISTENCIAL_INTERNACAO_REDE,
            {'cd_atendimento': cd_atendimento},
        ).mappings().all()
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=HTTPStatus.SERVICE_UNAVAILABLE,
            detail='Não foi possível consultar o catálogo assistencial no MV.',
        ) from exc

    catalogo_assistencial = [
        {
            'id': _texto_linha(item.get('id')).upper(),
            'label': _texto_linha(item.get('label')),
            'tipo': _texto_linha(item.get('tipo')).upper(),
        }
        for item in catalogo_rows
        if (
            _texto_linha(item.get('id'))
            and _texto_linha(item.get('label'))
            and _texto_linha(item.get('tipo')).upper()
            in {'EXAME', 'PROCEDIMENTO'}
        )
    ]

    return {
        'contexto': {
            'cd_atendimento': atendimento,
            'cd_paciente': paciente,
            'nm_paciente': _texto_linha(row.get('nm_paciente')),
            'unidade_internacao': _texto_linha(
                row.get('unidade_internacao')
            ),
            'setor_internacao': _texto_linha(row.get('setor_internacao')),
            'leito': _texto_linha(row.get('leito')),
            'usuario_mv': usuario,
            'cd_prestador': prestador,
            'nm_prestador': _texto_linha(row.get('nm_prestador')),
            'crm': crm,
            'dh_atendimento': _json_value(row.get('dh_atendimento')),
            'internacao_ativa': True,
            'pode_atualizar_assistencial': True,
            'catalogo_assistencial': catalogo_assistencial,
        }
    }

@router.get(
    "/indicadores-hospitalares/producao-cirurgica",
    status_code=HTTPStatus.OK,
)
def consultar_indicadores_hospitalares_producao_cirurgica(
    usuario_atual: ValidaUsuarioAtual,
    data_inicio: date,
    data_fim: date,
    cd_convenio: str | None = Query(default=None),
    procedimento: str | None = Query(default=None),
    session: Session = Depends(get_session_oracle),
):
    del usuario_atual
    rows = _executar_consulta_indicador(
        CONSULTA_PRODUCAO_CIRURGICA,
        data_inicio,
        data_fim,
        session,
        cd_convenio,
        procedimento,
    )
    producao = _rows_to_dict(rows)
    return {
        "periodo": {
            "data_inicio": data_inicio.isoformat(),
            "data_fim": data_fim.isoformat(),
        },
        "producao_cirurgica": producao,
        "total": len(producao),
    }


@router.get(
    "/indicadores-hospitalares/receita-hemodinamica",
    status_code=HTTPStatus.OK,
)
def consultar_indicadores_hospitalares_receita_hemodinamica(
    usuario_atual: ValidaUsuarioAtual,
    data_inicio: date,
    data_fim: date,
    cd_convenio: str | None = Query(default=None),
    procedimento: str | None = Query(default=None),
    session: Session = Depends(get_session_oracle),
):
    del usuario_atual
    if data_fim < data_inicio:
        raise HTTPException(
            status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
            detail="data_fim deve ser igual ou posterior a data_inicio.",
        )

    try:
        return consultar_receita_hemodinamica(
            session=session,
            data_inicio=data_inicio,
            data_fim=data_fim,
            cd_convenio=_normalizar_convenios(cd_convenio),
            procedimento=procedimento,
        )
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=HTTPStatus.SERVICE_UNAVAILABLE,
            detail="Não foi possível consultar a receita da Hemodinâmica no MV.",
        ) from exc


@router.get(
    "/indicadores-hospitalares/faturamento",
    status_code=HTTPStatus.OK,
)
def consultar_indicadores_hospitalares_faturamento(
    usuario_atual: ValidaUsuarioAtual,
    data_inicio: date,
    data_fim: date,
    cd_convenio: str | None = Query(default=None),
    limite: int = Query(default=2000, ge=1, le=10000),
    session: Session = Depends(get_session_oracle),
):
    del usuario_atual
    params = _periodo_inclusivo(data_inicio, data_fim, cd_convenio)
    params["limite"] = limite
    try:
        rows = session.execute(CONSULTA_FATURAMENTO_CONVENIO, params).mappings().all()
        agregado_rows = session.execute(CONSULTA_FATURAMENTO_AGREGADO, params).mappings().all()
        sus_pbix_rows = session.execute(CONSULTA_FATURAMENTO_SUS_PBIX, params).mappings().all()
        remessa_pbix_rows = session.execute(CONSULTA_FATURAMENTO_REMESSA_PBIX, params).mappings().all()
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=HTTPStatus.SERVICE_UNAVAILABLE,
            detail="Não foi possível consultar o faturamento no MV.",
        ) from exc

    faturamento = _rows_to_dict(rows)
    agregados = _rows_to_dict(agregado_rows)
    faturamento_sus_pbix = _rows_to_dict(sus_pbix_rows)
    faturamento_remessa_pbix = _rows_to_dict(remessa_pbix_rows)

    def por_nivel(nivel: str):
        return [row for row in agregados if row.get("nivel") == nivel]

    resumo_lista = por_nivel("GERAL")
    resumo_faturamento = resumo_lista[0] if resumo_lista else {}
    original_convenio_rows = por_nivel("CONVENIO")
    convenio_rows = [
        row for row in original_convenio_rows
        if str(row.get("cd_convenio") or "") != "1"
        and "SUS" not in str(row.get("nm_convenio") or "").upper()
    ]
    sus_total = next(
        (row for row in faturamento_sus_pbix if row.get("grupo") == "SUS_TOTAL"),
        None,
    )
    if sus_total:
        def sus_row(grupo: str):
            return next((row for row in faturamento_sus_pbix if row.get("grupo") == grupo), {})

        valor_estado = float(_json_value(sus_row("ESTADO").get("valor")) or 0)
        pacientes_estado = int(_json_value(sus_row("ESTADO").get("qtd_pacientes")) or 0)
        valor_municipio = float(_json_value(sus_row("MUNICIPIO").get("valor")) or 0)
        pacientes_municipio = int(_json_value(sus_row("MUNICIPIO").get("qtd_pacientes")) or 0)
        valor_sus_combinado = round(valor_estado + valor_municipio, 2)
        pacientes_sus_combinado = pacientes_estado + pacientes_municipio
        faturamento_sus_pbix.append({
            "grupo": "SUS_TOTAL_ESTADO_MUNICIPIO",
            "valor": valor_sus_combinado,
            "qtd_pacientes": pacientes_sus_combinado,
        })

        def append_sus_convenio(chave: str, nome: str, valor: float, pacientes: int):
            if valor <= 0 and pacientes <= 0:
                return
            convenio_rows.append({
                "nivel": "CONVENIO",
                "chave": chave,
                "descricao": nome,
                "tp_atendimento": None,
                "tipo_atendimento": None,
                "cd_convenio": 1,
                "nm_convenio": nome,
                "cd_prestador": None,
                "nm_prestador": None,
                "producao_total": valor,
                "producao_aberta": 0,
                "contas_fechadas": valor,
                "em_remessa": valor,
                "faturado_em_remessa": valor,
                "valor_bruto_original": valor,
                "qtd_itens": 0,
                "qtd_contas": 0,
                "qtd_atendimentos": pacientes,
                "qtd_pacientes": pacientes,
                "qtd_pacientes_em_remessa": pacientes,
                "qtd_itens_pacote": 0,
                "ticket_medio_atendimento": None,
                "ticket_medio_conta": None,
                "perc_faturado_remessa": 1,
                "fonte": "PBIX_FATUSUS",
            })

        append_sus_convenio("SUS_ESTADO_PBIX", "SUS Estado", round(valor_estado, 2), pacientes_estado)
        append_sus_convenio("SUS_MUNICIPIO_PBIX", "SUS Município", round(valor_municipio, 2), pacientes_municipio)

    def pbix_convenio_row(row):
        valor = float(_json_value(row.get("valor")) or 0)
        pacientes = int(_json_value(row.get("qtd_pacientes")) or 0)
        return {
            "nivel": "CONVENIO",
            "chave": str(row.get("cod_convenio") or row.get("grupo") or ""),
            "descricao": row.get("nome_convenio") or row.get("grupo"),
            "tp_atendimento": None,
            "tipo_atendimento": None,
            "cd_convenio": row.get("cod_convenio"),
            "nm_convenio": row.get("nome_convenio") or row.get("grupo"),
            "cd_prestador": None,
            "nm_prestador": None,
            "producao_total": valor,
            "producao_aberta": 0,
            "contas_fechadas": valor,
            "em_remessa": valor,
            "faturado_em_remessa": valor,
            "valor_bruto_original": valor,
            "qtd_itens": 0,
            "qtd_contas": int(_json_value(row.get("qtd_contas")) or 0),
            "qtd_atendimentos": int(_json_value(row.get("qtd_atendimentos")) or 0),
            "qtd_pacientes": pacientes,
            "qtd_pacientes_em_remessa": pacientes,
            "qtd_itens_pacote": 0,
            "ticket_medio_atendimento": None,
            "ticket_medio_conta": None,
            "perc_faturado_remessa": 1,
            "fonte": "PBIX_FATURA_REMESSA",
        }

    faturamento_pbix_por_convenio = [
        pbix_convenio_row(row)
        for row in faturamento_remessa_pbix
    ]

    def append_pbix_sus(chave: str, nome: str, valor: float, pacientes: int):
        if valor <= 0 and pacientes <= 0:
            return
        faturamento_pbix_por_convenio.append(pbix_convenio_row({
            "grupo": chave,
            "cod_convenio": 1,
            "nome_convenio": nome,
            "valor": round(valor, 2),
            "qtd_contas": 0,
            "qtd_atendimentos": pacientes,
            "qtd_pacientes": pacientes,
        }))

    sus_pbix_total_row = next(
        (row for row in faturamento_sus_pbix if row.get("grupo") == "SUS_TOTAL"),
        {},
    )
    sus_pbix_estado_row = next(
        (row for row in faturamento_sus_pbix if row.get("grupo") == "ESTADO"),
        {},
    )
    sus_pbix_municipio_row = next(
        (row for row in faturamento_sus_pbix if row.get("grupo") == "MUNICIPIO"),
        {},
    )
    valor_pbix_estado = float(_json_value(sus_pbix_estado_row.get("valor")) or 0)
    valor_pbix_municipio = float(_json_value(sus_pbix_municipio_row.get("valor")) or 0)
    valor_pbix_sus_total = float(_json_value(sus_pbix_total_row.get("valor")) or 0)
    valor_pbix_sus_residual = round(
        valor_pbix_sus_total - valor_pbix_estado - valor_pbix_municipio,
        2,
    )
    pacientes_pbix_estado = int(_json_value(sus_pbix_estado_row.get("qtd_pacientes")) or 0)
    pacientes_pbix_municipio = int(_json_value(sus_pbix_municipio_row.get("qtd_pacientes")) or 0)
    pacientes_pbix_sus_total = int(_json_value(sus_pbix_total_row.get("qtd_pacientes")) or 0)
    pacientes_pbix_sus_residual = max(
        pacientes_pbix_sus_total - pacientes_pbix_estado - pacientes_pbix_municipio,
        0,
    )
    append_pbix_sus("SUS_PBIX", "SUS", valor_pbix_sus_residual, pacientes_pbix_sus_residual)
    append_pbix_sus("SUS_ESTADO_PBIX", "SUS Estado", valor_pbix_estado, pacientes_pbix_estado)
    append_pbix_sus("SUS_MUNICIPIO_PBIX", "SUS Município", valor_pbix_municipio, pacientes_pbix_municipio)

    resumo_faturamento_pbix = {
        "nivel": "GERAL",
        "chave": "GERAL",
        "descricao": "Geral",
        "producao_total": round(
            sum(float(_json_value(row.get("producao_total")) or 0) for row in faturamento_pbix_por_convenio),
            2,
        ),
        "producao_aberta": 0,
        "contas_fechadas": round(
            sum(float(_json_value(row.get("contas_fechadas")) or 0) for row in faturamento_pbix_por_convenio),
            2,
        ),
        "em_remessa": round(
            sum(float(_json_value(row.get("em_remessa")) or 0) for row in faturamento_pbix_por_convenio),
            2,
        ),
        "faturado_em_remessa": round(
            sum(float(_json_value(row.get("faturado_em_remessa")) or 0) for row in faturamento_pbix_por_convenio),
            2,
        ),
        "qtd_contas": sum(int(_json_value(row.get("qtd_contas")) or 0) for row in faturamento_pbix_por_convenio),
        "qtd_atendimentos": sum(int(_json_value(row.get("qtd_atendimentos")) or 0) for row in faturamento_pbix_por_convenio),
        "qtd_pacientes": sum(int(_json_value(row.get("qtd_pacientes")) or 0) for row in faturamento_pbix_por_convenio),
        "fonte": "PBIX",
    }

    return {
        "periodo": {
            "data_inicio": data_inicio.isoformat(),
            "data_fim": data_fim.isoformat(),
        },
        "resumo_faturamento": resumo_faturamento,
        "resumo_faturamento_pbix": resumo_faturamento_pbix,
        "faturamento_por_tipo_atendimento": por_nivel("TIPO_ATENDIMENTO"),
        "faturamento_por_convenio": convenio_rows,
        "faturamento_pbix_por_convenio": faturamento_pbix_por_convenio,
        "faturamento_remessa_pbix": faturamento_remessa_pbix,
        "faturamento_por_prestador": por_nivel("PRESTADOR"),
        "faturamento_por_tipo_convenio": por_nivel("TIPO_CONVENIO"),
        "faturamento_por_tipo_prestador": por_nivel("TIPO_PRESTADOR"),
        "faturamento_sus_pbix": faturamento_sus_pbix,
        "faturamento": faturamento,
        "total": len(faturamento),
        "limite": limite,
    }


@router.get(
    "/indicadores-hospitalares/convenios",
    status_code=HTTPStatus.OK,
)
def consultar_indicadores_hospitalares_convenios(
    usuario_atual: ValidaUsuarioAtual,
    data_inicio: date,
    data_fim: date,
    session: Session = Depends(get_session_oracle),
):
    del usuario_atual
    rows = _executar_consulta_indicador(
        CONSULTA_CONVENIOS_INDICADORES,
        data_inicio,
        data_fim,
        session,
    )
    convenios = _rows_to_dict(rows)
    return {
        "periodo": {
            "data_inicio": data_inicio.isoformat(),
            "data_fim": data_fim.isoformat(),
        },
        "convenios": convenios,
        "total": len(convenios),
    }


@router.get(
    "/indicadores-hospitalares/procedimentos-cirurgicos",
    status_code=HTTPStatus.OK,
)
def consultar_indicadores_hospitalares_procedimentos_cirurgicos(
    usuario_atual: ValidaUsuarioAtual,
    data_inicio: date,
    data_fim: date,
    cd_convenio: str | None = Query(default=None),
    session: Session = Depends(get_session_oracle),
):
    del usuario_atual
    rows = _executar_consulta_indicador(
        CONSULTA_PROCEDIMENTOS_CIRURGICOS,
        data_inicio,
        data_fim,
        session,
        cd_convenio,
    )
    procedimentos = _rows_to_dict(rows)
    return {
        "periodo": {
            "data_inicio": data_inicio.isoformat(),
            "data_fim": data_fim.isoformat(),
        },
        "procedimentos_cirurgicos": procedimentos,
        "total": len(procedimentos),
    }


@router.get("/indicadores-hospitalares", status_code=HTTPStatus.OK)
def consultar_indicadores_hospitalares(
    usuario_atual: ValidaUsuarioAtual,
    data_inicio: date,
    data_fim: date,
    cd_convenio: str | None = Query(default=None),
    procedimento: str | None = Query(default=None),
    session: Session = Depends(get_session_oracle),
):
    del usuario_atual
    resumo_rows = _executar_consulta_indicador(
        CONSULTA_INDICADORES_HOSPITALARES_RESUMO,
        data_inicio,
        data_fim,
        session,
        cd_convenio,
    )
    series_rows = _executar_consulta_indicador(
        CONSULTA_INDICADORES_HOSPITALARES_SERIES,
        data_inicio,
        data_fim,
        session,
        cd_convenio,
    )
    agenda_rows = _executar_consulta_indicador(
        CONSULTA_AGENDA_AMBULATORIAL,
        data_inicio,
        data_fim,
        session,
        cd_convenio,
    )
    internacao_rows = _executar_consulta_indicador(
        CONSULTA_INTERNACOES_DETALHADAS,
        data_inicio,
        data_fim,
        session,
        cd_convenio,
    )
    producao_rows = _executar_consulta_indicador(
        CONSULTA_PRODUCAO_CIRURGICA,
        data_inicio,
        data_fim,
        session,
        cd_convenio,
        procedimento,
    )
    faturamento_params = _periodo_inclusivo(data_inicio, data_fim, cd_convenio)
    faturamento_params["limite"] = 2000
    try:
        faturamento_rows = session.execute(
            CONSULTA_FATURAMENTO_CONVENIO,
            faturamento_params,
        ).mappings().all()
    except SQLAlchemyError:
        faturamento_rows = []
    convenio_rows = _executar_consulta_indicador(
        CONSULTA_CONVENIOS_INDICADORES,
        data_inicio,
        data_fim,
        session,
    )
    procedimento_rows = _executar_consulta_indicador(
        CONSULTA_PROCEDIMENTOS_CIRURGICOS,
        data_inicio,
        data_fim,
        session,
        cd_convenio,
    )
    fluxo_params = _periodo_inclusivo(data_inicio, data_fim, cd_convenio)
    fluxo_params["limite"] = 1000
    try:
        fluxo_rows = session.execute(
            CONSULTA_FLUXO_PA_TEMPOS,
            fluxo_params,
        ).mappings().all()
    except SQLAlchemyError:
        fluxo_rows = []
    fluxo_ambulatorio_params = _periodo_inclusivo(data_inicio, data_fim, cd_convenio)
    fluxo_ambulatorio_params["limite"] = 1000
    try:
        fluxo_ambulatorio_rows = session.execute(
            CONSULTA_FLUXO_AMBULATORIO_EXAMES,
            fluxo_ambulatorio_params,
        ).mappings().all()
    except SQLAlchemyError:
        fluxo_ambulatorio_rows = []
    exames_procedimentos_params = _periodo_inclusivo(data_inicio, data_fim, cd_convenio)
    exames_procedimentos_params["limite"] = 5000
    try:
        exames_procedimentos_rows = session.execute(
            CONSULTA_EXAMES_PROCEDIMENTOS,
            exames_procedimentos_params,
        ).mappings().all()
    except SQLAlchemyError:
        exames_procedimentos_rows = []

    agenda = _rows_to_dict(agenda_rows)
    internacoes = _rows_to_dict(internacao_rows)
    producao_cirurgica = _rows_to_dict(producao_rows)
    fluxo_pa = _rows_to_dict(fluxo_rows)
    fluxo_ambulatorio = _rows_to_dict(fluxo_ambulatorio_rows)
    exames_procedimentos = _rows_to_dict(exames_procedimentos_rows)
    faturamento = _rows_to_dict(faturamento_rows)
    return {
        "periodo": {
            "data_inicio": data_inicio.isoformat(),
            "data_fim": data_fim.isoformat(),
        },
        "resumo": _rows_to_dict(resumo_rows)[0] if resumo_rows else {},
        "series": _rows_to_dict(series_rows),
        "agenda_ambulatorial": agenda,
        "internacoes_detalhadas": internacoes[:500],
        "fluxo_pa_tempos": fluxo_pa,
        "fluxo_ambulatorio_exames": fluxo_ambulatorio,
        "exames_procedimentos": exames_procedimentos,
        "producao_cirurgica": producao_cirurgica,
        "faturamento": faturamento,
        "convenios": _rows_to_dict(convenio_rows),
        "procedimentos_cirurgicos": _rows_to_dict(procedimento_rows),
        "totais": {
            "agenda_ambulatorial": len(agenda),
            "internacoes_detalhadas": len(internacoes),
            "fluxo_pa_tempos": len(fluxo_pa),
            "fluxo_ambulatorio_exames": len(fluxo_ambulatorio),
            "exames_procedimentos": len(exames_procedimentos),
            "producao_cirurgica": len(producao_cirurgica),
            "faturamento": len(faturamento),
        },
    }
