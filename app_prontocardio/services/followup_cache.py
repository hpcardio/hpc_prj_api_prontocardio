"""In-process cache of Follow-Up listing pages.

The listing preparation is global (all processes, remittances and portal
cards before paging) and takes tens of seconds. The API runs in a single
uvicorn process, so one in-memory cache serves every request.

- A successful write under /app_glosas advances the generation (middleware
  in app.py). A page from an older generation is never served: the request
  waits for the recomputation, which starts right away in the background
  for the most used pages, so reloading after a save rarely waits long.
- Without writes, a page older than FRESH_SECONDS is served at once while a
  background refresh brings external loads (Airflow, imports) in.
- One computation per page at a time.
"""

import logging
import threading
import time

from sqlalchemy.orm import Session

from app_prontocardio import database

FRESH_SECONDS = 300
STALE_SECONDS = 1800
WAIT_SECONDS = 120
MAX_ENTRIES = 128
PREWARM_PAGES = 6
WRITE_METHODS = frozenset({'POST', 'PUT', 'PATCH', 'DELETE'})
GLOSA_PREFIX = '/app_glosas/'
# Request objects, not query parameters.
NOT_KEY = frozenset({'usuario_atual', 'session', 'session_oracle'})

logger = logging.getLogger(__name__)
_cond = threading.Condition()
_generation = 0
_entries = {}  # key -> {'gen', 'ts', 'used', 'value', 'loader'}
_running = {}  # key -> generation being computed
_prewarm = {'pending': False, 'thread': None}


def invalidates(method, path, status_code):
    return (
        method in WRITE_METHODS
        and path.startswith(GLOSA_PREFIX)
        and status_code < 400  # noqa: PLR2004 - HTTP error boundary
    )


def generation():
    with _cond:
        return _generation


def _key(params):
    return tuple(sorted((k, v) for k, v in params.items() if k not in NOT_KEY))


def _store(key, gen, value, loader):
    with _cond:
        now = time.monotonic()
        current = _entries.get(key)
        if current is None or current['gen'] <= gen:
            _entries[key] = {
                'gen': gen,
                'ts': now,
                'used': current['used'] if current else now,
                'value': value,
                'loader': loader,
            }
        if _running.get(key) == gen:
            del _running[key]
        _cond.notify_all()
        _prune()


def _prune():
    expired = [
        k
        for k, e in _entries.items()
        if time.monotonic() - e['ts'] > STALE_SECONDS and k not in _running
    ]
    for k in expired:
        del _entries[k]
    for k in sorted(_entries, key=lambda k: _entries[k]['used'])[
        : max(len(_entries) - MAX_ENTRIES, 0)
    ]:
        del _entries[k]


def _compute_own_sessions(key, gen, loader):
    try:
        with (
            Session(database.postgres_engine) as session,
            Session(database.oracle_engine) as session_oracle,
        ):
            value = loader(session, session_oracle)
        _store(key, gen, value, loader)
    except Exception:
        logger.exception('Follow-Up: atualização em segundo plano falhou.')
        with _cond:
            if _running.get(key) == gen:
                del _running[key]
            _cond.notify_all()


def _start_background(key, gen, loader):
    """Caller holds _cond."""
    if key in _running:
        return
    _running[key] = gen
    threading.Thread(
        target=_compute_own_sessions, args=(key, gen, loader), daemon=True
    ).start()


def cached(params, loader):
    """loader(session, session_oracle) -> JSON-ready page."""
    key = _key(params)
    with _cond:
        gen = _generation
        now = time.monotonic()
        entry = _entries.get(key)
        if entry is not None:
            entry['used'] = now
            if entry['gen'] == gen:
                if now - entry['ts'] > FRESH_SECONDS:
                    _start_background(key, gen, loader)
                return entry['value']
        # Missing or from an older generation: wait for a running
        # computation of this generation, or compute it here.
        deadline = now + WAIT_SECONDS
        while _running.get(key) == gen and time.monotonic() < deadline:
            _cond.wait(deadline - time.monotonic())
            entry = _entries.get(key)
            if entry is not None and entry['gen'] == gen:
                return entry['value']
        _running[key] = gen
    try:
        value = loader(params['session'], params['session_oracle'])
    except Exception:
        with _cond:
            if _running.get(key) == gen:
                del _running[key]
            _cond.notify_all()
        raise
    _store(key, gen, value, loader)
    return value


def _prewarm_worker():
    while True:
        with _cond:
            if not _prewarm['pending']:
                _prewarm['thread'] = None
                return
            _prewarm['pending'] = False
            gen = _generation
            recent = sorted(
                (
                    (k, e)
                    for k, e in _entries.items()
                    if e['gen'] < gen and k not in _running
                ),
                key=lambda item: item[1]['used'],
                reverse=True,
            )[:PREWARM_PAGES]
            for key, _ in recent:
                _running[key] = gen
        # Sequential on purpose: each page repeats the global preparation.
        for key, entry in recent:
            _compute_own_sessions(key, gen, entry['loader'])


def bump_generation():
    global _generation  # noqa: PLW0603 - process-wide invalidation counter
    with _cond:
        _generation += 1
        _prewarm['pending'] = True
        if _prewarm['thread'] is None:
            _prewarm['thread'] = threading.Thread(
                target=_prewarm_worker, daemon=True
            )
            _prewarm['thread'].start()


def clear():
    with _cond:
        _entries.clear()
        _running.clear()
        _prewarm['pending'] = False
