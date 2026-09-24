import hashlib
import logging
import secrets
from http import HTTPStatus
from time import perf_counter
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Response
from pydantic import SecretStr
from starlette.concurrency import run_in_threadpool

from app_prontocardio.database import oracle_engine
from app_prontocardio.ecg_worklist_schema import (
    WorklistOrdersRequest,
    WorklistOrdersResponse,
    WorklistSearchRequest,
    WorklistSearchResponse,
    WorklistSectorsResponse,
)
from app_prontocardio.services.ecg_oracle_worklist import (
    OracleWorklistClient,
    OracleWorklistError,
)
from app_prontocardio.settings import Settings

router = APIRouter(prefix='/ecg/worklist', tags=['ECG Worklist'])
logger = logging.getLogger(__name__)
settings = Settings()
oracle_worklist_client = OracleWorklistClient(
    oracle_engine,
    procedure_code=settings.ECG_MV_PROCEDURE_CODE,
)


def get_oracle_worklist_client() -> OracleWorklistClient:
    return oracle_worklist_client


def validar_token_integracao_ecg(
    token: Annotated[
        str | None, Header(alias='X-ECG-Integration-Token')
    ] = None,
) -> None:
    configurado = settings.ECG_WORKLIST_INTEGRATION_TOKEN
    if configurado is None:
        raise HTTPException(
            status_code=HTTPStatus.SERVICE_UNAVAILABLE,
            detail='Integração ECG não configurada.',
        )
    esperado = (
        configurado.get_secret_value()
        if isinstance(configurado, SecretStr)
        else configurado
    )
    if not token or not secrets.compare_digest(token, esperado):
        raise HTTPException(
            status_code=HTTPStatus.UNAUTHORIZED,
            detail='Credencial ECG inválida.',
        )


def _erro_http(erro: OracleWorklistError) -> HTTPException:
    status = (
        HTTPStatus.GATEWAY_TIMEOUT
        if erro.code == 'MV_TIMEOUT'
        else HTTPStatus.SERVICE_UNAVAILABLE
    )
    return HTTPException(
        status_code=status,
        detail={
            'code': erro.code,
            'message': 'Não foi possível consultar a worklist.',
        },
    )


@router.post('/search', response_model=WorklistSearchResponse)
async def pesquisar_worklist(
    payload: WorklistSearchRequest,
    response: Response,
    _autorizado: None = Depends(validar_token_integracao_ecg),
    client: OracleWorklistClient = Depends(get_oracle_worklist_client),
) -> WorklistSearchResponse:
    response.headers['Cache-Control'] = 'no-store'
    inicio = perf_counter()
    identifier_hash = hashlib.sha256(
        payload.identifier.encode('utf-8')
    ).hexdigest()
    codigo = 'OK'
    items = []
    try:
        items = await run_in_threadpool(client.search, payload)
    except OracleWorklistError as erro:
        codigo = erro.code
        raise _erro_http(erro) from erro
    finally:
        duracao_ms = round((perf_counter() - inicio) * 1000, 2)
        logger.info(
            'ecg_worklist client=ecg-gateway search_type=%s count=%s '
            'duration_ms=%s code=%s identifier_hash=%s',
            payload.search_type,
            len(items),
            duracao_ms,
            codigo,
            identifier_hash,
        )
    return WorklistSearchResponse(
        searchType=payload.search_type,
        identifier=payload.identifier,
        items=items,
    )


@router.get('/health')
async def health_worklist(
    response: Response,
    _autorizado: None = Depends(validar_token_integracao_ecg),
    client: OracleWorklistClient = Depends(get_oracle_worklist_client),
) -> dict[str, str]:
    response.headers['Cache-Control'] = 'no-store'
    try:
        await run_in_threadpool(client.health)
    except OracleWorklistError as erro:
        raise _erro_http(erro) from erro
    return {'status': 'ok'}


@router.post('/orders', response_model=WorklistOrdersResponse)
async def listar_pedidos(
    payload: WorklistOrdersRequest,
    response: Response,
    _autorizado: None = Depends(validar_token_integracao_ecg),
    client: OracleWorklistClient = Depends(get_oracle_worklist_client),
) -> WorklistOrdersResponse:
    response.headers['Cache-Control'] = 'no-store'
    inicio = perf_counter()
    codigo = 'OK'
    items = []
    try:
        items = await run_in_threadpool(client.list_orders, payload)
    except OracleWorklistError as erro:
        codigo = erro.code
        raise _erro_http(erro) from erro
    finally:
        logger.info(
            'ecg_worklist client=ecg-gateway operation=orders count=%s '
            'duration_ms=%s code=%s',
            len(items),
            round((perf_counter() - inicio) * 1000, 2),
            codigo,
        )
    return WorklistOrdersResponse(
        dateFrom=payload.date_from,
        dateToExclusive=payload.date_to_exclusive,
        items=items,
    )


@router.get('/sectors', response_model=WorklistSectorsResponse)
async def listar_setores(
    response: Response,
    _autorizado: None = Depends(validar_token_integracao_ecg),
    client: OracleWorklistClient = Depends(get_oracle_worklist_client),
) -> WorklistSectorsResponse:
    response.headers['Cache-Control'] = 'no-store'
    try:
        items = await run_in_threadpool(client.list_sectors)
    except OracleWorklistError as erro:
        raise _erro_http(erro) from erro
    logger.info(
        'ecg_worklist client=ecg-gateway operation=sectors count=%s code=OK',
        len(items),
    )
    return WorklistSectorsResponse(items=items)
