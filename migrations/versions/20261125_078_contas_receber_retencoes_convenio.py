# ruff: noqa: E501
"""Contas a Receber: percentuais de retenção por convênio.

DDL idempotente; o mesmo SQL está em docs/oracle/contas_receber_retencoes_convenio.sql.
"""

from alembic import op

revision = '20261125_078'
down_revision = '20261124_077'
branch_labels = None
depends_on = None

UPGRADE_SQL = """
CREATE TABLE IF NOT EXISTS api_prontocardio.cr_convenio_retencoes (
        cd_convenio integer PRIMARY KEY,
        convenio varchar(120) NOT NULL,
        iss numeric(6,3) NOT NULL DEFAULT 0,
        ir numeric(6,3) NOT NULL DEFAULT 0,
        pis numeric(6,3) NOT NULL DEFAULT 0,
        cofins numeric(6,3) NOT NULL DEFAULT 0,
        csll numeric(6,3) NOT NULL DEFAULT 0,
        observacao varchar(300),
        atualizado_por integer,
        atualizado_por_nome varchar(200),
        atualizado_em timestamptz NOT NULL DEFAULT now(),
        CONSTRAINT cr_convenio_retencoes_faixa CHECK (
            iss BETWEEN 0 AND 100 AND ir BETWEEN 0 AND 100 AND pis BETWEEN 0 AND 100
            AND cofins BETWEEN 0 AND 100 AND csll BETWEEN 0 AND 100
        )
    );
"""

DOWNGRADE_SQL = """
DROP TABLE IF EXISTS api_prontocardio.cr_convenio_retencoes;
"""


def upgrade() -> None:
    op.execute(UPGRADE_SQL)


def downgrade() -> None:
    op.execute(DOWNGRADE_SQL)
