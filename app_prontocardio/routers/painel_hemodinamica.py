from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app_prontocardio.biq_hemodinamica import consultar_receita_hemodinamica
from app_prontocardio.database import oracle_engine

router = APIRouter(prefix='/painel-hemodinamica', tags=['painel-hemodinamica'])

SALAS = {'Sala Ge': 3, 'Sala Siemens': 4}
PERIODO_MAXIMO_DIAS = 370

SQL_PRODUCAO = text(
    """
    WITH base AS (
        SELECT
            TRUNC(ac.dt_aviso_cirurgia) AS dia,
            ac.tp_situacao AS status,
            NVL(c.ds_cirurgia, 'SEM PROCEDIMENTO') AS procedimento
        FROM dbamv.aviso_cirurgia ac
        LEFT JOIN dbamv.cirurgia_aviso ca
          ON ca.cd_aviso_cirurgia = ac.cd_aviso_cirurgia
        LEFT JOIN dbamv.cirurgia c
          ON c.cd_cirurgia = ca.cd_cirurgia
        WHERE ac.cd_cen_cir = 2
          AND ac.dt_aviso_cirurgia >= :data_inicio
          AND ac.dt_aviso_cirurgia < :data_fim_exclusiva
          AND (:cd_sal_cir IS NULL OR ac.cd_sal_cir = :cd_sal_cir)
    )
    SELECT
        dia,
        SUM(CASE WHEN status IN ('G', 'P') THEN 1 ELSE 0 END) AS agendados,
        SUM(CASE WHEN status = 'R' THEN 1 ELSE 0 END) AS realizados,
        SUM(CASE WHEN status = 'C' THEN 1 ELSE 0 END) AS cancelados
    FROM base
    GROUP BY dia
    ORDER BY dia
    """
)

SQL_PROCEDIMENTOS = text(
    """
    SELECT * FROM (
        SELECT
            NVL(c.ds_cirurgia, 'SEM PROCEDIMENTO') AS procedimento,
            COUNT(DISTINCT ac.cd_aviso_cirurgia) AS quantidade
        FROM dbamv.aviso_cirurgia ac
        JOIN dbamv.cirurgia_aviso ca
          ON ca.cd_aviso_cirurgia = ac.cd_aviso_cirurgia
        LEFT JOIN dbamv.cirurgia c
          ON c.cd_cirurgia = ca.cd_cirurgia
        WHERE ac.cd_cen_cir = 2
          AND ac.tp_situacao = 'R'
          AND ac.dt_aviso_cirurgia >= :data_inicio
          AND ac.dt_aviso_cirurgia < :data_fim_exclusiva
          AND (:cd_sal_cir IS NULL OR ac.cd_sal_cir = :cd_sal_cir)
        GROUP BY NVL(c.ds_cirurgia, 'SEM PROCEDIMENTO')
        ORDER BY quantidade DESC
    ) WHERE ROWNUM <= 8
    """
)


def _number(value):
    return float(value) if isinstance(value, Decimal) else value


@router.get('/health')
def health():
    return {'status': 'ok', 'servico': 'painel-hemodinamica'}


@router.get('/resumo')
def resumo(
    data_inicio: date,
    data_fim: date,
    sala: str = Query(default='todas'),
):
    if data_fim < data_inicio:
        raise HTTPException(status_code=422, detail='Período inválido')
    if (data_fim - data_inicio).days > PERIODO_MAXIMO_DIAS:
        raise HTTPException(
            status_code=422, detail='O período máximo é de 370 dias'
        )
    if sala != 'todas' and sala not in SALAS:
        raise HTTPException(status_code=422, detail='Sala inválida')

    params = {
        'data_inicio': data_inicio,
        'data_fim_exclusiva': data_fim + timedelta(days=1),
        'cd_sal_cir': SALAS.get(sala),
    }
    try:
        with Session(oracle_engine) as session:
            daily = session.execute(SQL_PRODUCAO, params).mappings().all()
            procedures = (
                session.execute(SQL_PROCEDIMENTOS, params).mappings().all()
            )
            revenue = consultar_receita_hemodinamica(
                session, data_inicio, data_fim
            )
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=503, detail='Oracle indisponível'
        ) from exc

    series = [
        {
            'data': row['dia'].strftime('%d/%m'),
            'agendados': int(_number(row['agendados']) or 0),
            'realizados': int(_number(row['realizados']) or 0),
            'cancelados': int(_number(row['cancelados']) or 0),
        }
        for row in daily
    ]
    totals = {
        key: sum(item[key] for item in series)
        for key in ('agendados', 'realizados', 'cancelados')
    }
    denominator = totals['realizados'] + totals['cancelados']
    totals['taxa_realizacao'] = (
        round(totals['realizados'] * 100 / denominator, 1)
        if denominator
        else 0.0
    )
    totals['receita'] = float(revenue['receita_total'])

    revenue_by_name = {
        item['procedimento']: item['receita']
        for item in revenue['receita_por_procedimento']
    }
    return {
        'atualizado_em': datetime.now(timezone.utc).isoformat(),
        'periodo': {'data_inicio': data_inicio, 'data_fim': data_fim},
        'sala': sala,
        'totais': totals,
        'serie_diaria': series,
        'procedimentos': [
            {
                'procedimento': row['procedimento'],
                'quantidade': int(_number(row['quantidade']) or 0),
                'receita': float(revenue_by_name.get(row['procedimento'], 0)),
            }
            for row in procedures
        ],
    }
