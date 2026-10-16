"""agenda medica: nivel do medico e triagem de pacientes

Revision ID: 20261015_060
Revises: 20261014_059
Create Date: 2026-10-15 09:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '20261015_060'
down_revision: Union[str, Sequence[str], None] = '20261014_059'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA_NAME = 'api_prontocardio'


def upgrade() -> None:
    op.add_column(
        'agenda_medica_vinculos',
        sa.Column(
            'nivel',
            sa.String(length=10),
            nullable=False,
            server_default='pleno',
        ),
        schema=SCHEMA_NAME,
    )
    op.create_table(
        'agenda_medica_triagem',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('cd_it_agenda_central', sa.Integer(), nullable=False),
        sa.Column('cd_paciente', sa.Integer(), nullable=False),
        sa.Column('decisao', sa.String(length=10), nullable=False),
        sa.Column('cd_prestador_senior', sa.Integer(), nullable=False),
        sa.Column('usuario_id', sa.Integer(), nullable=False),
        sa.Column('usuario_nome', sa.String(length=200), nullable=False),
        sa.Column('decidido_em', sa.DateTime(), server_default=sa.func.now()),
        sa.UniqueConstraint('cd_it_agenda_central'),
        schema=SCHEMA_NAME,
    )
    for coluna in ('cd_paciente', 'cd_prestador_senior'):
        op.create_index(
            f'ix_agenda_medica_triagem_{coluna}',
            'agenda_medica_triagem',
            [coluna],
            schema=SCHEMA_NAME,
        )


def downgrade() -> None:
    for coluna in ('cd_paciente', 'cd_prestador_senior'):
        op.drop_index(
            f'ix_agenda_medica_triagem_{coluna}',
            table_name='agenda_medica_triagem',
            schema=SCHEMA_NAME,
        )
    op.drop_table('agenda_medica_triagem', schema=SCHEMA_NAME)
    op.drop_column('agenda_medica_vinculos', 'nivel', schema=SCHEMA_NAME)
