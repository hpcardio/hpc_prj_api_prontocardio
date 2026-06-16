"""add sn_ativo to registros_glosa

Revision ID: 20260616_005
Revises: 20260616_004
Create Date: 2026-06-16 16:20:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '20260616_005'
down_revision: Union[str, Sequence[str], None] = '20260616_004'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA_NAME = 'api_prontocardio'


def upgrade() -> None:
    op.add_column(
        'registros_glosa',
        sa.Column(
            'sn_ativo',
            sa.String(),
            nullable=False,
            server_default=sa.text("'true'"),
        ),
        schema=SCHEMA_NAME,
    )


def downgrade() -> None:
    op.drop_column(
        'registros_glosa',
        'sn_ativo',
        schema=SCHEMA_NAME,
    )
