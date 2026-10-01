from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import threading
import time

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app_prontocardio.database import oracle_engine
from app_prontocardio.biq_hemodinamica import consultar_receita_hemodinamica


router = APIRouter(prefix="/painel-hemodinamica", tags=["painel-hemodinamica"])

SALAS = {"Sala Ge": 3, "Sala Siemens": 4}
CACHE_TTL_SECONDS = 45
_cache: dict[tuple[str, str, str], tuple[float, dict]] = {}
_cache_lock = threading.Lock()

SQL_PRODUCAO = text(
    """
    WITH avisos AS (
        SELECT
            ac.cd_aviso_cirurgia,
            TRUNC(NVL(ac.dt_sugerida, ac.dt_aviso_cirurgia)) AS dia,
            ac.tp_situacao AS status
        FROM dbamv.aviso_cirurgia ac
        WHERE ac.cd_cen_cir = 2
          AND ac.tp_situacao <> 'P'
          AND NVL(ac.dt_sugerida, ac.dt_aviso_cirurgia) >= :data_inicio
          AND NVL(ac.dt_sugerida, ac.dt_aviso_cirurgia) < :data_fim_exclusiva
          AND (:cd_sal_cir IS NULL OR ac.cd_sal_cir = :cd_sal_cir)
    ), procedimentos AS (
        SELECT
            ac.cd_aviso_cirurgia,
            TRUNC(NVL(ac.dt_sugerida, ac.dt_aviso_cirurgia)) AS dia,
            COUNT(ca.cd_cirurgia_aviso) AS quantidade
        FROM dbamv.aviso_cirurgia ac
        LEFT JOIN dbamv.cirurgia_aviso ca
          ON ca.cd_aviso_cirurgia = ac.cd_aviso_cirurgia
        WHERE ac.cd_cen_cir = 2
          AND ac.tp_situacao <> 'P'
          AND NVL(ac.dt_sugerida, ac.dt_aviso_cirurgia) >= :data_inicio
          AND NVL(ac.dt_sugerida, ac.dt_aviso_cirurgia) < :data_fim_exclusiva
          AND (:cd_sal_cir IS NULL OR ac.cd_sal_cir = :cd_sal_cir)
        GROUP BY ac.cd_aviso_cirurgia, TRUNC(NVL(ac.dt_sugerida, ac.dt_aviso_cirurgia))
    )
    SELECT
        a.dia,
        SUM(CASE WHEN a.status = 'G' THEN 1 ELSE 0 END) AS agendados,
        SUM(CASE WHEN a.status = 'R' THEN 1 ELSE 0 END) AS realizados,
        SUM(CASE WHEN a.status = 'C' THEN 1 ELSE 0 END) AS cancelados,
        COUNT(*) AS prontuarios,
        SUM(NVL(p.quantidade, 0)) AS qtd_procedimentos
    FROM avisos a
    LEFT JOIN procedimentos p ON p.cd_aviso_cirurgia = a.cd_aviso_cirurgia
    GROUP BY a.dia
    ORDER BY a.dia
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
          AND NVL(ac.dt_sugerida, ac.dt_aviso_cirurgia) >= :data_inicio
          AND NVL(ac.dt_sugerida, ac.dt_aviso_cirurgia) < :data_fim_exclusiva
          AND (:cd_sal_cir IS NULL OR ac.cd_sal_cir = :cd_sal_cir)
        GROUP BY NVL(c.ds_cirurgia, 'SEM PROCEDIMENTO')
        ORDER BY quantidade DESC
    ) WHERE ROWNUM <= 8
    """
)

