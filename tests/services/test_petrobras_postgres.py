"""Opt-in checks against a disposable localhost PostgreSQL database only."""

import importlib.util
import os
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from app_prontocardio.models import table_registry
from app_prontocardio.petrobras_models import (
    PetrobrasAnexo,
    PetrobrasGlosa,
    PetrobrasProtocolo,
)
from app_prontocardio.services.petrobras_config import PetrobrasConfig
from app_prontocardio.services.petrobras_sync import import_rows

SCHEMA = 'api_prontocardio'


@pytest.fixture
def pg():
    url = os.environ.get('PETROBRAS_TEST_POSTGRES_URL')
    if not url:
        pytest.skip('Disposable PostgreSQL URL not configured.')
    parsed = make_url(url)
    if (
        parsed.host != '127.0.0.1'
        or parsed.database != 'petrobras_validation'
        or parsed.username != 'petrobras_test'
    ):
        pytest.fail('Only the dedicated localhost validation DB is allowed.')
    engine = create_engine(url)
    path = (
        Path(__file__).parents[2]
        / 'migrations/versions/20261029_063_petrobras_tiss.py'
    )
    spec = importlib.util.spec_from_file_location('pg_migration', path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    with engine.begin() as conn:
        conn.execute(text('CREATE SCHEMA api_prontocardio'))
        migration.op = Operations(MigrationContext.configure(conn))
        migration.upgrade()
    try:
        yield engine, migration
    finally:
        with engine.begin() as conn:
            migration.op = Operations(MigrationContext.configure(conn))
            migration.downgrade()
            conn.execute(text('DROP SCHEMA api_prontocardio'))
        engine.dispose()


def test_postgres_migration_schema_and_reupgrade(pg):
    engine, migration = pg
    inspector = inspect(engine)
    for table in table_registry.metadata.tables.values():
        if not table.name.startswith('petrobras_'):
            continue
        actual = inspector.get_columns(table.name, schema=SCHEMA)
        assert {c['name'] for c in actual} == {c.name for c in table.columns}
    indexes = inspector.get_indexes('petrobras_glosas', schema=SCHEMA)
    assert any(i['column_names'] == ['protocolo_id'] for i in indexes)
    with engine.begin() as conn:
        migration.op = Operations(MigrationContext.configure(conn))
        migration.downgrade()
        assert not inspect(conn).get_table_names(schema=SCHEMA)
        migration.upgrade()


def test_postgres_import_json_binary_and_constraints(pg):
    engine, _ = pg
    config = PetrobrasConfig(
        prestador_cnpj='00000000000000', oracle_guia='guia_operadora'
    )
    source = {
        'chave': 'synthetic-postgres',
        'demonstrativo': 'D1',
        'protocolo': '123',
        'guia_operadora': '000123',
        'tuss': '00123456',
        'carteira': '000001',
        'escopo': 'item',
        'valor_glosa': '20.00',
        'motivos': [{'codigo': '1701', 'valor': '20.00'}],
    }
    candidate = {
        'cd_convenio': 37,
        'nr_guia': '000123',
        'cd_tuss': '00123456',
        'nr_carteira': '000001',
        'cd_reg': 1,
        'cd_lancamento': 1,
    }
    with Session(engine) as db:
        protocol = PetrobrasProtocolo(
            prestador=config.prestador_cnpj, numero='123'
        )
        db.add(protocol)
        db.commit()
        import_rows(
            db, protocol, [source], {('123', 'D1')}, [candidate], config
        )
        db.commit()
        row = db.scalar(select(PetrobrasGlosa))
        row.justificativa = 'Justificativa preservada'
        db.add(
            PetrobrasAnexo(
                glosa_id=row.id,
                nome='teste.pdf',
                mime='application/pdf',
                conteudo=b'%PDF-synthetic',
                sha256='a' * 64,
                tipo_documento='02',
                natureza_guia='2',
                formato='02',
            )
        )
        db.commit()
        import_rows(
            db, protocol, [source], {('123', 'D1')}, [candidate], config
        )
        db.commit()
        db.expire_all()
        rows = list(db.scalars(select(PetrobrasGlosa)))
        assert len(rows) == 1
        assert rows[0].oracle['nr_carteira'] == '000001'
        assert rows[0].justificativa == 'Justificativa preservada'
        assert rows[0].atualizado_em.tzinfo is not None
        assert db.scalar(select(PetrobrasAnexo)).conteudo == b'%PDF-synthetic'
        duplicate = PetrobrasProtocolo(
            prestador=config.prestador_cnpj, numero='123'
        )
        orphan = PetrobrasGlosa(
            chave='invalid-parent',
            protocolo_id=-1,
            demonstrativo='D2',
            fonte={},
        )
        for invalid in (duplicate, orphan):
            with pytest.raises(IntegrityError):
                insert_invalid(db, invalid)


def insert_invalid(db, row):
    with db.begin_nested():
        db.add(row)
        db.flush()


def test_postgres_row_and_worker_locks(pg):
    engine, _ = pg
    with Session(engine) as db:
        row = PetrobrasProtocolo(prestador='00000000000000', numero='123')
        db.add(row)
        db.commit()
        row_id = row.id
    with Session(engine) as first, Session(engine) as second:
        stmt = select(PetrobrasProtocolo).where(
            PetrobrasProtocolo.id == row_id
        )
        first.scalar(stmt.with_for_update())
        with pytest.raises(OperationalError):
            second.scalar(stmt.with_for_update(nowait=True))
        second.rollback()
        first.rollback()
        assert second.scalar(stmt.with_for_update(nowait=True)) is not None
        second.rollback()
    with engine.connect() as first, engine.connect() as second:
        lock = text('SELECT pg_try_advisory_lock(422631, 40100)')
        assert first.scalar(lock)
        assert not second.scalar(lock)
        assert first.scalar(text('SELECT pg_advisory_unlock(422631, 40100)'))
        assert second.scalar(lock)
        assert second.scalar(text('SELECT pg_advisory_unlock(422631, 40100)'))
