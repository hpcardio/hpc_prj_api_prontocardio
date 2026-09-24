'''Bounded device-proof challenges for ProntoRede MV context exchange.'''

import base64
import binascii
import json
import os
import secrets
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec

from evolucao_sadt_backend.prontorede_context import VerifiedMvContext
from evolucao_sadt_backend.prontorede_device_registry import (
    DeviceKeyConflictError,
    SqliteDeviceRegistry,
)

CHALLENGE_TTL_SECONDS = 30.0
EVIDENCE_TTL_SECONDS = 60.0
MAX_LIVE_ENTRIES = 1_000
MAX_PROOF_ATTEMPTS = 5
P256_COORDINATE_BYTES = 32
# P-256 ECDSA DER signatures contain two at-most-33-byte INTEGER values.
MAX_P256_DER_SIGNATURE_BYTES = 72
MAX_INSTALLATION_ID_LENGTH = 64
MAX_FINALIDADE_LENGTH = 64
MAX_MV_USER_LENGTH = 64
MAX_MV_CRM_LENGTH = 64
DEVICE_PUBLIC_KEYS_ENVIRONMENT = 'PRONTOREDE_MV_DEVICE_PUBLIC_KEYS_JSON'


class ProntoRedeDeviceVerificationError(PermissionError):
    '''Raised when a device proof cannot establish an MV context.'''


class ProntoRedeDeviceConfigurationError(RuntimeError):
    '''Raised when the configured public-key registry is malformed.'''


class ProntoRedeDeviceResolutionError(ProntoRedeDeviceVerificationError):
    '''Raised when the MV resolver fails or returns an invalid context.'''


class ProntoRedeDeviceConflictError(ProntoRedeDeviceVerificationError):
    '''Raised when an installation id is already bound to another key.'''


@dataclass(frozen=True)
class ChallengeRequest:
    installation_id: str
    finalidade: str
    cd_atendimento_hint: int
    cd_usuario_hint: str


@dataclass(frozen=True)
class ChallengeResponse:
    challenge_id: str
    signing_payload: str


@dataclass(frozen=True)
class ProofRequest:
    challenge_id: str
    signature: bytes


@dataclass(frozen=True)
class ProofResponse:
    mv_session_evidence: str


@dataclass(frozen=True)
class EnrollmentChallengeRequest:
    installation_id: str
    public_jwk: Mapping[str, object]
    extension_version: str
    finalidade: str
    cd_atendimento_hint: int
    cd_usuario_hint: str


@dataclass(frozen=True)
class EnrollmentProofRequest:
    challenge_id: str
    signature: bytes


@dataclass(frozen=True)
class EnrollmentChallengeRecord:
    challenge_id: str
    signing_payload: str
    request: EnrollmentChallengeRequest
    expires_at: float
    attempts: int = 0


@dataclass(frozen=True)
class ChallengeRecord:
    challenge_id: str
    signing_payload: str
    installation_id: str
    finalidade: str
    cd_atendimento_hint: int
    cd_usuario_hint: str
    expires_at: float
    attempts: int = 0


@dataclass(frozen=True)
class EvidenceRecord:
    context: VerifiedMvContext
    expires_at: float


MvContextResolver = Callable[[int, str], VerifiedMvContext]


