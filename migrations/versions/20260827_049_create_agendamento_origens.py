"""create auditable scheduling origins

Revision ID: 20260827_049
Revises: 20260826_048
Create Date: 2026-08-27 00:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import context, op

from app_prontocardio.settings import Settings

revision: str = '20260827_049'
down_revision: Union[str, Sequence[str], None] = '20260826_048'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA_NAME = Settings().POSTGRES_SCHEMA


def upgrade() -> None:
    bind = op.get_bind()
    inspector = None if context.is_offline_mode() else sa.inspect(bind)
    audit_exists = (
        inspector.has_table('auditoria_agendamentos', SCHEMA_NAME)
        if inspector is not None
        else False
    )
    if not audit_exists:
        op.create_table(
            'auditoria_agendamentos',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('operador_id', sa.Integer(), nullable=True),
            sa.Column('operador_nome', sa.String(200), nullable=False),
            sa.Column('origem', sa.String(30), nullable=False),
            sa.Column('cd_paciente', sa.BigInteger(), nullable=False),
            sa.Column('cd_item_agendamento', sa.BigInteger(), nullable=False),
            sa.Column('cd_it_agenda_central', sa.BigInteger(), nullable=False),
            sa.Column('cd_agenda_central', sa.BigInteger(), nullable=False),
            sa.Column('cd_tip_mar', sa.BigInteger(), nullable=True),
            sa.Column('protocolo_mv', sa.BigInteger(), nullable=True),
            sa.Column('status', sa.String(30), nullable=False),
            sa.Column('chave_efeito_lote', sa.String(100), nullable=True),
            sa.Column(
                'data_criacao',
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.UniqueConstraint(
                'chave_efeito_lote',
                name='uq_auditoria_agendamentos_chave_efeito_lote',
            ),
            schema=SCHEMA_NAME,
        )
    elif inspector is not None:
        columns = {
            column['name']
            for column in inspector.get_columns(
                'auditoria_agendamentos', schema=SCHEMA_NAME
            )
        }
        if 'chave_efeito_lote' not in columns:
            op.add_column(
                'auditoria_agendamentos',
                sa.Column('chave_efeito_lote', sa.String(100), nullable=True),
                schema=SCHEMA_NAME,
            )
        constraints = {
            item['name']
            for item in inspector.get_unique_constraints(
                'auditoria_agendamentos', schema=SCHEMA_NAME
            )
        }
        if 'uq_auditoria_agendamentos_chave_efeito_lote' not in constraints:
            op.create_unique_constraint(
                'uq_auditoria_agendamentos_chave_efeito_lote',
                'auditoria_agendamentos',
                ['chave_efeito_lote'],
                schema=SCHEMA_NAME,
            )
    op.create_table(
        'agendamento_origens',
        sa.Column('cd_it_agenda_central', sa.BigInteger(), primary_key=True),
        sa.Column('protocolo_mv', sa.BigInteger(), primary_key=True, nullable=False),
        sa.Column('cd_paciente', sa.BigInteger(), nullable=False),
        sa.Column('cd_agenda_central', sa.BigInteger(), nullable=True),
        sa.Column('origem', sa.String(30), nullable=False),
        sa.Column('evidencia', sa.String(40), nullable=False),
        sa.Column('referencia_externa', sa.String(160), nullable=True),
        sa.Column('slot_origem_anterior', sa.BigInteger(), nullable=True),
        sa.Column('protocolo_origem_anterior', sa.BigInteger(), nullable=True),
        sa.Column('registrada_por_id', sa.Integer(), nullable=True),
        sa.Column('registrada_por_nome', sa.String(200), nullable=False),
        sa.Column(
            'criada_em',
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            'atualizada_em',
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "origem IN ('MEU_PRONTOCARDIO','PRONTOREDE','PRONTOCHECKUP',"
            "'CALL_CENTER','MV','NAO_IDENTIFICADA')",
            name='ck_agendamento_origens_origem',
        ),
        sa.CheckConstraint(
            '(slot_origem_anterior IS NULL AND '
            'protocolo_origem_anterior IS NULL) OR '
            '(slot_origem_anterior IS NOT NULL AND '
            'protocolo_origem_anterior IS NOT NULL)',
            name='ck_agendamento_origens_instancia_anterior_completa',
        ),
        sa.ForeignKeyConstraint(
            ['slot_origem_anterior', 'protocolo_origem_anterior'],
            [
                f'{SCHEMA_NAME}.agendamento_origens.cd_it_agenda_central',
                f'{SCHEMA_NAME}.agendamento_origens.protocolo_mv',
            ],
            name='fk_agendamento_origens_instancia_anterior',
        ),
        schema=SCHEMA_NAME,
    )
    op.create_index(
        'ix_agendamento_origens_paciente_slot',
        'agendamento_origens',
        ['cd_paciente', 'cd_it_agenda_central', 'protocolo_mv'],
        schema=SCHEMA_NAME,
    )
    op.create_index(
        'ix_agendamento_origens_protocolo',
        'agendamento_origens',
        ['protocolo_mv'],
        schema=SCHEMA_NAME,
    )
    op.create_table(
        'agendamento_origem_eventos',
        sa.Column('id', sa.BigInteger(), primary_key=True),
        sa.Column('cd_it_agenda_central', sa.BigInteger(), nullable=False),
        sa.Column('protocolo_mv', sa.BigInteger(), nullable=False),
        sa.Column('origem_anterior', sa.String(30), nullable=True),
        sa.Column('origem_nova', sa.String(30), nullable=False),
        sa.Column('evidencia', sa.String(40), nullable=False),
        sa.Column('resultado', sa.String(20), nullable=False),
        sa.Column('justificativa', sa.String(300), nullable=False),
        sa.Column('registrada_por_id', sa.Integer(), nullable=True),
        sa.Column('registrada_por_nome', sa.String(200), nullable=False),
        sa.Column(
            'criada_em',
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ['cd_it_agenda_central', 'protocolo_mv'],
            [
                f'{SCHEMA_NAME}.agendamento_origens.cd_it_agenda_central',
                f'{SCHEMA_NAME}.agendamento_origens.protocolo_mv',
            ],
            name='fk_agendamento_origem_eventos_instancia',
        ),
        schema=SCHEMA_NAME,
    )


def downgrade() -> None:
    raise RuntimeError(
        'Migração irreversível em produção: use as feature flags e o rollback '
        'operacional para preservar o histórico de agendamentos.'
    )
