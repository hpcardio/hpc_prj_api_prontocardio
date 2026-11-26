"""Histórico imutável da gestão de tratativas da Agenda Médica."""

from alembic import op

revision = '20261126_081'
down_revision = '20261126_080'
branch_labels = None
depends_on = None

UPGRADE_SQL = """
CREATE TABLE IF NOT EXISTS api_prontocardio.agenda_medica_tratativas_historico (
    id SERIAL PRIMARY KEY,
    tratativa_id INTEGER NOT NULL,
    evento VARCHAR(40) NOT NULL,
    usuario_id INTEGER NOT NULL,
    usuario_nome VARCHAR(200) NOT NULL,
    status_anterior VARCHAR(30),
    status_novo VARCHAR(30),
    responsavel_anterior_id INTEGER,
    responsavel_anterior_nome VARCHAR(200),
    responsavel_novo_id INTEGER,
    responsavel_novo_nome VARCHAR(200),
    motivo TEXT,
    criado_em TIMESTAMP NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_agenda_tratativa_historico_tratativa
    ON api_prontocardio.agenda_medica_tratativas_historico
    (tratativa_id, criado_em);
CREATE INDEX IF NOT EXISTS ix_agenda_tratativa_historico_evento
    ON api_prontocardio.agenda_medica_tratativas_historico
    (evento, criado_em);
"""


def upgrade() -> None:
    op.execute(UPGRADE_SQL)


def downgrade() -> None:
    op.execute(
        'DROP TABLE IF EXISTS '
        'api_prontocardio.agenda_medica_tratativas_historico'
    )
