from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from app_prontocardio.database import (
    ensure_postgres_schema,
    run_postgres_migrations,
)
from app_prontocardio.routers import (
    agendamentos,
    app_glosas,
    autenticacao,
    biq,
    contas_pagar,
    cuidados_paciente,
    ecg_worklist,
    evolucoes,
    farmacia,
    faturamento_rede,
    financeiro,
    institucional,
    livre,
    operacional,
    origens_mv,
    paciente_auth,
    painel_senhas_soulmv,
    painel_senhas_soulmv_clinica1,
    painel_senhas_soulmv_emergencia,
    painel_leitos,
    painel_leitos_soulmv,
    prescricoes,
    prontolaudo,
    requisicoes,
    resultados,
    resultados_laboratoriais,
    usuarios,
    whatsapp,
)
from app_prontocardio.security import usuario_token_somente_leitura
from app_prontocardio.settings import Settings
from evolucao_sadt_backend.app import app as evolucao_sadt_app

settings = Settings()


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    if settings.RUN_MIGRATIONS_ON_STARTUP:
        ensure_postgres_schema()
        run_postgres_migrations()
    yield


app = FastAPI(title='API Hospital Prontocardio 💙', lifespan=lifespan)


@app.middleware('http')
async def bloquear_escrita_perfil_leitura(request: Request, call_next):
    if request.method in {'GET', 'HEAD', 'OPTIONS'}:
        return await call_next(request)
    if (
        request.method == 'POST'
        and request.url.path == '/ecg/worklist/search'
    ):
        return await call_next(request)

    authorization = request.headers.get('Authorization', '')
    scheme, _, token = authorization.partition(' ')
    if (
        scheme.casefold() == 'bearer'
        and token
        and await run_in_threadpool(usuario_token_somente_leitura, token)
    ):
        return JSONResponse(
            status_code=403,
            content={
                'detail': 'Perfil de leitura não permite alterações.'
            },
        )

    return await call_next(request)

if settings.cors_allowed_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_allowed_origins,
        allow_credentials='*' not in settings.cors_allowed_origins,
        allow_methods=['*'],
        allow_headers=['*'],
    )

app.include_router(autenticacao.router)
app.include_router(ecg_worklist.router)
app.include_router(livre.router)
app.include_router(paciente_auth.router)
app.include_router(prescricoes.router)
app.include_router(usuarios.router)
app.include_router(app_glosas.router)
app.include_router(financeiro.router)
app.include_router(contas_pagar.router)
app.include_router(requisicoes.router)
app.include_router(agendamentos.router)
app.include_router(evolucoes.router)
app.include_router(operacional.router)
app.include_router(cuidados_paciente.router)
app.include_router(cuidados_paciente.operational_router)
app.include_router(prontolaudo.router)
app.include_router(origens_mv.router)
app.include_router(painel_senhas_soulmv.router)
app.include_router(painel_senhas_soulmv_clinica1.router)
app.include_router(painel_senhas_soulmv_emergencia.router)
app.include_router(painel_leitos.router)
app.include_router(painel_leitos_soulmv.router)
app.include_router(resultados.router)
app.include_router(resultados_laboratoriais.router)
app.include_router(faturamento_rede.router)
app.include_router(biq.router)
app.include_router(farmacia.router)
app.include_router(whatsapp.router)
app.include_router(institucional.router)
app.mount('/evolucao-sadt', evolucao_sadt_app)
