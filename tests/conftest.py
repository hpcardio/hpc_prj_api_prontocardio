import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app_prontocardio.app import app
from app_prontocardio.settings import Settings


@pytest.fixture
def cliente():
    with TestClient(app) as client:
        yield client


@pytest.fixture(scope='session')
def oracle_engine():
    database_url = Settings().ORACLE_DATABASE_URL
    engine = create_engine(
        database_url,
        thick_mode=True,
        pool_pre_ping=True,
    )

    try:
        with engine.connect() as conn:
            conn.execute(text('SELECT * FROM v$version')).fetchall()
    except Exception as exc:
        pytest.fail(f'Falha ao validar conexao Oracle: {exc}')

    yield engine
    engine.dispose()


@pytest.fixture(scope='session')
def session(oracle_engine):
    with Session(oracle_engine) as db_session:
        yield db_session
