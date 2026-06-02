"""create api_prontocardio schema and usuarios_api table

Revision ID: 20260602_001
Revises:
Create Date: 2026-06-02 00:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '20260602_001'
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA_NAME = 'api_prontocardio'


def upgrade() -> None:
    op.execute(sa.text(f'CREATE SCHEMA IF NOT EXISTS "{SCHEMA_NAME}"'))
    op.create_table(
        'usuarios_api',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('nome', sa.String(), nullable=False),
        sa.Column('email', sa.String(), nullable=False),
        sa.Column('senha', sa.String(), nullable=False),
        sa.Column(
            'data_criacao',
            sa.DateTime(),
            server_default=sa.text('now()'),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('email'),
        sa.UniqueConstraint('nome'),
        schema=SCHEMA_NAME,
    )


def downgrade() -> None:
    op.drop_table('usuarios_api', schema=SCHEMA_NAME)
    op.execute(sa.text(f'DROP SCHEMA IF EXISTS "{SCHEMA_NAME}"'))
