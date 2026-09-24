"""Sync MV tomography tickets into the NVC AngioTC triage queue.

Runs inside the api-prontocardio container, where Oracle access is already
configured. The NVC endpoint is idempotent by active MV attendance number.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime

from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session

from app_prontocardio.database import oracle_engine


DEFAULT_QUEUE_IDS = "8"
DEFAULT_LOOKBACK_MINUTES = "360"
DEFAULT_NVC_URL = "http://192.168.4.45:4003/api/angiotc/triage-entry"
DEFAULT_STATE_FILE = "/tmp/nvc_angiotc_triage_sync_seen.json"


def _env_int(name: str, default: str) -> int:
    try:
        return int(os.getenv(name, default))
    except ValueError:
        return int(default)


def _queue_ids() -> list[int]:
    raw = os.getenv("ANGIOTC_MV_QUEUE_IDS", DEFAULT_QUEUE_IDS)
    ids: list[int] = []
    for value in raw.split(","):
        value = value.strip()
        if value:
            ids.append(int(value))
    return ids or [8]


def _load_recent_tickets() -> list[dict]:
    queue_ids = _queue_ids()
    lookback_minutes = _env_int("ANGIOTC_TRIAGE_LOOKBACK_MINUTES", DEFAULT_LOOKBACK_MINUTES)
    sql = text(
        """
        SELECT t.cd_triagem_atendimento,
               TRIM(t.ds_senha) ds_senha,
               t.cd_fila_senha,
               t.dh_pre_atendimento,
               t.cd_atendimento,
               t.cd_paciente
          FROM dbamv.triagem_atendimento t
         WHERE t.cd_fila_senha IN :queue_ids
           AND t.dh_pre_atendimento >= SYSDATE - (:lookback_minutes / 1440)
           AND t.cd_atendimento IS NOT NULL
           AND UPPER(TRIM(t.ds_senha)) LIKE 'PTM%'
         ORDER BY t.dh_pre_atendimento ASC
        """
    ).bindparams(bindparam("queue_ids", expanding=True))

    with Session(oracle_engine) as session:
        return [
            dict(row)
            for row in session.execute(
                sql,
                {"queue_ids": queue_ids, "lookback_minutes": lookback_minutes},
            ).mappings()
        ]


def _state_path() -> str:
    return os.getenv("ANGIOTC_TRIAGE_STATE_FILE", DEFAULT_STATE_FILE)


def _load_seen() -> set[str]:
    try:
        with open(_state_path(), "r", encoding="utf-8") as handle:
            values = json.load(handle)
        if isinstance(values, list):
            return {str(value) for value in values}
    except FileNotFoundError:
        return set()
    except Exception:
        return set()
    return set()


def _save_seen(values: set[str]) -> None:
    trimmed = sorted(values)[-2000:]
    with open(_state_path(), "w", encoding="utf-8") as handle:
        json.dump(trimmed, handle, ensure_ascii=False)


def _post_to_nvc(row: dict) -> tuple[bool, str]:
    token = os.getenv("ANGIOTC_IMPORT_TOKEN", "")
    if not token:
        return False, "ANGIOTC_IMPORT_TOKEN ausente"

    url = os.getenv("NVC_ANGIOTC_TRIAGE_URL", DEFAULT_NVC_URL)
    payload = {
        "atendimento": str(row["cd_atendimento"]),
        "ticketNumber": str(row["ds_senha"] or ""),
        "importedBy": "Integração senha MV",
    }
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "Accept": "application/json",
            "x-angiotc-import-token": token,
        },
    )

    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            body = response.read().decode("utf-8", errors="replace")
            return 200 <= response.status < 300, body
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        return False, f"HTTP {exc.code}: {body}"
    except Exception as exc:  # pragma: no cover - production diagnostics
        return False, f"{type(exc).__name__}: {exc}"


def main() -> int:
    rows = _load_recent_tickets()
    seen = _load_seen()
    failures = 0
    for row in rows:
        row_key = str(row.get("cd_triagem_atendimento") or "")
        if row_key and row_key in seen:
            continue
        ok, message = _post_to_nvc(row)
        status = "ok" if ok else "falha"
        if not ok:
            failures += 1
        elif row_key:
            seen.add(row_key)
        print(
            json.dumps(
                {
                    "timestamp": datetime.now().isoformat(timespec="seconds"),
                    "status": status,
                    "ticket": row.get("ds_senha"),
                    "atendimento": row.get("cd_atendimento"),
                    "triagem": row.get("cd_triagem_atendimento"),
                    "response": message[:500],
                },
                ensure_ascii=False,
                default=str,
            ),
            flush=True,
        )
    if rows:
        _save_seen(seen)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
