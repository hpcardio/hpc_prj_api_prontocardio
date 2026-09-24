import os
from types import SimpleNamespace

from fastapi import FastAPI
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app_prontocardio.database import get_session_postgres
from app_prontocardio.models import AuditoriaEvolucaoMv
from evolucao_sadt_backend import evolucoes
from evolucao_sadt_backend.forms_app import app as forms_app

ESCRITA_HABILITADA = (
    os.getenv('EVOLUCAO_MV_WRITE_ENABLED', 'false').lower() == 'true'
)

engine_auditoria = create_engine(
    'sqlite+pysqlite://',
    connect_args={'check_same_thread': False},
    poolclass=StaticPool,
)


@event.listens_for(engine_auditoria, 'connect')
def anexar_schema_sqlite(dbapi_connection, _):
    dbapi_connection.execute("ATTACH DATABASE ':memory:' AS api_prontocardio")


AuditoriaEvolucaoMv.__table__.create(engine_auditoria)

app = FastAPI(
    title='Evolução / SADT - Evolução',
    description='Serviço de evolução médica integrado ao MV.',
)

evolucoes.settings.EVOLUCAO_MV_WRITE_ENABLED = ESCRITA_HABILITADA
def operador_evolucao_sadt():
    return SimpleNamespace(id=None, nome='Evolução / SADT')


def sessao_auditoria_piloto():
    with Session(engine_auditoria) as session:
        yield session


app.dependency_overrides[evolucoes.valida_usuario_ti] = operador_evolucao_sadt
app.dependency_overrides[get_session_postgres] = sessao_auditoria_piloto
app.include_router(evolucoes.router)
app.mount('/formularios', forms_app)


@app.get('/status')
def status():
    return {
        'status': 'ok',
        'modo': 'producao',
        'restricao_por_atendimento': False,
        'escrita_mv': ESCRITA_HABILITADA,
    }
