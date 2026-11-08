import base64

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec

from evolucao_sadt_backend.prontorede_context import VerifiedMvContext
from evolucao_sadt_backend.prontorede_device import (
    ChallengeRequest,
    EnrollmentChallengeRequest,
    EnrollmentProofRequest,
    ProntoRedeDeviceService,
    ProntoRedeDeviceVerificationError,
)
from evolucao_sadt_backend.prontorede_device_registry import SqliteDeviceRegistry


INSTALLATION_ID = '22222222-2222-4222-8222-222222222222'
FINALIDADE = 'ATUALIZACAO_PRONTOCARDIO_REDE'


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


def context(attendance: int, user: str) -> VerifiedMvContext:
    return VerifiedMvContext(attendance, user, 781, '00000')


def enrollment_request(private_key) -> EnrollmentChallengeRequest:
    return EnrollmentChallengeRequest(
        installation_id=INSTALLATION_ID,
        public_jwk=public_jwk(private_key),
        extension_version='2.4.0',
        finalidade=FINALIDADE,
        cd_atendimento_hint=347113,
        cd_usuario_hint='DBAMV',
    )


def normal_request() -> ChallengeRequest:
    return ChallengeRequest(
        installation_id=INSTALLATION_ID,
        finalidade=FINALIDADE,
        cd_atendimento_hint=347113,
        cd_usuario_hint='DBAMV',
    )


def test_enrollment_proof_persists_then_allows_normal_challenge(tmp_path):
    private_key = ec.generate_private_key(ec.SECP256R1())
    registry = SqliteDeviceRegistry(tmp_path / 'devices.sqlite3')
    service = ProntoRedeDeviceService(
        mv_context_resolver=context,
        device_registry=registry,
        autoenroll_enabled=True,
    )
    challenge = service.issue_enrollment_challenge(enrollment_request(private_key))
    signature = private_key.sign(
        challenge.signing_payload.encode('utf-8'), ec.ECDSA(hashes.SHA256())
    )

    service.prove_enrollment(
        EnrollmentProofRequest(challenge.challenge_id, signature)
    )

    assert service.issue_challenge(normal_request()).challenge_id
    assert registry.get_public_jwk(INSTALLATION_ID) == public_jwk(private_key)


def test_enrollment_rejects_invalid_signature_without_persisting(tmp_path):
    private_key = ec.generate_private_key(ec.SECP256R1())
    other_key = ec.generate_private_key(ec.SECP256R1())
    registry = SqliteDeviceRegistry(tmp_path / 'devices.sqlite3')
    service = ProntoRedeDeviceService(
        mv_context_resolver=context,
        device_registry=registry,
        autoenroll_enabled=True,
    )
    challenge = service.issue_enrollment_challenge(enrollment_request(private_key))
    signature = other_key.sign(
        challenge.signing_payload.encode('utf-8'), ec.ECDSA(hashes.SHA256())
    )

    with pytest.raises(ProntoRedeDeviceVerificationError):
        service.prove_enrollment(
            EnrollmentProofRequest(challenge.challenge_id, signature)
        )

    assert registry.get_public_jwk(INSTALLATION_ID) is None


def test_enrollment_is_disabled_by_default(tmp_path):
    private_key = ec.generate_private_key(ec.SECP256R1())
    service = ProntoRedeDeviceService(
        mv_context_resolver=context,
        device_registry=SqliteDeviceRegistry(tmp_path / 'devices.sqlite3'),
    )

    with pytest.raises(ProntoRedeDeviceVerificationError):
        service.issue_enrollment_challenge(enrollment_request(private_key))
