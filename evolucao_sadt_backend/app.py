import base64
import binascii
import hmac
import importlib
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, StrictInt, constr

from evolucao_sadt_backend.evolution_app import app as evolution_app
from evolucao_sadt_backend.prontorede_context import (
    MvContextVerificationError,
    ProntoRedeContextInvalidResponseError,
    ProntoRedeContextUnavailableError,
    exchange_verified_mv_context,
    resolve_verified_mv_context,
)
from evolucao_sadt_backend.prontorede_device import (
    CHALLENGE_TTL_SECONDS,
    ChallengeRequest,
    EnrollmentChallengeRequest,
    EnrollmentProofRequest,
    ProntoRedeDeviceConflictError,
    ProntoRedeDeviceConfigurationError,
    ProntoRedeDeviceResolutionError,
    ProntoRedeDeviceService,
    ProntoRedeDeviceVerificationError,
    ProofRequest,
)
from evolucao_sadt_backend.prontorede_device_registry import SqliteDeviceRegistry
from evolucao_sadt_backend.sadt_app import app as sadt_app

app = FastAPI(
    title='Evolução / SADT',
    description='Gateway protegido dos módulos de Evolução e SADT.',
)
logger = logging.getLogger(__name__)


class ProntoRedeContextRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')

    cd_atendimento: StrictInt


class DeviceChallengeRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')

    installation_id: constr(pattern=r'^[a-f0-9-]{36}$')
    finalidade: Literal['ATUALIZACAO_PRONTOCARDIO_REDE']
    cd_atendimento_hint: StrictInt
    cd_usuario_hint: constr(pattern=r'^[A-Z0-9_.-]{2,30}$')


class DeviceProofRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')

    challenge_id: constr(min_length=32, max_length=128)
    signature: constr(min_length=64, max_length=256)


class DevicePublicJwk(BaseModel):
    model_config = ConfigDict(extra='forbid')

    kty: Literal['EC']
    crv: Literal['P-256']
    x: constr(min_length=43, max_length=43)
    y: constr(min_length=43, max_length=43)
    ext: bool = True
    key_ops: list[Literal['verify']] = ['verify']


class DeviceEnrollmentChallengeRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')

    installation_id: constr(pattern=r'^[a-f0-9-]{36}$')
    public_jwk: DevicePublicJwk
    extension_version: constr(pattern=r'^2\.4\.[0-9]+$')
    finalidade: Literal['ATUALIZACAO_PRONTOCARDIO_REDE']
    cd_atendimento_hint: StrictInt
    cd_usuario_hint: constr(pattern=r'^[A-Z0-9_.-]{2,30}$')


def _resolve_authorized_mv_context(cd_atendimento: int, cd_usuario: str):
    oracle = importlib.import_module(
        'evolucao_sadt_backend.prontorede_oracle'
    )
    return oracle.resolve_authorized_mv_context(cd_atendimento, cd_usuario)


def _configure_prontorede_device_service() -> None:
    if not os.getenv('PRONTOREDE_MV_ALLOWED_USERS'):
        return
    database_path = os.getenv(
        'PRONTOREDE_MV_DEVICE_DB_PATH',
        '/app/evolucao_sadt_data/prontorede_devices.sqlite3',
    )
    service = ProntoRedeDeviceService(
        mv_context_resolver=_resolve_authorized_mv_context,
        device_registry=SqliteDeviceRegistry(database_path),
        autoenroll_enabled=(
            os.getenv('PRONTOREDE_MV_DEVICE_AUTOENROLL_ENABLED', 'true').lower()
            == 'true'
        ),
    )
    app.state.prontorede_device_service = service
    app.state.prontorede_session_verifier = service.consume_evidence


def _is_prontorede_sensitive_request(request: Request) -> bool:
    path = request.scope.get('path', '')
    return any(
        path.endswith(suffix)
        for suffix in (
            '/api/prontorede/context',
            '/api/prontorede/device/challenge',
            '/api/prontorede/device/prove',
            '/api/prontorede/device/enrollment/challenge',
            '/api/prontorede/device/enrollment/prove',
        )
    )