SQL_PACIENTES = text(
    """
    WITH prestador_principal AS (
        SELECT cd_aviso_cirurgia, nm_prestador
        FROM (
            SELECT
                pa.cd_aviso_cirurgia,
                pr.nm_prestador,
                ROW_NUMBER() OVER (
                    PARTITION BY pa.cd_aviso_cirurgia
                    ORDER BY CASE WHEN pa.sn_principal = 'S' THEN 0 ELSE 1 END,
                             pa.cd_prestador
                ) AS rn
            FROM dbamv.prestador_aviso pa
            LEFT JOIN dbamv.prestador pr
              ON pr.cd_prestador = pa.cd_prestador
        )
        WHERE rn = 1
    )
    SELECT * FROM (
        SELECT
            ac.cd_aviso_cirurgia,
            NVL(ac.dt_sugerida, ac.dt_aviso_cirurgia) AS dt_aviso_cirurgia,
            NVL(p.nm_paciente, ac.nm_paciente) AS paciente,
            p.dt_nascimento,
            CASE
                WHEN ui.ds_unid_int IS NOT NULL AND l.ds_leito IS NOT NULL
                    THEN ui.ds_unid_int || ' - ' || l.ds_leito
                WHEN l.ds_leito IS NOT NULL THEN l.ds_leito
                ELSE 'EXTERNO / SEM LEITO'
            END AS local,
            NVL(c.ds_cirurgia, 'PROCEDIMENTO NÃO INFORMADO') AS procedimento,
            CASE ac.tp_situacao
                WHEN 'R' THEN 'Realizada'
                WHEN 'C' THEN 'Cancelada'
                WHEN 'G' THEN 'Agendada'
                WHEN 'P' THEN 'Agendada'
                ELSE 'Em acompanhamento'
            END AS status,
            NVL(s.ds_sal_cir, 'SALA NÃO INFORMADA') AS sala,
            NVL(pp.nm_prestador, 'NÃO INFORMADO') AS prestador,
            NVL(co.nm_convenio, 'NÃO INFORMADO') AS convenio,
            NVL(va.ds_via_de_acesso, 'NÃO INFORMADA') AS via_acesso
        FROM dbamv.aviso_cirurgia ac
        LEFT JOIN dbamv.atendime a
          ON a.cd_atendimento = ac.cd_atendimento
        LEFT JOIN dbamv.paciente p
          ON p.cd_paciente = NVL(ac.cd_paciente, a.cd_paciente)
        LEFT JOIN dbamv.leito l
          ON l.cd_leito = a.cd_leito
        LEFT JOIN dbamv.unid_int ui
          ON ui.cd_unid_int = l.cd_unid_int
        LEFT JOIN dbamv.sal_cir s
          ON s.cd_sal_cir = ac.cd_sal_cir
        LEFT JOIN dbamv.cirurgia_aviso ca
          ON ca.cd_aviso_cirurgia = ac.cd_aviso_cirurgia
        LEFT JOIN dbamv.cirurgia c
          ON c.cd_cirurgia = ca.cd_cirurgia
        LEFT JOIN dbamv.convenio co
          ON co.cd_convenio = NVL(ca.cd_convenio, a.cd_convenio)
        LEFT JOIN dbamv.via_de_acesso va
          ON va.cd_via_de_acesso = NVL(ca.cd_via_de_acesso, c.cd_via_de_acesso)
        LEFT JOIN prestador_principal pp
          ON pp.cd_aviso_cirurgia = ac.cd_aviso_cirurgia
        WHERE ac.cd_cen_cir = 2
          AND ac.tp_situacao <> 'P'
          AND NVL(ac.dt_sugerida, ac.dt_aviso_cirurgia) >= :data_inicio
          AND NVL(ac.dt_sugerida, ac.dt_aviso_cirurgia) < :data_fim_exclusiva
          AND (:cd_sal_cir IS NULL OR ac.cd_sal_cir = :cd_sal_cir)
        ORDER BY NVL(ac.dt_sugerida, ac.dt_aviso_cirurgia) DESC, ac.cd_aviso_cirurgia, c.ds_cirurgia
    ) WHERE ROWNUM <= 1500
    """
)


def _number(value):
    return float(value) if isinstance(value, Decimal) else value


@router.get("/health")
def health():
    return {"status": "ok", "servico": "painel-hemodinamica"}


