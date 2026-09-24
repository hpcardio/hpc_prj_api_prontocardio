import logging
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app_prontocardio.database import get_session_oracle
from app_prontocardio.oracle_resilience import executar_consulta_painel
from app_prontocardio.panel_cache import CacheBusyError, SingleFlightTTLCache

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/painel-senhas-soulmv-emergencia",
    tags=["painel-senhas-soulmv-emergencia"],
)

CALL_TYPE_LABELS = {
    10: "Classificacao",
    20: "Cadastro",
    30: "Atendimento medico",
}

EXAM_DESTINATION_TERMS = (
    "EXAME",
    "TOMOGRAFIA",
)

PANEL_SQL = """
SELECT event_id,
       called_at,
       ticket,
       destination,
       queue_id,
       process_type,
       priority,
       patient_name,
       risk_classification,
       match_order
  FROM (
    SELECT matched_events.*,
           ROW_NUMBER() OVER (
               PARTITION BY CASE
                   WHEN queue_id = 10
                     OR REGEXP_LIKE(
                       UPPER(destination),
                       'EXAME|TOMOGRAFIA'
                   )
                   THEN 3
                   WHEN process_type IN (10, 20)
                     OR REGEXP_LIKE(UPPER(destination), 'CLASSIFICA')
                   THEN 1
                   ELSE 2
               END
               ORDER BY called_at DESC
           ) segment_order
      FROM (
        SELECT event_id,
               called_at,
               ticket,
               destination,
               queue_id,
               process_type,
               priority,
               patient_name,
               risk_classification,
               ROW_NUMBER() OVER (
                   PARTITION BY event_id
                   ORDER BY match_quality, triage_at DESC NULLS LAST
               ) match_order
          FROM (
            SELECT /* PAINEL_SENHAS_SOULMV_EMERGENCIA */
                   TO_CHAR(p.cd_tempo_processo) event_id,
                   p.dh_processo called_at,
                   COALESCE(
                       NULLIF(TRIM(t.ds_senha), ''),
                       NULLIF(TRIM(a.nr_chamada_painel), ''),
                       'SEM SENHA'
                   ) ticket,
                   COALESCE(
                       NULLIF(TRIM(p.nm_local_estacao), ''),
                       'AGUARDE'
                   ) destination,
                   t.cd_fila_senha queue_id,
                   p.cd_tipo_tempo_processo process_type,
                   CASE
                       WHEN NVL(t.sn_prioridade_especial, 'N') = 'S'
                         OR NVL(t.sn_prioridade_classificacao, 'N') = 'S'
                         OR NVL(t.sn_prioridade_octo, 'N') = 'S'
                       THEN 1
                       ELSE 0
                   END priority,
                   COALESCE(
                       DECODE(
                           pac.sn_utiliza_nome_social,
                           'S',
                           NVL(pac.nm_social_paciente, pac.nm_paciente),
                           pac.nm_paciente
                       ),
                       NULLIF(TRIM(t.nm_paciente), '')
                   ) patient_name,
                   sc.ds_tipo_risco risk_classification,
                   t.dh_pre_atendimento triage_at,
                   CASE
                       WHEN p.cd_triagem_atendimento = t.cd_triagem_atendimento
                       THEN 0
                       ELSE 1
                   END match_quality
              FROM dbamv.sacr_tempo_processo p
              LEFT JOIN dbamv.triagem_atendimento t
                ON t.cd_triagem_atendimento = p.cd_triagem_atendimento
                OR (
                    p.cd_triagem_atendimento IS NULL
                    AND p.cd_atendimento IS NOT NULL
                    AND t.cd_atendimento = p.cd_atendimento
                )
              LEFT JOIN dbamv.sacr_classificacao sc
                ON sc.cd_classificacao = t.cd_classificacao
              LEFT JOIN dbamv.atendime a
                ON a.cd_atendimento = p.cd_atendimento
              LEFT JOIN dbamv.paciente pac
                ON pac.cd_paciente = a.cd_paciente
             WHERE p.cd_tipo_tempo_processo IN (10, 20, 30)
               AND p.dh_processo >= SYSDATE - (:window_minutes / 1440)
               AND t.cd_fila_senha IN (1, 10, 11)
          )
      ) matched_events
     WHERE matched_events.match_order = 1
  )
 WHERE segment_order <= 16
 ORDER BY called_at DESC
"""


