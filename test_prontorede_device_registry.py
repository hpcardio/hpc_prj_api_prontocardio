import base64

import pytest

from evolucao_sadt_backend.prontorede_device_registry import (
    DeviceKeyConflictError,
    SqliteDeviceRegistry,
)


INSTALLATION_ID = '11111111-1111-4111-8111-111111111111'


def coordinate(byte: int) -> str:
    return base64.urlsafe_b64encode(bytes([byte]) * 32).decode().rstrip('=')


def jwk(x_byte: int = 1, y_byte: int = 2) -> dict[str, object]:
    return {
        'crv': 'P-256',
        'ext': True,
        'key_ops': ['verify'],
        'kty': 'EC',
        'x': coordinate(x_byte),
        'y': coordinate(y_byte),
    }


def test_registry_persists_device_across_instances(tmp_path):
    path = tmp_path / 'devices.sqlite3'
    first = SqliteDeviceRegistry(path)
    first.enroll(INSTALLATION_ID, jwk(), '2.4.0', 'DBAMV')

    second = SqliteDeviceRegistry(path)
    assert second.get_public_jwk(INSTALLATION_ID) == jwk()


def test_registry_rejects_key_replacement(tmp_path):
    registry = SqliteDeviceRegistry(tmp_path / 'devices.sqlite3')
    registry.enroll(INSTALLATION_ID, jwk(), '2.4.0', 'DBAMV')

    with pytest.raises(DeviceKeyConflictError):
        registry.enroll(INSTALLATION_ID, jwk(3, 4), '2.4.0', 'DBAMV')


def test_registry_returns_none_for_inactive_device(tmp_path):
    registry = SqliteDeviceRegistry(tmp_path / 'devices.sqlite3')
    registry.enroll(INSTALLATION_ID, jwk(), '2.4.0', 'DBAMV')
    registry.set_active(INSTALLATION_ID, False)

    assert registry.get_public_jwk(INSTALLATION_ID) is None


def test_registry_rejects_non_p256_jwk(tmp_path):
    registry = SqliteDeviceRegistry(tmp_path / 'devices.sqlite3')
    invalid = {**jwk(), 'crv': 'P-384'}

    with pytest.raises(ValueError):
        registry.enroll(INSTALLATION_ID, invalid, '2.4.0', 'DBAMV')
