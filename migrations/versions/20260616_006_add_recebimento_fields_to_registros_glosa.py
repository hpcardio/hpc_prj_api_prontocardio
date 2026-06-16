"""add recebimento fields to registros_glosa

Revision ID: 20260616_006
Revises: 20260616_005
Create Date: 2026-06-16 17:10:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '20260616_006'
down_revision: Union[str, Sequence[str], None] = '20260616_005'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA_NAME = 'api_prontocardio'


def upgrade() -> None:
    op.add_column(
        'registros_glosa',
        sa.Column('nm_paciente', sa.String(), nullable=True),
        schema=SCHEMA_NAME,
    )
    op.add_column(
        'registros_glosa',
        sa.Column('dt_recebimento', sa.Date(), nullable=True),
        schema=SCHEMA_NAME,
    )
    op.add_column(
        'registros_glosa',
        sa.Column('valor_recebido', sa.Numeric(12, 2), nullable=True),
        schema=SCHEMA_NAME,
    )
    op.add_column(
        'registros_glosa',
        sa.Column('observacao_recebimento', sa.String(), nullable=True),
        schema=SCHEMA_NAME,
    )


def downgrade() -> None:
    op.drop_column(
        'registros_glosa',
        'observacao_recebimento',
        schema=SCHEMA_NAME,
    )
    op.drop_column(
        'registros_glosa',
        'valor_recebido',
        schema=SCHEMA_NAME,
    )
    op.drop_column(
        'registros_glosa',
        'dt_recebimento',
        schema=SCHEMA_NAME,
    )
    op.drop_column(
        'registros_glosa',
        'nm_paciente',
        schema=SCHEMA_NAME,
    )
