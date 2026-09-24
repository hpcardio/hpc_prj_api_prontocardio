from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.orm import Session


CD_SETOR_HEMODINAMICA = 26
LIMITE_PROCEDIMENTOS = 12


CONSULTA_RECEITA_HEMODINAMICA = text(
    """
    WITH receita_agrupada AS (
        SELECT
            NVL(pf.ds_pro_fat, 'SEM PROCEDIMENTO') AS procedimento,
            SUM(
                CASE
                    WHEN NVL(irf.sn_pertence_pacote, 'N') = 'S' THEN 0
                    ELSE NVL(irf.vl_total_conta, 0)
                END
            ) AS receita,
            SUM(
                CASE
                    WHEN NVL(irf.sn_pertence_pacote, 'N') = 'S' THEN 0
                    ELSE NVL(irf.qt_lancamento, 0)
                END
            ) AS quantidade,
            COUNT(
                DISTINCT CASE
                    WHEN NVL(irf.sn_pertence_pacote, 'N') <> 'S' THEN rf.cd_atendimento
                END
            ) AS atendimentos
        FROM dbamv.itreg_fat irf
        JOIN dbamv.reg_fat rf
          ON rf.cd_reg_fat = irf.cd_reg_fat
        JOIN dbamv.atendime a
          ON a.cd_atendimento = rf.cd_atendimento
        LEFT JOIN dbamv.pro_fat pf
          ON pf.cd_pro_fat = irf.cd_pro_fat
        WHERE irf.dt_lancamento >= :data_inicio
          AND irf.dt_lancamento < :data_fim_exclusiva
          AND irf.cd_setor = :cd_setor_hemodinamica
          AND (
              :cd_convenio IS NULL
              OR INSTR(
                  ',' || :cd_convenio || ',',
                  ',' || TO_CHAR(NVL(rf.cd_convenio, a.cd_convenio)) || ','
              ) > 0
          )
          AND (
              :procedimento IS NULL
              OR EXISTS (
                  SELECT 1
                  FROM dbamv.aviso_cirurgia ac
                  JOIN dbamv.cirurgia_aviso ca
                    ON ca.cd_aviso_cirurgia = ac.cd_aviso_cirurgia
                  JOIN dbamv.cirurgia c
                    ON c.cd_cirurgia = ca.cd_cirurgia
                  WHERE ac.cd_atendimento = rf.cd_atendimento
                    AND ac.tp_situacao = 'R'
                    AND c.ds_cirurgia = :procedimento
              )
          )
        GROUP BY NVL(pf.ds_pro_fat, 'SEM PROCEDIMENTO')
    ), ranking AS (
        SELECT
            procedimento,
            receita,
            quantidade,
            atendimentos,
            SUM(receita) OVER () AS receita_total,
            ROW_NUMBER() OVER (ORDER BY receita DESC, procedimento) AS posicao
        FROM receita_agrupada
        WHERE receita <> 0
    )
    SELECT
        procedimento,
        receita,
        quantidade,
        atendimentos,
        receita_total
    FROM ranking
    WHERE posicao <= :limite
    ORDER BY posicao
    """
)


def _number(value):
    if isinstance(value, Decimal):
        return float(value)
    return value


def consultar_receita_hemodinamica(
    session: Session,
    data_inicio: date,
    data_fim: date,
    cd_convenio: str | None = None,
    procedimento: str | None = None,
):
    params = {
        "data_inicio": data_inicio,
        "data_fim_exclusiva": data_fim + timedelta(days=1),
        "cd_convenio": cd_convenio or None,
        "procedimento": procedimento or None,
        "cd_setor_hemodinamica": CD_SETOR_HEMODINAMICA,
        "limite": LIMITE_PROCEDIMENTOS,
    }
    rows = session.execute(CONSULTA_RECEITA_HEMODINAMICA, params).mappings().all()
    receita_total = float(_number(rows[0].get("receita_total")) or 0) if rows else 0.0

    return {
        "periodo": {
            "data_inicio": data_inicio.isoformat(),
            "data_fim": data_fim.isoformat(),
        },
        "cd_setor": CD_SETOR_HEMODINAMICA,
        "receita_total": receita_total,
        "receita_por_procedimento": [
            {
                "procedimento": row.get("procedimento"),
                "receita": float(_number(row.get("receita")) or 0),
                "quantidade": float(_number(row.get("quantidade")) or 0),
                "atendimentos": int(_number(row.get("atendimentos")) or 0),
            }
            for row in rows
        ],
    }
