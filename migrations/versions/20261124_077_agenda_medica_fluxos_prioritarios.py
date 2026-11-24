"""Fluxos prioritários da Agenda Médica sem dados pessoais."""

from alembic import op

revision = '20261124_077'
down_revision = '20261124_076'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    CREATE TABLE IF NOT EXISTS api_prontocardio.agenda_medica_contrapropostas (
        id SERIAL PRIMARY KEY,
        solicitacao_id INTEGER NOT NULL,
        data_agenda DATE NOT NULL,
        hr_inicio VARCHAR(5) NOT NULL,
        hr_fim VARCHAR(5) NOT NULL,
        motivo TEXT NOT NULL,
        criado_por_id INTEGER NOT NULL,
        criado_por_nome VARCHAR(200) NOT NULL,
        status VARCHAR(30) NOT NULL DEFAULT 'AGUARDANDO_MEDICO',
        respondido_por_id INTEGER,
        respondido_por_nome VARCHAR(200),
        resposta_motivo TEXT,
        respondido_em TIMESTAMP,
        criado_em TIMESTAMP NOT NULL DEFAULT now(),
        CONSTRAINT ck_agenda_contraproposta_status CHECK (status IN (
            'AGUARDANDO_MEDICO', 'ACEITA', 'RECUSADA', 'CANCELADA'
        ))
    );
    CREATE INDEX IF NOT EXISTS ix_agenda_contraproposta_solicitacao
        ON api_prontocardio.agenda_medica_contrapropostas (solicitacao_id);
    CREATE INDEX IF NOT EXISTS ix_agenda_contraproposta_status
        ON api_prontocardio.agenda_medica_contrapropostas (status);

    CREATE TABLE IF NOT EXISTS api_prontocardio.agenda_medica_tratativas_paciente (
        id SERIAL PRIMARY KEY,
        cd_it_agenda_central INTEGER NOT NULL,
        cd_agenda_central INTEGER,
        solicitacao_id INTEGER,
        tipo VARCHAR(30) NOT NULL,
        status VARCHAR(30) NOT NULL,
        cd_it_agenda_destino INTEGER,
        valor_anterior VARCHAR(40),
        valor_novo VARCHAR(40),
        motivo TEXT,
        criado_por_id INTEGER NOT NULL,
        criado_por_nome VARCHAR(200) NOT NULL,
        concluido_por_id INTEGER,
        concluido_por_nome VARCHAR(200),
        concluido_em TIMESTAMP,
        criado_em TIMESTAMP NOT NULL DEFAULT now(),
        atualizado_em TIMESTAMP NOT NULL DEFAULT now(),
        CONSTRAINT ck_agenda_tratativa_tipo CHECK (tipo IN (
            'NAO_VIRA', 'REMANEJAMENTO', 'CORRECAO_CONFIRMACAO'
        )),
        CONSTRAINT ck_agenda_tratativa_status CHECK (status IN (
            'PENDENTE', 'EM_TRATATIVA', 'CONCLUIDA', 'CANCELADA', 'FALHA'
        ))
    );
    CREATE INDEX IF NOT EXISTS ix_agenda_tratativa_horario
        ON api_prontocardio.agenda_medica_tratativas_paciente
        (cd_it_agenda_central);
    CREATE INDEX IF NOT EXISTS ix_agenda_tratativa_fila
        ON api_prontocardio.agenda_medica_tratativas_paciente
        (tipo, status, criado_em);
    CREATE UNIQUE INDEX IF NOT EXISTS uq_agenda_tratativa_nao_vira_aberta
        ON api_prontocardio.agenda_medica_tratativas_paciente
        (cd_it_agenda_central, tipo)
        WHERE status IN ('PENDENTE', 'EM_TRATATIVA');
    """)


def downgrade() -> None:
    op.execute("""
    DROP TABLE IF EXISTS api_prontocardio.agenda_medica_tratativas_paciente;
    DROP TABLE IF EXISTS api_prontocardio.agenda_medica_contrapropostas;
    """)
