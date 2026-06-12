from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app_prontocardio.database import (
    ensure_postgres_schema,
    run_postgres_migrations,
)
from app_prontocardio.routers import (
    app_glosas,
    autenticacao,
    livre,
    usuarios,
)
from app_prontocardio.settings import Settings

settings = Settings()


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    ensure_postgres_schema()
    if settings.RUN_MIGRATIONS_ON_STARTUP:
        run_postgres_migrations()
    yield


app = FastAPI(title='API Hospital Prontocardio 💙', lifespan=lifespan)

app.include_router(autenticacao.router)
app.include_router(livre.router)
app.include_router(usuarios.router)
app.include_router(app_glosas.router)
