import base64
import importlib
import sys
import types
import pytest

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import FastAPI
from fastapi.testclient import TestClient

for module_name in (
    'evolucao_sadt_backend.evolution_app',
    'evolucao_sadt_backend.sadt_app',
):
    stub = types.ModuleType(module_name)
    stub.app = FastAPI()
    sys.modules[module_name] = stub

api_module = importlib.import_module('evolucao_sadt_backend.app')
app = api_module.app
from evolucao_sadt_backend.prontorede_context import VerifiedMvContext
from evolucao_sadt_backend.prontorede_device import (
    EnrollmentChallengeRequest,
    ProntoRedeDeviceService,
    ProntoRedeDeviceVerificationError,
)
from evolucao_sadt_backend.prontorede_device_registry import SqliteDeviceRegistry
from evolucao_sadt_backend.mv_context_token import verify_mv_context_token


INSTALLATION_ID = '33333333-3333-4333-8333-333333333333'


def b64(value: int) -> str:
    return base64.urlsafe_b64encode(value.to_bytes(32, 'big')).decode().rstrip('=')


def public_jwk(private_key) -> dict[str, object]:
    numbers = private_key.public_key().public_numbers()
    return {
        'crv': 'P-256',
        'ext': True,
        'key_ops': ['verify'],
        'kty': 'EC',
        'x': b64(numbers.x),
        'y': b64(numbers.y),
    }


def signature(private_key, payload: str) -> str:
    value = private_key.sign(
        payload.encode('utf-8'), ec.ECDSA(hashes.SHA256())
    )
    return base64.urlsafe_b64encode(value).decode().rstrip('=')


def test_autoenrollment_is_disabled_by_default_even_when_mv_integration_is_configured(
    tmp_path, monkeypatch
):
    monkeypatch.setenv('PRONTOREDE_MV_ALLOWED_USERS', 'DBAMV')
    monkeypatch.setenv(
        'PRONTOREDE_MV_DEVICE_DB_PATH', str(tmp_path / 'devices.sqlite3')
    )
    monkeypatch.delenv('PRONTOREDE_MV_DEVICE_AUTOENROLL_ENABLED', raising=False)
    monkeypatch.setattr(
        api_module,
        '_resolve_authorized_mv_context',
        lambda attendance, user: VerifiedMvContext(attendance, user, 781, '00000'),
    )

    api_module._configure_prontorede_device_service()
    private_key = ec.generate_private_key(ec.SECP256R1())
    with pytest.raises(ProntoRedeDeviceVerificationError):
        app.state.prontorede_device_service.issue_enrollment_challenge(
            EnrollmentChallengeRequest(
                installation_id=INSTALLATION_ID,
                public_jwk=public_jwk(private_key),
                extension_version='2.4.0',
                finalidade='ATUALIZACAO_PRONTOCARDIO_REDE',
                cd_atendimento_hint=347113,
                cd_usuario_hint='DBAMV',
            )
        )


def test_enrollment_api_registers_unknown_station(tmp_path, monkeypatch):
    monkeypatch.setenv('EVOLUCAO_SADT_API_KEY', 'test-key')
    registry = SqliteDeviceRegistry(tmp_path / 'devices.sqlite3')
    app.state.prontorede_device_service = ProntoRedeDeviceService(
        mv_context_resolver=lambda attendance, user: VerifiedMvContext(
            attendance, user, 781, '00000'
        ),
        device_registry=registry,
        autoenroll_enabled=True,
    )
    private_key = ec.generate_private_key(ec.SECP256R1())
    client = TestClient(app)
    headers = {'x-evolucao-sadt-key': 'test-key'}
    challenge = client.post(
        '/api/prontorede/device/enrollment/challenge',
        headers=headers,
        json={
            'installation_id': INSTALLATION_ID,
            'public_jwk': public_jwk(private_key),
            'extension_version': '2.4.0',
            'finalidade': 'ATUALIZACAO_PRONTOCARDIO_REDE',
            'cd_atendimento_hint': 347113,
            'cd_usuario_hint': 'DBAMV',
        },
    )
    assert challenge.status_code == 200
    challenge_body = challenge.json()

    proof = client.post(
        '/api/prontorede/device/enrollment/prove',
        headers=headers,
        json={
            'challenge_id': challenge_body['challenge_id'],
            'signature': signature(private_key, challenge_body['signing_payload']),
        },
    )

    assert proof.status_code == 201
    assert proof.json() == {'enrolled': True}
    assert proof.headers['cache-control'] == 'no-store'
    assert registry.get_public_jwk(INSTALLATION_ID) == public_jwk(private_key)


def test_evolution_context_exchanges_one_time_evidence_for_signed_token(
    monkeypatch,
):
    monkeypatch.setenv('EVOLUCAO_SADT_API_KEY', 'test-key')
    monkeypatch.setenv(
        'EVOLUCAO_SADT_CONTEXT_SECRET',
        'test-secret-with-at-least-thirty-two-bytes',
    )
    expected = VerifiedMvContext(365250, 'MEDICO.TESTE', 781, 'CRM-CE 00000')
    app.state.prontorede_session_verifier = (
        lambda attendance, evidence: expected
        if attendance == 365250 and evidence == 'one-time-evidence'
        else None
    )
    client = TestClient(app)
    response = client.post(
        '/api/evolucao/context',
        headers={
            'x-evolucao-sadt-key': 'test-key',
            'x-prontorede-mv-session-evidence': 'one-time-evidence',
        },
        json={'cd_atendimento': 365250},
    )

    assert response.status_code == 200, response.text
    claims = verify_mv_context_token(
        response.json()['context_token'],
        'test-secret-with-at-least-thirty-two-bytes',
    )
    assert claims.cd_usuario == 'MEDICO.TESTE'
    assert claims.cd_prestador == 781