class ProntoRedeDeviceService:
    '''Issue and verify short-lived ProntoRede device challenges.

    The MV resolver is injected so the device proof boundary is independent of
    the Oracle-specific read-only resolver implemented by the integration
    layer.
    '''

    def __init__(
        self,
        *,
        mv_context_resolver: MvContextResolver,
        monotonic_clock: Callable[[], float] = time.monotonic,
        device_registry: SqliteDeviceRegistry | None = None,
        autoenroll_enabled: bool = False,
    ):
        self._mv_context_resolver = mv_context_resolver
        self._monotonic_clock = monotonic_clock
        self._challenges: dict[str, ChallengeRecord] = {}
        self._enrollment_challenges: dict[str, EnrollmentChallengeRecord] = {}
        self._evidence: dict[str, EvidenceRecord] = {}
        self._device_registry = device_registry
        self._autoenroll_enabled = autoenroll_enabled
        self._lock = threading.Lock()

    def issue_enrollment_challenge(
        self, request: EnrollmentChallengeRequest
    ) -> ChallengeResponse:
        if not self._autoenroll_enabled or self._device_registry is None:
            raise ProntoRedeDeviceVerificationError(
                'Cadastro automático de estação indisponível.'
            )
        self._validate_enrollment_request(request)
        _public_key_from_jwk(request.public_jwk)
        registered = self._device_registry.get_public_jwk(
            request.installation_id
        )
        if registered is not None and dict(registered) != dict(
            request.public_jwk
        ):
            raise ProntoRedeDeviceConflictError(
                'Identidade da estação divergente.'
            )
        challenge_id = secrets.token_urlsafe(32)
        signing_payload = json.dumps(
            {
                'cd_atendimento_hint': request.cd_atendimento_hint,
                'cd_usuario_hint': request.cd_usuario_hint,
                'challenge_id': challenge_id,
                'extension_version': request.extension_version,
                'finalidade': request.finalidade,
                'installation_id': request.installation_id,
                'purpose': 'PRONTOREDE_DEVICE_ENROLLMENT_V1',
            },
            separators=(',', ':'),
            sort_keys=True,
        )
        now = self._monotonic_clock()
        record = EnrollmentChallengeRecord(
            challenge_id=challenge_id,
            signing_payload=signing_payload,
            request=request,
            expires_at=now + CHALLENGE_TTL_SECONDS,
        )
        with self._lock:
            self._prune_expired_entries(now)
            self._make_room()
            self._enrollment_challenges[challenge_id] = record
        return ChallengeResponse(challenge_id, signing_payload)

    def prove_enrollment(self, request: EnrollmentProofRequest) -> None:
        self._validate_proof_request(
            ProofRequest(request.challenge_id, request.signature)
        )
        now = self._monotonic_clock()
        with self._lock:
            self._prune_expired_entries(now)
            record = self._enrollment_challenges.get(request.challenge_id)
            if record is None:
                raise ProntoRedeDeviceVerificationError(
                    'Prova de cadastro inválida.'
                )
            public_key = _public_key_from_jwk(record.request.public_jwk)
            if not self._verify_signature(
                public_key, record.signing_payload, request.signature
            ):
                attempts = record.attempts + 1
                if attempts >= MAX_PROOF_ATTEMPTS:
                    self._enrollment_challenges.pop(record.challenge_id, None)
                else:
                    self._enrollment_challenges[record.challenge_id] = replace(
                        record, attempts=attempts
                    )
                raise ProntoRedeDeviceVerificationError(
                    'Prova de cadastro inválida.'
                )
            self._enrollment_challenges.pop(record.challenge_id, None)

        context = self._resolve_context(record.request)
        try:
            self._device_registry.enroll(
                record.request.installation_id,
                record.request.public_jwk,
                record.request.extension_version,
                context.cd_usuario,
            )
        except DeviceKeyConflictError as exc:
            raise ProntoRedeDeviceConflictError(
                'Identidade da estação divergente.'
            ) from exc

    def issue_challenge(self, request: ChallengeRequest) -> ChallengeResponse:
        self._validate_challenge_request(request)
        self._public_key_for_installation(request.installation_id)
        challenge_id = secrets.token_urlsafe(32)
        signing_payload = json.dumps(
            {
                'cd_atendimento_hint': request.cd_atendimento_hint,
                'cd_usuario_hint': request.cd_usuario_hint,
                'challenge_id': challenge_id,
                'finalidade': request.finalidade,
                'installation_id': request.installation_id,
            },
            separators=(',', ':'),
            sort_keys=True,
        )
        now = self._monotonic_clock()
        record = ChallengeRecord(
            challenge_id=challenge_id,
            signing_payload=signing_payload,
            installation_id=request.installation_id,
            finalidade=request.finalidade,
            cd_atendimento_hint=request.cd_atendimento_hint,
            cd_usuario_hint=request.cd_usuario_hint,
            expires_at=now + CHALLENGE_TTL_SECONDS,
        )
        with self._lock:
            self._prune_expired_entries(now)
            self._make_room()
            self._challenges[challenge_id] = record
        return ChallengeResponse(
            challenge_id=challenge_id,
            signing_payload=signing_payload,
        )

    def prove(self, request: ProofRequest) -> ProofResponse:
        self._validate_proof_request(request)
        now = self._monotonic_clock()
        with self._lock:
            self._prune_expired_entries(now)
            record = self._challenges.get(request.challenge_id)
            if record is None:
                raise ProntoRedeDeviceVerificationError(
                    'Prova de dispositivo inválida.'
                )
            public_key = self._public_key_for_installation(
                record.installation_id
            )
            if not self._verify_signature(
                public_key, record.signing_payload, request.signature
            ):
                self._record_failed_attempt(record)
                raise ProntoRedeDeviceVerificationError(
                    'Prova de dispositivo inválida.'
                )
            self._challenges.pop(record.challenge_id, None)

        context = self._resolve_context(record)
        if self._device_registry is not None:
            self._device_registry.set_last_seen(record.installation_id)
        evidence = secrets.token_urlsafe(32)
        with self._lock:
            now = self._monotonic_clock()
            self._prune_expired_entries(now)
            self._make_room()
            self._evidence[evidence] = EvidenceRecord(
                context=context,
                expires_at=now + EVIDENCE_TTL_SECONDS,
            )
        return ProofResponse(mv_session_evidence=evidence)

    def consume_evidence(
        self, cd_atendimento: int, evidence: str
    ) -> VerifiedMvContext | None:
        if not _is_positive_integer(cd_atendimento) or not isinstance(
            evidence, str
        ):
            return None
        now = self._monotonic_clock()
        with self._lock:
            self._prune_expired_entries(now)
            record = self._evidence.get(evidence)
            if (
                record is None
                or record.context.cd_atendimento != cd_atendimento
            ):
                return None
            self._evidence.pop(evidence, None)
            return record.context

    def _record_failed_attempt(self, record: ChallengeRecord) -> None:
        attempts = record.attempts + 1
        if attempts >= MAX_PROOF_ATTEMPTS:
            self._challenges.pop(record.challenge_id, None)
            return
        self._challenges[record.challenge_id] = replace(
            record, attempts=attempts
        )

    def _resolve_context(self, record: ChallengeRecord) -> VerifiedMvContext:
        try:
            context = self._mv_context_resolver(
                record.cd_atendimento_hint, record.cd_usuario_hint
            )
        except Exception as exc:
            raise ProntoRedeDeviceResolutionError(
                'Contexto MV não verificável.'
            ) from exc
        if (
            not isinstance(context, VerifiedMvContext)
            or context.cd_atendimento != record.cd_atendimento_hint
            or context.cd_usuario != record.cd_usuario_hint
            or not _is_bounded_nonempty_string(
                context.cd_usuario, MAX_MV_USER_LENGTH
            )
            or not _is_bounded_nonempty_string(
                context.crm, MAX_MV_CRM_LENGTH
            )
        ):
            raise ProntoRedeDeviceResolutionError(
                'Contexto MV não verificável.'
            )
        return context

    def _prune_expired_entries(self, now: float) -> None:
        for challenge_id, record in tuple(self._challenges.items()):
            if record.expires_at <= now:
                self._challenges.pop(challenge_id, None)
        for challenge_id, record in tuple(
            self._enrollment_challenges.items()
        ):
            if record.expires_at <= now:
                self._enrollment_challenges.pop(challenge_id, None)
        for evidence, record in tuple(self._evidence.items()):
            if record.expires_at <= now:
                self._evidence.pop(evidence, None)

    def _make_room(self) -> None:
        while (
            len(self._challenges)
            + len(self._enrollment_challenges)
            + len(self._evidence)
            >= MAX_LIVE_ENTRIES
        ):
            if self._challenges:
                self._challenges.pop(next(iter(self._challenges)))
            elif self._enrollment_challenges:
                self._enrollment_challenges.pop(
                    next(iter(self._enrollment_challenges))
                )
            else:
                self._evidence.pop(next(iter(self._evidence)))

    @staticmethod
    def _validate_enrollment_request(
        request: EnrollmentChallengeRequest,
    ) -> None:
        if (
            not isinstance(request, EnrollmentChallengeRequest)
            or not _is_bounded_nonempty_string(
                request.installation_id, MAX_INSTALLATION_ID_LENGTH
            )
            or not isinstance(request.public_jwk, Mapping)
            or not isinstance(request.extension_version, str)
            or not request.extension_version.startswith('2.4.')
        ):
            raise ProntoRedeDeviceVerificationError(
                'Solicitação de cadastro inválida.'
            )
        ProntoRedeDeviceService._validate_challenge_request(
            ChallengeRequest(
                request.installation_id,
                request.finalidade,
                request.cd_atendimento_hint,
                request.cd_usuario_hint,
            )
        )

    @staticmethod
    def _validate_challenge_request(request: ChallengeRequest) -> None:
        if (
            not isinstance(request, ChallengeRequest)
            or not _is_bounded_nonempty_string(
                request.installation_id, MAX_INSTALLATION_ID_LENGTH
            )
            or not _is_bounded_nonempty_string(
                request.finalidade, MAX_FINALIDADE_LENGTH
            )
            or not _is_positive_integer(request.cd_atendimento_hint)
            or not _is_bounded_nonempty_string(
                request.cd_usuario_hint, MAX_MV_USER_LENGTH
            )
        ):
            raise ProntoRedeDeviceVerificationError(
                'Solicitação de dispositivo inválida.'
            )

    @staticmethod
    def _validate_proof_request(request: ProofRequest) -> None:
        if (
            not isinstance(request, ProofRequest)
            or not isinstance(request.challenge_id, str)
            or not request.challenge_id
            or not isinstance(request.signature, bytes)
            or not request.signature
            or len(request.signature) > MAX_P256_DER_SIGNATURE_BYTES
        ):
            raise ProntoRedeDeviceVerificationError(
                'Prova de dispositivo inválida.'
            )

    @staticmethod
    def _verify_signature(
        public_key: ec.EllipticCurvePublicKey,
        signing_payload: str,
        signature: bytes,
    ) -> bool:
        try:
            public_key.verify(
                signature,
                signing_payload.encode('utf-8'),
                ec.ECDSA(hashes.SHA256()),
            )
        except InvalidSignature:
            return False
        return True

    def _public_key_for_installation(
        self,
        installation_id: str,
    ) -> ec.EllipticCurvePublicKey:
        if self._device_registry is not None:
            registered = self._device_registry.get_public_jwk(installation_id)
            if registered is not None:
                return _public_key_from_jwk(registered)
        raw_keys = os.getenv(DEVICE_PUBLIC_KEYS_ENVIRONMENT, '')
        if not raw_keys:
            raw_keys = '{}'
        try:
            configured_keys = json.loads(raw_keys)
        except json.JSONDecodeError as exc:
            raise ProntoRedeDeviceConfigurationError(
                'Configuração de dispositivos ProntoRede inválida.'
            ) from exc
        if not isinstance(configured_keys, Mapping):
            raise ProntoRedeDeviceConfigurationError(
                'Configuração de dispositivos ProntoRede inválida.'
            )
        if installation_id not in configured_keys:
            raise ProntoRedeDeviceVerificationError(
                'Dispositivo ProntoRede não autorizado.'
            )
        jwk = configured_keys[installation_id]
        if not isinstance(jwk, Mapping):
            raise ProntoRedeDeviceConfigurationError(
                'Configuração de dispositivos ProntoRede inválida.'
            )
        try:
            return _public_key_from_jwk(jwk)
        except (TypeError, UnicodeError, ValueError, binascii.Error) as exc:
            raise ProntoRedeDeviceConfigurationError(
                'Configuração de dispositivos ProntoRede inválida.'
            ) from exc


def _public_key_from_jwk(
    jwk: Mapping[str, object],
) -> ec.EllipticCurvePublicKey:
    if jwk.get('kty') != 'EC' or jwk.get('crv') != 'P-256':
        raise ValueError('Invalid P-256 public key.')
    x = _decode_coordinate(jwk.get('x'))
    y = _decode_coordinate(jwk.get('y'))
    return ec.EllipticCurvePublicNumbers(
        int.from_bytes(x, byteorder='big'),
        int.from_bytes(y, byteorder='big'),
        ec.SECP256R1(),
    ).public_key()


def _decode_coordinate(value: object) -> bytes:
    if not isinstance(value, str) or not value:
        raise ValueError('Missing P-256 coordinate.')
    padding = '=' * (-len(value) % 4)
    decoded = base64.b64decode(
        value.encode('ascii') + padding.encode('ascii'),
        altchars=b'-_',
        validate=True,
    )
    if len(decoded) != P256_COORDINATE_BYTES:
        raise ValueError('Invalid P-256 coordinate length.')
    return decoded


def _is_positive_integer(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _is_bounded_nonempty_string(value: object, maximum_length: int) -> bool:
    return (
        isinstance(value, str)
        and bool(value.strip())
        and len(value) <= maximum_length
    )
