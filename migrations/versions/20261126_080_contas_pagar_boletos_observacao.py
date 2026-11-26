# ruff: noqa: E501
"""Contas a Pagar: observação do setor ao enviar a NFS-e ao financeiro.

DDL idempotente; o mesmo SQL está em docs/oracle/contas_pagar_boletos_observacao.sql.
"""

from alembic import op

revision = '20261126_080'
down_revision = '20261125_079'
branch_labels = None
depends_on = None

UPGRADE_SQL = """
ALTER TABLE api_prontocardio.contas_pagar_boletos
    ADD COLUMN IF NOT EXISTS observacao TEXT;
"""

DOWNGRADE_SQL = """
ALTER TABLE api_prontocardio.contas_pagar_boletos
    DROP COLUMN IF EXISTS observacao;
"""


def upgrade() -> None:
    op.execute(UPGRADE_SQL)


def downgrade() -> None:
    op.execute(DOWNGRADE_SQL)
