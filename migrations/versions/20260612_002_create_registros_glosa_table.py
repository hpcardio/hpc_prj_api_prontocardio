"""create registros_glosa table

Revision ID: 20260612_002
Revises: 20260602_001
Create Date: 2026-06-12 00:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '20260612_002'
down_revision: Union[str, Sequence[str], None] = '20260602_001'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA_NAME = 'api_prontocardio'


def upgrade() -> None:
    op.create_table(
        'registros_glosa',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('codigo_paciente', sa.Integer(), nullable=False),
        sa.Column('cd_remessa', sa.Integer(), nullable=False),
        sa.Column('cd_atendimento', sa.Integer(), nullable=False),
        sa.Column('conta', sa.Integer(), nullable=False),
        sa.Column('cd_prestador', sa.Integer(), nullable=False),
        sa.Column('cd_convenio', sa.Integer(), nullable=False),
        sa.Column('tp_atendimento', sa.String(), nullable=False),
        sa.Column('procedimento', sa.String(), nullable=False),
        sa.Column('convenio', sa.String(), nullable=False),
        sa.Column('guia', sa.String(), nullable=False),
        sa.Column('prestador', sa.String(), nullable=False),
        sa.Column('data_atendimento', sa.DateTime(), nullable=False),
        sa.Column('valor', sa.Numeric(12, 2), nullable=False),
        sa.Column(
            'processo_controle_fatura_gab', sa.String(), nullable=False
        ),
        sa.Column('data_glosa', sa.Date(), nullable=False),
        sa.Column('motivo_glosa', sa.String(), nullable=False),
        sa.Column('descricao_glosa', sa.String(), nullable=False),
        sa.Column(
            'sn_glosado',
            sa.Boolean(),
            nullable=False,
            server_default=sa.text('true'),
        ),
        sa.Column(
            'data_criacao',
            sa.DateTime(),
            server_default=sa.text('now()'),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint('id'),
        schema=SCHEMA_NAME,
    )


def downgrade() -> None:
    op.drop_table('registros_glosa', schema=SCHEMA_NAME)
