"""create patient OTP challenges and sessions

Revision ID: 20260810_038
Revises: 20260729_037
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '20260810_038'
down_revision: Union[str, Sequence[str], None] = '20260809_038'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA_NAME = 'api_prontocardio'


def upgrade() -> None:
    op.create_table(
        'desafios_otp_paciente',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('desafio_id', sa.String(64), nullable=False, unique=True),
        sa.Column('cd_paciente', sa.Integer(), nullable=False),
        sa.Column('nm_paciente', sa.String(200), nullable=False),
        sa.Column('cpf_hash', sa.String(64), nullable=False),
        sa.Column('telefone_final', sa.String(4), nullable=False),
        sa.Column('codigo_hash', sa.String(64), nullable=False),
        sa.Column('expira_em', sa.DateTime(timezone=True), nullable=False),
        sa.Column('proximo_envio_em', sa.DateTime(timezone=True), nullable=False),
        sa.Column('tentativas', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('utilizado_em', sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            'data_criacao',
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        schema=SCHEMA_NAME,
    )
    op.create_index(
        'ix_desafios_otp_paciente_cpf_hash',
        'desafios_otp_paciente',
        ['cpf_hash', 'data_criacao'],
        schema=SCHEMA_NAME,
    )
    op.create_table(
        'sessoes_paciente',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('token_hash', sa.String(64), nullable=False, unique=True),
        sa.Column('cd_paciente', sa.Integer(), nullable=False),
        sa.Column('nm_paciente', sa.String(200), nullable=False),
        sa.Column('expira_em', sa.DateTime(timezone=True), nullable=False),
        sa.Column('revogado_em', sa.DateTime(timezone=True), nullable=True),
        sa.Column('ultimo_acesso_em', sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            'data_criacao',
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        schema=SCHEMA_NAME,
    )
    op.create_index(
        'ix_sessoes_paciente_token_hash',
        'sessoes_paciente',
        ['token_hash'],
        schema=SCHEMA_NAME,
    )
    op.create_index(
        'ix_sessoes_paciente_paciente',
        'sessoes_paciente',
        ['cd_paciente', 'expira_em'],
        schema=SCHEMA_NAME,
    )


def downgrade() -> None:
    op.drop_index(
        'ix_sessoes_paciente_paciente',
        table_name='sessoes_paciente',
        schema=SCHEMA_NAME,
    )
    op.drop_index(
        'ix_sessoes_paciente_token_hash',
        table_name='sessoes_paciente',
        schema=SCHEMA_NAME,
    )
    op.drop_table('sessoes_paciente', schema=SCHEMA_NAME)
    op.drop_index(
        'ix_desafios_otp_paciente_cpf_hash',
        table_name='desafios_otp_paciente',
        schema=SCHEMA_NAME,
    )
    op.drop_table('desafios_otp_paciente', schema=SCHEMA_NAME)