class PanelEvent(BaseModel):
    id: str
    ticket: str
    destination: str
    queue_id: int
    service: str
    called_at: datetime
    priority: bool
    process_type: int
    patient_name: str | None = None
    risk_classification: str | None = None


class PanelResponse(BaseModel):
    source: str
    database_status: str
    generated_at: datetime
    current: PanelEvent | None
    recent: list[PanelEvent]
    message: str | None = None


panel_cache = SingleFlightTTLCache[int, PanelResponse](
    ttl_seconds=3,
    stale_seconds=60,
    wait_seconds=5,
)


def _value(row: Any, name: str) -> Any:
    for candidate in (name, name.lower(), name.upper()):
        if candidate in row:
            return row[candidate]
    return None


def _service_label(process_type: int, destination: str, queue_id: int) -> str:
    normalized_destination = destination.upper().replace("-", " ")
    if queue_id == 10 or any(
        term in normalized_destination for term in EXAM_DESTINATION_TERMS
    ):
        return "Exame ou procedimento"
    return CALL_TYPE_LABELS.get(process_type, "Atendimento")


def _to_event(row: Any) -> PanelEvent:
    process_type = int(_value(row, "process_type"))
    queue_id = int(_value(row, "queue_id"))
    destination = str(_value(row, "destination") or "AGUARDE").strip()
    patient_name = _value(row, "patient_name")
    risk_classification = _value(row, "risk_classification")
    return PanelEvent(
        id=str(_value(row, "event_id")),
        ticket=str(_value(row, "ticket") or "SEM SENHA").strip(),
        destination=destination,
        queue_id=queue_id,
        service=_service_label(process_type, destination, queue_id),
        called_at=_value(row, "called_at"),
        priority=bool(_value(row, "priority")),
        process_type=process_type,
        patient_name=str(patient_name).strip() if patient_name else None,
        risk_classification=(
            str(risk_classification).strip().upper()
            if risk_classification
            else None
        ),
    )


@router.get("/health")
def health(session_oracle: Session = Depends(get_session_oracle)) -> dict:
    try:
        session_oracle.execute(text("SELECT 1 FROM dual")).scalar_one()
        return {"status": "ok", "database": "online"}
    except SQLAlchemyError as exc:
        logger.warning("Falha no healthcheck SoulMV Emergencia", exc_info=True)
        raise HTTPException(
            status_code=503,
            detail="Oracle temporariamente indisponivel.",
        ) from exc


@router.get("/painel", response_model=PanelResponse)
def painel(
    window_minutes: int = Query(default=480, ge=5, le=1440),
    session_oracle: Session = Depends(get_session_oracle),
) -> PanelResponse:
    def load_panel() -> PanelResponse:
        rows = executar_consulta_painel(
            session_oracle,
            text(PANEL_SQL),
            {"window_minutes": window_minutes},
        )
        events = [_to_event(row) for row in rows]

        return PanelResponse(
            source="live",
            database_status="online",
            generated_at=datetime.now(),
            current=events[0] if events else None,
            recent=events[1:],
        )

    try:
        return panel_cache.get_or_load(window_minutes, load_panel)
    except (SQLAlchemyError, CacheBusyError) as exc:
        logger.warning("Falha na consulta SoulMV Emergencia", exc_info=True)
        raise HTTPException(
            status_code=503,
            detail="Oracle temporariamente indisponivel.",
        ) from exc
