"""Central de Atendimento: idempotência e auditoria técnica sem PII."""

from alembic import op

revision = '20261123_075'
down_revision = '20261123_074'
branch_labels = None
depends_on = None

UPGRADE_SQL = """
CREATE TABLE IF NOT EXISTS api_prontocardio.central_atendimento_tentativas (
    id BIGSERIAL PRIMARY KEY,
    attempt_id VARCHAR(36) NOT NULL,
    command_hash VARCHAR(64) NOT NULL,
    state VARCHAR(30) NOT NULL,
    rule_version VARCHAR(40) NOT NULL,
    unit_code INTEGER NOT NULL,
    attendance_type VARCHAR(20) NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (attempt_id),
    CONSTRAINT ck_central_state CHECK (state IN (
        'RECEBIDA',
        'PRE_VALIDADA',
        'BLOQUEADA_ESCRITA',
        'CONFLITO',
        'FALHA_LEITURA'
    ))
);
CREATE INDEX IF NOT EXISTS ix_central_tentativas_state
    ON api_prontocardio.central_atendimento_tentativas (state);
CREATE INDEX IF NOT EXISTS ix_central_tentativas_unit_type
    ON api_prontocardio.central_atendimento_tentativas
    (unit_code, attendance_type);

CREATE TABLE IF NOT EXISTS api_prontocardio.central_atendimento_eventos (
    id BIGSERIAL PRIMARY KEY,
    attempt_id VARCHAR(36) NOT NULL,
    operator_id INTEGER NOT NULL,
    route VARCHAR(80) NOT NULL,
    previous_state VARCHAR(30),
    new_state VARCHAR(30) NOT NULL,
    result_code VARCHAR(60) NOT NULL,
    unit_code INTEGER NOT NULL,
    attendance_type VARCHAR(20) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_central_eventos_attempt_id
    ON api_prontocardio.central_atendimento_eventos (attempt_id);
CREATE INDEX IF NOT EXISTS ix_central_eventos_created_at
    ON api_prontocardio.central_atendimento_eventos (created_at);
"""

DOWNGRADE_SQL = """
DROP TABLE IF EXISTS api_prontocardio.central_atendimento_eventos;
DROP TABLE IF EXISTS api_prontocardio.central_atendimento_tentativas;
"""


def upgrade() -> None:
    op.execute(UPGRADE_SQL)


def downgrade() -> None:
    op.execute(DOWNGRADE_SQL)
