from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app_prontocardio.central_atendimento_schema import (
    CentralCommand,
    EffectResponse,
    OperatorScopeRequest,
    OperatorScopeResponse,
    OptionsRequest,
    OptionsResponse,
    PatientReviewRequest,
    PatientReviewResponse,
    PatientSearchRequest,
    PatientSearchResponse,
    QueueResponse,
    RiskPermissionsRequest,
    RiskPermissionsResponse,
)
from app_prontocardio.database import (
    get_session_postgres,
    oracle_readonly_engine,
)
from app_prontocardio.models import Usuario
from app_prontocardio.security import valida_token_usuario_atual
from app_prontocardio.services.central_atendimento_commands import (
    AttemptNotFound,
    CentralCommandService,
)
from app_prontocardio.services.central_atendimento_read import (
    OracleCentralReadProvider,
)
from app_prontocardio.services.central_atendimento_rules import ValidationFacts

router = APIRouter(prefix='/central-atendimento', tags=['central-atendimento'])

SessionPostgres = Annotated[Session, Depends(get_session_postgres)]


def central_user(
    user: Usuario = Depends(valida_token_usuario_atual),
) -> Usuario:
    profile = str(user.perfil or '').casefold()
    screens = set(user.telas_permitidas or [])
    if profile not in {'ti', 'administrador', 'leitura'} and (
        'central_atendimento' not in screens
    ):
        raise HTTPException(
            status_code=HTTPStatus.FORBIDDEN,
            detail='Permissão insuficiente.',
        )
    return user


CentralUser = Annotated[Usuario, Depends(central_user)]


def get_read_provider() -> OracleCentralReadProvider:
    return OracleCentralReadProvider(oracle_readonly_engine)


ReadProvider = Annotated[OracleCentralReadProvider, Depends(get_read_provider)]


def unavailable_facts() -> ValidationFacts:
    return ValidationFacts(
        source_fresh=False,
        source_matches_type=False,
        appointment_cancelled=False,
        ticket_ambiguous=False,
        account_closed=False,
        bed_occupied=False,
        attendance_exists=False,
        official_plan_code=None,
    )


@router.get('/fila', response_model=QueueResponse)
def queue(_user: CentralUser, provider: ReadProvider):
    return provider.queue()


@router.post('/escopo-operador', response_model=OperatorScopeResponse)
def operator_scope(payload: OperatorScopeRequest, _user: CentralUser):
    del payload
    return OperatorScopeResponse(
        unit_codes=[],
        sector_codes=[],
        attendance_types=[],
        valid_until=datetime.now(UTC) + timedelta(minutes=1),
        stale=True,
    )


@router.post(
    '/fila/permissoes-risco',
    response_model=RiskPermissionsResponse,
)
def risk_permissions(payload: RiskPermissionsRequest, _user: CentralUser):
    del payload
    return RiskPermissionsResponse(risk_codes=[], stale=True)


@router.post(
    '/pacientes/pesquisar',
    response_model=PatientSearchResponse,
)
def patient_search(
    payload: PatientSearchRequest,
    _user: CentralUser,
    provider: ReadProvider,
):
    try:
        return provider.search_patients(payload.criterion)
    except Exception as exc:
        raise HTTPException(
            status_code=HTTPStatus.BAD_GATEWAY,
            detail={'code': 'SOURCE_UNAVAILABLE'},
        ) from exc


@router.post('/pacientes/revisar', response_model=PatientReviewResponse)
def patient_review(payload: PatientReviewRequest, _user: CentralUser):
    return PatientReviewResponse(
        patient_code=payload.patient_code,
        divergence_codes=['SOURCE_NOT_MAPPED'],
        stale=True,
    )


@router.post('/opcoes', response_model=OptionsResponse)
def options(payload: OptionsRequest, _user: CentralUser):
    del payload
    return OptionsResponse(items=[], stale=True)


@router.post('/opcoes/preflight', response_model=OptionsResponse)
def options_preflight(payload: OptionsRequest, _user: CentralUser):
    del payload
    return OptionsResponse(items=[], stale=True)


@router.post('/pre-validar')
def prevalidate(
    payload: CentralCommand,
    user: CentralUser,
    session: SessionPostgres,
):
    return CentralCommandService(session).prevalidate(
        payload,
        operator_id=user.id,
        facts=unavailable_facts(),
    )


@router.post('/abrir')
def open_attendance(
    payload: CentralCommand,
    user: CentralUser,
    session: SessionPostgres,
):
    response = CentralCommandService(session).open(
        payload,
        operator_id=user.id,
        facts=unavailable_facts(),
    )
    raise HTTPException(
        status_code=HTTPStatus.LOCKED,
        detail={'code': response.reason_code, 'state': response.state.value},
    )


@router.get(
    '/tentativas/{attempt_id}/efeitos',
    response_model=EffectResponse,
)
def effects(attempt_id: str, _user: CentralUser):
    return EffectResponse(
        attempt_id=attempt_id,
        attendance_code=None,
        account_code=None,
        order_codes=[],
        verified_at=datetime.now(UTC),
        stale=True,
    )


@router.post('/tentativas/{attempt_id}/retomar')
def resume(
    attempt_id: str,
    user: CentralUser,
    session: SessionPostgres,
):
    try:
        response = CentralCommandService(session).resume(
            attempt_id,
            operator_id=user.id,
        )
    except AttemptNotFound as exc:
        raise HTTPException(
            status_code=HTTPStatus.NOT_FOUND,
            detail={'code': 'ATTEMPT_NOT_FOUND'},
        ) from exc
    raise HTTPException(
        status_code=HTTPStatus.LOCKED,
        detail={'code': response.reason_code, 'state': response.state.value},
    )
