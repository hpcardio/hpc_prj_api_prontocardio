"""Backup PostgreSQL schema before installing the additive supplier tables.

Requires pg_dump matching the server major version. Credentials stay in the
child environment, not command arguments or printed output. No database writes.
"""
import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sqlalchemy.engine import make_url
from app_prontocardio.settings import Settings

parser = argparse.ArgumentParser()
parser.add_argument('output', type=Path)
args = parser.parse_args()
settings = Settings()
executable = shutil.which('pg_dump')
if not executable:
    raise SystemExit('pg_dump is required; install the matching PostgreSQL client before any change.')
url = make_url(settings.DATABASE_URL or '')
if url.get_backend_name() != 'postgresql':
    raise SystemExit('Expected PostgreSQL DATABASE_URL.')
output = args.output.resolve()
output.parent.mkdir(parents=True, exist_ok=True)
if output.exists():
    raise SystemExit('Output exists; select a new backup filename.')
environment = {**os.environ, 'PGPASSWORD': url.password or ''}
command = [executable, '--format=custom', '--no-owner', '--no-acl', '--host', url.host or 'localhost',
           '--port', str(url.port or 5432), '--username', url.username or '', '--dbname', url.database or '',
           '--schema', 'api_prontocardio', '--file', str(output)]
subprocess.run(command, env=environment, check=True)
os.chmod(output, 0o600)
if output.stat().st_size == 0:
    raise SystemExit('Empty backup; do not proceed.')
subprocess.run([shutil.which('pg_restore') or 'pg_restore', '--list', str(output)], check=True, stdout=subprocess.DEVNULL)
print('Backup created and catalog verified. Restore should be rehearsed on an isolated database.')
