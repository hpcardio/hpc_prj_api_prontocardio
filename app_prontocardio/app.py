from fastapi import FastAPI

from app_prontocardio.routers import (
    app_glosas,
    autenticacao,
    livre,
    usuarios,
)

app = FastAPI(title='API Hospital Prontocardio 💙')

app.include_router(autenticacao.router)
app.include_router(livre.router)
app.include_router(usuarios.router)
app.include_router(app_glosas.router)
