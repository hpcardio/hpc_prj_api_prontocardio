# ruff: noqa: E501
"""Vínculo usuário Receita Certa x login MV: origem (cas/manual) e proxy Oracle.

DDL idempotente; o mesmo SQL está em docs/oracle/usuarios_mv_cas_proxy.sql.
"""

from alembic import op

revision = '20261117_069'
down_revision = '20261117_068'
branch_labels = None
depends_on = None

UPGRADE_SQL = """
ALTER TABLE api_prontocardio.usuarios_mv
    ADD COLUMN IF NOT EXISTS origem VARCHAR(10) NOT NULL DEFAULT 'manual',
    ADD COLUMN IF NOT EXISTS validado_cas_em TIMESTAMP,
    ADD COLUMN IF NOT EXISTS proxy_concedido_em TIMESTAMP;
"""

DOWNGRADE_SQL = """
ALTER TABLE api_prontocardio.usuarios_mv
    DROP COLUMN IF EXISTS proxy_concedido_em,
    DROP COLUMN IF EXISTS validado_cas_em,
    DROP COLUMN IF EXISTS origem;
"""


def upgrade():
    op.execute(UPGRADE_SQL)


def downgrade():
    op.execute(DOWNGRADE_SQL)
