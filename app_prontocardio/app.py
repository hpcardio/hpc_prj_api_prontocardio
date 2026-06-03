from http import HTTPStatus

from fastapi import Depends, FastAPI
from sqlalchemy import text
from sqlalchemy.orm import Session

from app_prontocardio.database import get_session_oracle
from app_prontocardio.routers import (
    usuarios,
    app_glosas,
    livre
)
from app_prontocardio.schema import VersaoOracle


app = FastAPI(title='API Hospital Prontocardio 💙')

app.include_router(livre.router)
app.include_router(usuarios.router)
app.include_router(app_glosas.router)

