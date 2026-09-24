"""create whatsapp idempotent delivery table

Revision ID: 20260918_057
Revises: 20260917_056
Create Date: 2026-09-18 17:30:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '20260918_057'
down_revision: Union[str, Sequence[str], None] = '20260917_056'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA_NAME = 'api_prontocardio'


def upgrade() -> None:
    op.create_table(
        'whatsapp_envios_idempotentes',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('chave', sa.String(length=160), nullable=False),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('id_externo', sa.String(length=255), nullable=True),
        sa.Column('telefone_final', sa.String(length=4), nullable=True),
        sa.Column('erro_sanitizado', sa.String(length=240), nullable=True),
        sa.Column('criado_em', sa.DateTime(), server_default=sa.func.now()),
        sa.Column(
            'atualizado_em', sa.DateTime(), server_default=sa.func.now()
        ),
        sa.UniqueConstraint('chave'),
        schema=SCHEMA_NAME,
    )


def downgrade() -> None:
    op.drop_table('whatsapp_envios_idempotentes', schema=SCHEMA_NAME)