def _device_service(request: Request) -> ProntoRedeDeviceService | None:
    service = getattr(request.app.state, 'prontorede_device_service', None)
    return service if isinstance(service, ProntoRedeDeviceService) else None


def _device_error(detail: str, status_code: int) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={'detail': detail},
        headers={'Cache-Control': 'no-store'},
    )


def _decode_signature(value: str) -> bytes:
    padding = '=' * (-len(value) % 4)
    return base64.b64decode(
        value.encode('ascii') + padding.encode('ascii'),
        altchars=b'-_',
        validate=True,
    )


def _is_mv_context_authorization_error(error: BaseException | None) -> bool:
    if error is None:
        return False
    try:
        oracle = importlib.import_module(
            'evolucao_sadt_backend.prontorede_oracle'
        )
    except Exception:
        return False
    return isinstance(error, oracle.MvContextAuthorizationError)


def _device_resolution_error(
    error: ProntoRedeDeviceResolutionError,
) -> JSONResponse:
    if _is_mv_context_authorization_error(error.__cause__):
        return _device_error('Contexto MV não autorizado.', 403)
    return _device_error('Contexto MV indisponível.', 503)


_configure_prontorede_device_service()


@app.middleware('http')
async def validate_internal_key(request: Request, call_next):
    expected = os.getenv('EVOLUCAO_SADT_API_KEY', '')
    provided = request.headers.get('x-evolucao-sadt-key', '')
    if not expected or not hmac.compare_digest(provided, expected):
        headers = (
            {'Cache-Control': 'no-store'}
            if _is_prontorede_sensitive_request(request)
            else None
        )
        return JSONResponse(
            status_code=401,
            content={'detail': 'Credencial interna inválida.'},
            headers=headers,
        )
    response = await call_next(request)
    if _is_prontorede_sensitive_request(request):
        response.headers['Cache-Control'] = 'no-store'
    return response


@app.get('/health')
def health():
    return {'status': 'ok', 'service': 'evolucao-sadt'}


@app.post('/api/prontorede/device/challenge')
def create_prontorede_device_challenge(
    payload: DeviceChallengeRequest,
    request: Request,
):
    service = _device_service(request)
    if service is None:
        return _device_error('Prova de dispositivo indisponível.', 503)
    try:
        challenge = service.issue_challenge(
            ChallengeRequest(
                installation_id=payload.installation_id,
                finalidade=payload.finalidade,
                cd_atendimento_hint=payload.cd_atendimento_hint,
                cd_usuario_hint=payload.cd_usuario_hint,
            )
        )
    except ProntoRedeDeviceConfigurationError:
        return _device_error('Prova de dispositivo indisponível.', 503)
    except ProntoRedeDeviceVerificationError:
        return _device_error('Dispositivo ProntoRede não autorizado.', 401)

    expires_at = datetime.now(timezone.utc) + timedelta(
        seconds=CHALLENGE_TTL_SECONDS
    )
    return JSONResponse(
        content={
            'challenge_id': challenge.challenge_id,
            'signing_payload': challenge.signing_payload,
            'expires_at': expires_at.isoformat().replace('+00:00', 'Z'),
        },
        headers={'Cache-Control': 'no-store'},
    )


@app.post('/api/prontorede/device/prove')
def prove_prontorede_device(
    payload: DeviceProofRequest,
    request: Request,
):
    service = _device_service(request)
    if service is None:
        return _device_error('Prova de dispositivo indisponível.', 503)
    try:
        signature = _decode_signature(payload.signature)
    except (UnicodeError, ValueError, binascii.Error):
        return _device_error('Prova de dispositivo inválida.', 401)

    try:
        proof = service.prove(
            ProofRequest(
                challenge_id=payload.challenge_id,
                signature=signature,
            )
        )
    except ProntoRedeDeviceConfigurationError:
        return _device_error('Prova de dispositivo indisponível.', 503)
    except ProntoRedeDeviceResolutionError as exc:
        return _device_resolution_error(exc)
    except ProntoRedeDeviceVerificationError:
        return _device_error('Prova de dispositivo inválida.', 401)

    return JSONResponse(
        content={'mv_session_evidence': proof.mv_session_evidence},
        headers={'Cache-Control': 'no-store'},
    )


