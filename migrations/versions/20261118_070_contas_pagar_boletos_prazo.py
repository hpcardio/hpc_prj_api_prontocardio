# ruff: noqa: E501
"""Contas a Pagar: prazo mínimo de 3 dias úteis e justificativa do atraso nos boletos.

DDL idempotente; o mesmo SQL está em docs/oracle/contas_pagar_boletos_prazo.sql.
"""

from alembic import op

revision = '20261118_070'
down_revision = '20261117_069'
branch_labels = None
depends_on = None

UPGRADE_SQL = """
ALTER TABLE api_prontocardio.contas_pagar_boletos
    ADD COLUMN IF NOT EXISTS prazo_minimo DATE,
    ADD COLUMN IF NOT EXISTS fora_do_prazo BOOLEAN NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS justificativa_atraso TEXT;
"""

DOWNGRADE_SQL = """
ALTER TABLE api_prontocardio.contas_pagar_boletos
    DROP COLUMN IF EXISTS justificativa_atraso,
    DROP COLUMN IF EXISTS fora_do_prazo,
    DROP COLUMN IF EXISTS prazo_minimo;
"""


def upgrade() -> None:
    op.execute(UPGRADE_SQL)


def downgrade() -> None:
    op.execute(DOWNGRADE_SQL)
