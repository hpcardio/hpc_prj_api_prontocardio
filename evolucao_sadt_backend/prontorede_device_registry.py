'''Persistent public-key registry for automatically enrolled ProntoRede devices.'''

import base64
import json
import sqlite3
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path


class DeviceKeyConflictError(RuntimeError):
    '''Raised when an installation tries to replace its registered key.'''


def _canonical_jwk(public_jwk: Mapping[str, object]) -> str:
    if public_jwk.get('kty') != 'EC' or public_jwk.get('crv') != 'P-256':
        raise ValueError('Only P-256 public keys are accepted.')
    for name in ('x', 'y'):
        value = public_jwk.get(name)
        if not isinstance(value, str) or not value:
            raise ValueError('Invalid P-256 public key.')
        padding = '=' * (-len(value) % 4)
        try:
            decoded = base64.b64decode(
                value.encode('ascii') + padding.encode('ascii'),
                altchars=b'-_',
                validate=True,
            )
        except (UnicodeError, ValueError) as exc:
            raise ValueError('Invalid P-256 public key.') from exc
        if len(decoded) != 32:
            raise ValueError('Invalid P-256 public key.')
    normalized = {
        'crv': 'P-256',
        'ext': True,
        'key_ops': ['verify'],
        'kty': 'EC',
        'x': public_jwk['x'],
        'y': public_jwk['y'],
    }
    return json.dumps(normalized, separators=(',', ':'), sort_keys=True)


class SqliteDeviceRegistry:
    def __init__(self, path: str | Path):
        self._path = str(path)
        Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(
                '''
                CREATE TABLE IF NOT EXISTS prontorede_devices (
                    installation_id TEXT PRIMARY KEY,
                    public_jwk_json TEXT NOT NULL,
                    extension_version TEXT NOT NULL,
                    enrolled_by_mv_user TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1))
                )
                '''
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._path, timeout=5)
        connection.execute('PRAGMA journal_mode=WAL')
        connection.execute('PRAGMA busy_timeout=5000')
        return connection

    def enroll(
        self,
        installation_id: str,
        public_jwk: Mapping[str, object],
        extension_version: str,
        enrolled_by_mv_user: str,
    ) -> None:
        canonical = _canonical_jwk(public_jwk)
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            existing = connection.execute(
                'SELECT public_jwk_json FROM prontorede_devices WHERE installation_id = ?',
                (installation_id,),
            ).fetchone()
            if existing is not None:
                if existing[0] != canonical:
                    raise DeviceKeyConflictError(
                        'Installation already has a different public key.'
                    )
                connection.execute(
                    '''UPDATE prontorede_devices
                       SET extension_version = ?, last_seen_at = ?, active = 1
                       WHERE installation_id = ?''',
                    (extension_version, now, installation_id),
                )
                return
            connection.execute(
                '''INSERT INTO prontorede_devices
                   (installation_id, public_jwk_json, extension_version,
                    enrolled_by_mv_user, created_at, last_seen_at, active)
                   VALUES (?, ?, ?, ?, ?, ?, 1)''',
                (
                    installation_id,
                    canonical,
                    extension_version,
                    enrolled_by_mv_user,
                    now,
                    now,
                ),
            )

    def get_public_jwk(self, installation_id: str) -> dict[str, object] | None:
        with self._connect() as connection:
            row = connection.execute(
                '''SELECT public_jwk_json FROM prontorede_devices
                   WHERE installation_id = ? AND active = 1''',
                (installation_id,),
            ).fetchone()
        return json.loads(row[0]) if row is not None else None

    def set_last_seen(self, installation_id: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            connection.execute(
                '''UPDATE prontorede_devices SET last_seen_at = ?
                   WHERE installation_id = ? AND active = 1''',
                (now, installation_id),
            )

    def set_active(self, installation_id: str, active: bool) -> None:
        with self._connect() as connection:
            connection.execute(
                'UPDATE prontorede_devices SET active = ? WHERE installation_id = ?',
                (1 if active else 0, installation_id),
            )
