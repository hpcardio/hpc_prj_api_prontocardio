"""add habilitado to prazos recurso convenio

Revision ID: 20260622_008
Revises: 20260617_007
Create Date: 2026-06-22 12:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '20260622_008'
down_revision: Union[str, Sequence[str], None] = '20260617_007'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA_NAME = 'api_prontocardio'


def upgrade() -> None:
    op.add_column(
        'prazos_recurso_convenio',
        sa.Column(
            'habilitado',
            sa.Boolean(),
            server_default=sa.text('true'),
            nullable=False,
        ),
        schema=SCHEMA_NAME,
    )


def downgrade() -> None:
    op.drop_column(
        'prazos_recurso_convenio',
        'habilitado',
        schema=SCHEMA_NAME,
    )
