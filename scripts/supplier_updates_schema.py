"""Additive schema installation. Run with the payables API environment, after backup."""
import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sqlalchemy.schema import CreateTable, CreateIndex
from sqlalchemy.dialects import postgresql
from app_prontocardio.services.supplier_updates_store import metadata
from app_prontocardio.services import supplier_payment_store  # register additive payment tables
from app_prontocardio.services import supplier_bulk_models  # register durable bulk outbox

parser = argparse.ArgumentParser()
parser.add_argument('--apply', action='store_true', help='Create only the new supplier_update_* tables')
args = parser.parse_args()
if not args.apply:
    dialect = postgresql.dialect()
    for table in metadata.sorted_tables:
        ddl=str(CreateTable(table).compile(dialect=dialect)) + ';'
        print('\n'.join(line.rstrip() for line in ddl.splitlines()))
        for index in table.indexes:
            print(str(CreateIndex(index).compile(dialect=dialect)) + ';')
else:
    from app_prontocardio.database import postgres_engine
    if postgres_engine is None:
        raise SystemExit('DATABASE_URL must be configured.')
    with postgres_engine.begin() as connection:
        metadata.create_all(connection)
    print(f'{len(metadata.tables)} additive supplier tables checked. No MV writes.')
