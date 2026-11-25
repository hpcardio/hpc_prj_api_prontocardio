"""Login do Receita Certa pelo CPF.

DDL idempotente; o mesmo SQL está em docs/oracle/usuarios_api_cpf.sql.
"""

from alembic import op

revision = '20261125_079'
down_revision = '20261125_078'
branch_labels = None
depends_on = None

UPGRADE_SQL = """
ALTER TABLE api_prontocardio.usuarios_api
    ADD COLUMN IF NOT EXISTS cpf VARCHAR(11);

CREATE UNIQUE INDEX IF NOT EXISTS usuarios_api_cpf_uk
    ON api_prontocardio.usuarios_api (cpf)
 WHERE cpf IS NOT NULL;
"""

DOWNGRADE_SQL = """
DROP INDEX IF EXISTS api_prontocardio.usuarios_api_cpf_uk;
ALTER TABLE api_prontocardio.usuarios_api DROP COLUMN IF EXISTS cpf;
"""


def upgrade() -> None:
    op.execute(UPGRADE_SQL)


def downgrade() -> None:
    op.execute(DOWNGRADE_SQL)
