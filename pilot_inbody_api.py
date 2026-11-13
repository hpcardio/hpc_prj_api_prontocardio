"""Loopback-only APIHPC adapter for the supervised InBody pilot."""

from __future__ import annotations

import os
import secrets
from http import HTTPStatus

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Response
from starlette.concurrency import run_in_threadpool

from app_prontocardio.ecg_worklist_schema import (
    WorklistOrdersRequest,
    WorklistOrdersResponse,
    WorklistSearchRequest,
    WorklistSearchResponse,
)
from app_prontocardio.services.ecg_oracle_worklist import OracleWorklistError

TOKEN_MIN_LENGTH = 32


def create_app(client, token: str) -> FastAPI:
    if len(token) < TOKEN_MIN_LENGTH:
        raise RuntimeError("Token do piloto inválido.")

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    def authorize(
        supplied: str | None = Header(
            default=None, alias="X-InBody-Integration-Token"
        ),
    ) -> None:
        if not supplied or not secrets.compare_digest(supplied, token):
            raise HTTPException(
                status_code=HTTPStatus.UNAUTHORIZED,
                detail="Credencial InBody inválida.",
            )

    def service_error(error: OracleWorklistError) -> HTTPException:
        status = (
            HTTPStatus.GATEWAY_TIMEOUT
            if error.code == "MV_TIMEOUT"
            else HTTPStatus.SERVICE_UNAVAILABLE
        )
        return HTTPException(
            status_code=status,
            detail={
                "code": error.code,
                "message": "Não foi possível consultar a worklist.",
            },
        )

    @app.post(
        "/inbody/worklist/search",
        response_model=WorklistSearchResponse,
        dependencies=[Depends(authorize)],
    )
    async def search(payload: WorklistSearchRequest, response: Response):
        response.headers["Cache-Control"] = "no-store"
        try:
            items = await run_in_threadpool(client.search, payload)
        except OracleWorklistError as error:
            raise service_error(error) from error
        if len(items) > 1:
            raise HTTPException(
                status_code=HTTPStatus.CONFLICT,
                detail={
                    "code": "INBODY_AMBIGUOUS_ORDER",
                    "message": "Pedido ambíguo para bioimpedanciometria.",
                },
            )
        return WorklistSearchResponse(
            searchType=payload.search_type,
            identifier=payload.identifier,
            items=items,
        )

    @app.post(
        "/inbody/worklist/orders",
        response_model=WorklistOrdersResponse,
        dependencies=[Depends(authorize)],
    )
    async def orders(payload: WorklistOrdersRequest, response: Response):
        response.headers["Cache-Control"] = "no-store"
        try:
            items = await run_in_threadpool(client.list_orders, payload)
        except OracleWorklistError as error:
            raise service_error(error) from error
        return WorklistOrdersResponse(
            dateFrom=payload.date_from,
            dateToExclusive=payload.date_to_exclusive,
            items=items,
        )

    @app.get(
        "/inbody/worklist/status",
        dependencies=[Depends(authorize)],
    )
    async def status(
        response: Response,
        order_number: str = Query(alias="orderNumber", pattern=r"^[0-9]+$"),
        order_item_id: str = Query(alias="orderItemId", pattern=r"^[0-9]+$"),
        attendance_number: str = Query(
            alias="attendanceNumber", pattern=r"^[0-9]+$"
        ),
    ):
        response.headers["Cache-Control"] = "no-store"
        try:
            return await run_in_threadpool(
                client.get_item_status,
                order_number,
                order_item_id,
                attendance_number,
            )
        except OracleWorklistError as error:
            raise service_error(error) from error

    @app.get(
        "/inbody/worklist/health",
        dependencies=[Depends(authorize)],
    )
    async def health(response: Response):
        response.headers["Cache-Control"] = "no-store"
        try:
            await run_in_threadpool(client.health)
        except OracleWorklistError as error:
            raise service_error(error) from error
        return {"status": "ok"}

    return app


def create_runtime_app() -> FastAPI:
    # Import tardio: o engine Oracle só é criado ao subir o piloto.
    from app_prontocardio.database import oracle_engine  # noqa: PLC0415
    from app_prontocardio.services.ecg_oracle_worklist import (  # noqa: PLC0415
        OracleWorklistClient,
    )

    token = os.environ.get("INBODY_PILOT_TOKEN", "")
    client = OracleWorklistClient(
        oracle_engine,
        procedure_code=597,
        reject_invalid_rows=True,
    )
    return create_app(client, token)
