"""add user access management and password reset tokens

Revision ID: 20260622_009
Revises: 20260622_008
Create Date: 2026-06-22 18:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '20260622_009'
down_revision: Union[str, Sequence[str], None] = '20260622_008'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA_NAME = 'api_prontocardio'


def upgrade() -> None:
    op.add_column(
        'usuarios_api',
        sa.Column('perfil', sa.String(length=20), server_default='usuario', nullable=False),
        schema=SCHEMA_NAME,
    )
    op.add_column(
        'usuarios_api',
        sa.Column('ativo', sa.Boolean(), server_default=sa.text('true'), nullable=False),
        schema=SCHEMA_NAME,
    )
    op.execute(
        sa.text(
            f"UPDATE {SCHEMA_NAME}.usuarios_api SET perfil = 'ti' "
            f"WHERE id = (SELECT MIN(id) FROM {SCHEMA_NAME}.usuarios_api)"
        )
    )
    op.create_table(
        'tokens_redefinicao_senha',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('usuario_id', sa.Integer(), nullable=False),
        sa.Column('token_hash', sa.String(length=64), nullable=False),
        sa.Column('expira_em', sa.DateTime(timezone=True), nullable=False),
        sa.Column('utilizado', sa.Boolean(), server_default=sa.text('false'), nullable=False),
        sa.Column('data_criacao', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['usuario_id'], [f'{SCHEMA_NAME}.usuarios_api.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('token_hash'),
        schema=SCHEMA_NAME,
    )


def downgrade() -> None:
    op.drop_table('tokens_redefinicao_senha', schema=SCHEMA_NAME)
    op.drop_column('usuarios_api', 'ativo', schema=SCHEMA_NAME)
    op.drop_column('usuarios_api', 'perfil', schema=SCHEMA_NAME)