@router.get("/resumo")
def resumo(
    data_inicio: date,
    data_fim: date,
    sala: str = Query(default="todas"),
):
    if data_fim < data_inicio:
        raise HTTPException(status_code=422, detail="Período inválido")
    if (data_fim - data_inicio).days > 370:
        raise HTTPException(status_code=422, detail="O período máximo é de 370 dias")
    if sala != "todas" and sala not in SALAS:
        raise HTTPException(status_code=422, detail="Sala inválida")

    cache_key = (data_inicio.isoformat(), data_fim.isoformat(), sala)
    now = time.monotonic()
    with _cache_lock:
        cached = _cache.get(cache_key)
        if cached and now - cached[0] < CACHE_TTL_SECONDS:
            return cached[1]

    params = {
        "data_inicio": data_inicio,
        "data_fim_exclusiva": data_fim + timedelta(days=1),
        "cd_sal_cir": SALAS.get(sala),
    }
    try:
        with Session(oracle_engine) as session:
            daily = session.execute(SQL_PRODUCAO, params).mappings().all()
            procedures = session.execute(SQL_PROCEDIMENTOS, params).mappings().all()
            patient_rows = session.execute(SQL_PACIENTES, params).mappings().all()
            revenue = consultar_receita_hemodinamica(session, data_inicio, data_fim)
    except SQLAlchemyError as exc:
        raise HTTPException(status_code=503, detail="Oracle indisponível") from exc

    series = [
        {
            "data": row["dia"].strftime("%d/%m"),
            "agendados": int(_number(row["agendados"]) or 0),
            "realizados": int(_number(row["realizados"]) or 0),
            "cancelados": int(_number(row["cancelados"]) or 0),
            "prontuarios": int(_number(row["prontuarios"]) or 0),
            "procedimentos": int(_number(row["qtd_procedimentos"]) or 0),
        }
        for row in daily
    ]
    totals = {
        key: sum(item[key] for item in series)
        for key in ("agendados", "realizados", "cancelados")
    }
    denominator = totals["realizados"] + totals["cancelados"]
    totals["taxa_realizacao"] = round(
        totals["realizados"] * 100 / denominator, 1
    ) if denominator else 0.0
    totals["receita"] = float(revenue["receita_total"])

    revenue_by_name = {
        item["procedimento"]: item["receita"]
        for item in revenue["receita_por_procedimento"]
    }
    patients_by_notice = {}
    for row in patient_rows:
        notice_id = int(_number(row["cd_aviso_cirurgia"]))
        if notice_id not in patients_by_notice:
            patients_by_notice[notice_id] = {
                "id": notice_id,
                "data": row["dt_aviso_cirurgia"].isoformat() if row["dt_aviso_cirurgia"] else None,
                "paciente": str(row["paciente"] or "PACIENTE NÃO INFORMADO").strip(),
                "data_nascimento": row["dt_nascimento"].date().isoformat() if row["dt_nascimento"] else None,
                "local": str(row["local"] or "-").strip(),
                "procedimentos": [],
                "status": row["status"],
                "sala": str(row["sala"] or "-").strip(),
                "prestador": str(row["prestador"] or "-").strip(),
                "convenio": str(row["convenio"] or "-").strip(),
                "via_acesso": str(row["via_acesso"] or "-").strip(),
            }
        procedure = str(row["procedimento"] or "").strip()
        if procedure and procedure not in patients_by_notice[notice_id]["procedimentos"]:
            patients_by_notice[notice_id]["procedimentos"].append(procedure)
    payload = {
        "atualizado_em": datetime.now(timezone.utc).isoformat(),
        "periodo": {"data_inicio": data_inicio, "data_fim": data_fim},
        "sala": sala,
        "totais": totals,
        "serie_diaria": series,
        "procedimentos": [
            {
                "procedimento": row["procedimento"],
                "quantidade": int(_number(row["quantidade"]) or 0),
                "receita": float(revenue_by_name.get(row["procedimento"], 0)),
            }
            for row in procedures
        ],
        "pacientes": list(patients_by_notice.values()),
        "pacientes_limitado": len(patient_rows) >= 1500,
    }
    with _cache_lock:
        _cache[cache_key] = (time.monotonic(), payload)
        if len(_cache) > 100:
            oldest = min(_cache, key=lambda key: _cache[key][0])
            _cache.pop(oldest, None)
    return payload
