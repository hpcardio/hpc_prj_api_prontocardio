"""Responsável e momento de aceite das tratativas da Agenda Médica."""

from alembic import op

revision = '20261125_078'
down_revision = '20261124_077'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    ALTER TABLE api_prontocardio.agenda_medica_tratativas_paciente
        ADD COLUMN IF NOT EXISTS assumido_por_id INTEGER,
        ADD COLUMN IF NOT EXISTS assumido_por_nome VARCHAR(200),
        ADD COLUMN IF NOT EXISTS assumido_em TIMESTAMP;
    CREATE INDEX IF NOT EXISTS ix_agenda_tratativa_responsavel
        ON api_prontocardio.agenda_medica_tratativas_paciente
        (assumido_por_id, status);
    """)


def downgrade() -> None:
    op.execute("""
    DROP INDEX IF EXISTS api_prontocardio.ix_agenda_tratativa_responsavel;
    ALTER TABLE api_prontocardio.agenda_medica_tratativas_paciente
        DROP COLUMN IF EXISTS assumido_em,
        DROP COLUMN IF EXISTS assumido_por_nome,
        DROP COLUMN IF EXISTS assumido_por_id;
    """)
