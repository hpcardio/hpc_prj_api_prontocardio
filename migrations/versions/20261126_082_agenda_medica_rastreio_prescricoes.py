"""Rastreio de exames prescritos nos atendimentos ambulatoriais."""

from alembic import op

revision = '20261126_082'
down_revision = '20261126_081'
branch_labels = None
depends_on = None

UPGRADE_SQL = """
CREATE TABLE IF NOT EXISTS api_prontocardio.agenda_medica_rastreio_prescricoes (
    id SERIAL PRIMARY KEY,
    cd_itpre_med INTEGER NOT NULL UNIQUE,
    cd_pre_med INTEGER NOT NULL,
    cd_atendimento INTEGER NOT NULL,
    cd_paciente INTEGER NOT NULL,
    cd_tip_presc INTEGER NOT NULL,
    cd_item_agendamento INTEGER NOT NULL,
    codigo_tuss VARCHAR(30),
    cd_prestador INTEGER,
    data_atendimento TIMESTAMP NOT NULL,
    data_prescricao TIMESTAMP NOT NULL,
    elegivel_em DATE NOT NULL,
    status VARCHAR(30) NOT NULL DEFAULT 'AGUARDANDO_D12',
    cd_it_agenda_central INTEGER,
    protocolo_mv INTEGER,
    motivo_descarte TEXT,
    atualizado_por_id INTEGER,
    atualizado_por_nome VARCHAR(200),
    criado_em TIMESTAMP NOT NULL DEFAULT NOW(),
    atualizado_em TIMESTAMP NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_rastreio_prescricao_status_elegivel
    ON api_prontocardio.agenda_medica_rastreio_prescricoes
    (status, elegivel_em);
CREATE INDEX IF NOT EXISTS ix_rastreio_prescricao_paciente
    ON api_prontocardio.agenda_medica_rastreio_prescricoes
    (cd_paciente, cd_item_agendamento);
ALTER TABLE api_prontocardio.agendamento_origens
    DROP CONSTRAINT IF EXISTS ck_agendamento_origens_origem;
ALTER TABLE api_prontocardio.agendamento_origens
    ADD CONSTRAINT ck_agendamento_origens_origem CHECK (
        origem IN ('MEU_PRONTOCARDIO','PRONTOREDE','PRONTOCHECKUP',
        'CALL_CENTER','MV','NAO_IDENTIFICADA','PRESCRICAO_AMBULATORIAL')
    );
"""


def upgrade() -> None:
    op.execute(UPGRADE_SQL)


def downgrade() -> None:
    raise RuntimeError(
        'Migração irreversível: preserve o histórico do rastreio.'
    )