@app.post('/api/prontorede/device/enrollment/challenge')
def create_prontorede_device_enrollment_challenge(
    payload: DeviceEnrollmentChallengeRequest,
    request: Request,
):
    service = _device_service(request)
    if service is None:
        return _device_error('Cadastro automático indisponível.', 503)
    try:
        challenge = service.issue_enrollment_challenge(
            EnrollmentChallengeRequest(
                installation_id=payload.installation_id,
                public_jwk=payload.public_jwk.model_dump(),
                extension_version=payload.extension_version,
                finalidade=payload.finalidade,
                cd_atendimento_hint=payload.cd_atendimento_hint,
                cd_usuario_hint=payload.cd_usuario_hint,
            )
        )
    except ProntoRedeDeviceConflictError:
        return _device_error('Identidade da estação divergente.', 409)
    except ProntoRedeDeviceVerificationError:
        return _device_error('Cadastro automático não autorizado.', 403)
    except (ProntoRedeDeviceConfigurationError, ValueError):
        return _device_error('Cadastro automático indisponível.', 503)
    expires_at = datetime.now(timezone.utc) + timedelta(
        seconds=CHALLENGE_TTL_SECONDS
    )
    return JSONResponse(
        content={
            'challenge_id': challenge.challenge_id,
            'signing_payload': challenge.signing_payload,
            'expires_at': expires_at.isoformat().replace('+00:00', 'Z'),
        },
        headers={'Cache-Control': 'no-store'},
    )


@app.post('/api/prontorede/device/enrollment/prove', status_code=201)
def prove_prontorede_device_enrollment(
    payload: DeviceProofRequest,
    request: Request,
):
    service = _device_service(request)
    if service is None:
        return _device_error('Cadastro automático indisponível.', 503)
    try:
        signature = _decode_signature(payload.signature)
        service.prove_enrollment(
            EnrollmentProofRequest(payload.challenge_id, signature)
        )
    except (UnicodeError, ValueError, binascii.Error):
        return _device_error('Prova de cadastro inválida.', 401)
    except ProntoRedeDeviceConflictError:
        return _device_error('Identidade da estação divergente.', 409)
    except ProntoRedeDeviceResolutionError as exc:
        return _device_resolution_error(exc)
    except ProntoRedeDeviceVerificationError:
        return _device_error('Prova de cadastro inválida.', 401)
    return JSONResponse(
        status_code=201,
        content={'enrolled': True},
        headers={'Cache-Control': 'no-store'},
    )


@app.post('/api/prontorede/context')
def create_prontorede_context(
    payload: ProntoRedeContextRequest,
    request: Request,
):
    session_evidence = request.headers.get(
        'x-prontorede-mv-session-evidence',
        '',
    )
    session_verifier = getattr(
        request.app.state,
        'prontorede_session_verifier',
        None,
    )
    exchange_client = getattr(
        request.app.state,
        'prontorede_exchange_client',
        None,
    )
    try:
        context = resolve_verified_mv_context(
            cd_atendimento=payload.cd_atendimento,
            session_evidence=session_evidence,
            session_verifier=session_verifier,
        )
    except MvContextVerificationError:
        return JSONResponse(
            status_code=401,
            content={'detail': 'Contexto MV não verificável.'},
            headers={'Cache-Control': 'no-store'},
        )

    try:
        exchange = exchange_verified_mv_context(
            context,
            exchange_client=exchange_client,
        )
    except ProntoRedeContextUnavailableError:
        return JSONResponse(
            status_code=503,
            content={'detail': 'Contexto ProntoRede indisponível.'},
            headers={'Cache-Control': 'no-store'},
        )
    except ProntoRedeContextInvalidResponseError as exc:
        logger.warning('Falha na troca de contexto: %s', exc)
        return JSONResponse(
            status_code=502,
            content={'detail': 'Contexto ProntoRede inválido.'},
            headers={'Cache-Control': 'no-store'},
        )

    return JSONResponse(
        content={
            'formulario_token': exchange.formulario_token,
            'expira_em': exchange.expira_em,
            'url': exchange.url,
        },
        headers={'Cache-Control': 'no-store'},
    )


app.mount('/evolucao', evolution_app)
app.mount('/sadt', sadt_app)
