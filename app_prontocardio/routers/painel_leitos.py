import logging
import threading
import unicodedata
from datetime import datetime
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app_prontocardio.database import get_session_oracle


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/painel-leitos", tags=["painel-leitos"])

_cache_lock = threading.Lock()
_cached_payload: dict[str, Any] | None = None
_cached_at: datetime | None = None
_cache_seconds = 12


LEITOS_SQL = """
SELECT ui.cd_unid_int,
       ui.ds_unid_int,
       ui.ds_localizacao,
       l.cd_leito,
       l.ds_leito,
       l.ds_enfermaria,
       l.tp_ocupacao,
       l.tp_sexo AS tp_sexo_leito,
       l.sn_extra,
       a.cd_atendimento,
       a.dt_atendimento,
       p.nm_paciente,
       p.tp_sexo AS tp_sexo_paciente,
       p.dt_nascimento
  FROM dbamv.leito l
  JOIN dbamv.unid_int ui
    ON ui.cd_unid_int = l.cd_unid_int
  LEFT JOIN dbamv.atendime a
    ON a.cd_leito = l.cd_leito
   AND a.dt_alta IS NULL
   AND NVL(a.sn_internado, 'S') = 'S'
  LEFT JOIN dbamv.paciente p
    ON p.cd_paciente = a.cd_paciente
 WHERE l.tp_situacao = 'A'
   AND NVL(ui.sn_ativo, 'S') = 'S'
 ORDER BY ui.ds_unid_int, l.ds_leito
"""


def _value(row: Any, name: str) -> Any:
    for candidate in (name, name.lower(), name.upper()):
        if candidate in row:
            return row[candidate]
    return None


def _plain(value: Any) -> Any:
    if isinstance(value, Decimal):
        return int(value)
    return value


def _unit_id(value: str) -> str:
    normalized = unicodedata.normalize("NFD", value or "")
    ascii_value = "".join(
        character
        for character in normalized
        if unicodedata.category(character) != "Mn"
    )
    return "".join(character.lower() for character in ascii_value if character.isalnum())


def _initials(name: str | None) -> str | None:
    if not name:
        return None
    parts = [part for part in name.strip().split() if part]
    if not parts:
        return None
    selected = parts[:1] if len(parts) == 1 else [parts[0], parts[-1]]
    return " ".join(f"{part[0].upper()}." for part in selected)


def _age(birth_date: datetime | None, now: datetime) -> int | None:
    if not birth_date:
        return None
    years = now.year - birth_date.year
    if (now.month, now.day) < (birth_date.month, birth_date.day):
        years -= 1
    return max(0, years)


def _stay_days(admission_date: datetime | None, now: datetime) -> int | None:
    if not admission_date:
        return None
    return max(0, (now.date() - admission_date.date()).days)


def _build_payload(rows: list[Any]) -> dict[str, Any]:
    now = datetime.now()
    grouped: dict[str, dict[str, Any]] = {}

    for row in rows:
        unit_name = str(_value(row, "ds_unid_int") or "Unidade sem nome").strip()
        unit_id = _unit_id(unit_name)
        unit = grouped.setdefault(
            unit_id,
            {
                "id": unit_id,
                "name": unit_name,
                "floor": str(
                    _value(row, "ds_localizacao") or "Localização não informada"
                ).strip(),
                "beds": [],
            },
        )

        occupied = str(_value(row, "tp_ocupacao") or "V").upper() == "O"
        patient_gender = str(_value(row, "tp_sexo_paciente") or "").upper()
        if patient_gender not in {"M", "F"}:
            patient_gender = None

        unit["beds"].append(
            {
                "code": str(
                    _value(row, "ds_leito") or _value(row, "cd_leito") or ""
                ).strip(),
                "status": "occupied" if occupied else "available",
                "patient": _initials(_value(row, "nm_paciente")) if occupied else None,
                "age": _age(_value(row, "dt_nascimento"), now) if occupied else None,
                "stay": _stay_days(_value(row, "dt_atendimento"), now) if occupied else None,
                "gender": patient_gender if occupied else None,
                "extra": str(_value(row, "sn_extra") or "N").upper() == "S",
                "bedId": _plain(_value(row, "cd_leito")),
                "admissionId": (
                    _plain(_value(row, "cd_atendimento")) if occupied else None
                ),
            }
        )

    units = list(grouped.values())
    total = sum(len(unit["beds"]) for unit in units)
    occupied = sum(
        1
        for unit in units
        for bed in unit["beds"]
        if bed["status"] == "occupied"
    )
    return {
        "source": "soulmv",
        "updatedAt": now.isoformat(),
        "totals": {"units": len(units), "beds": total, "occupied": occupied},
        "units": units,
    }


@router.get("/situacao")
def situacao_leitos(
    session_oracle: Session = Depends(get_session_oracle),
) -> dict[str, Any]:
    global _cached_payload, _cached_at
    now = datetime.now()

    with _cache_lock:
        if (
            _cached_payload is not None
            and _cached_at is not None
            and (now - _cached_at).total_seconds() < _cache_seconds
        ):
            return _cached_payload

    try:
        rows = list(session_oracle.execute(text(LEITOS_SQL)).mappings())
        payload = _build_payload(rows)
    except SQLAlchemyError as exc:
        logger.warning("Falha na consulta dos leitos do Soul MV", exc_info=True)
        raise HTTPException(
            status_code=503,
            detail="Soul MV temporariamente indisponível para o painel de leitos.",
        ) from exc

    with _cache_lock:
        _cached_payload = payload
        _cached_at = now
    return payload


@router.get("/health")
def health(session_oracle: Session = Depends(get_session_oracle)) -> dict[str, Any]:
    try:
        active_beds = session_oracle.execute(
            text("SELECT COUNT(1) FROM dbamv.leito WHERE tp_situacao = 'A'")
        ).scalar_one()
        return {
            "status": "ok",
            "database": "online",
            "source": "soulmv",
            "activeBeds": _plain(active_beds),
        }
    except SQLAlchemyError as exc:
        logger.warning("Falha no healthcheck do painel de leitos", exc_info=True)
        raise HTTPException(
            status_code=503,
            detail="Oracle temporariamente indisponível.",
        ) from exc
