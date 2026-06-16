"""add recurso fields to registros_glosa

Revision ID: 20260615_003
Revises: 20260612_002
Create Date: 2026-06-15 18:30:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '20260615_003'
down_revision: Union[str, Sequence[str], None] = '20260612_002'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA_NAME = 'api_prontocardio'


def upgrade() -> None:
    op.add_column(
        'registros_glosa',
        sa.Column('processo_recurso', sa.String(), nullable=True),
        schema=SCHEMA_NAME,
    )
    op.add_column(
        'registros_glosa',
        sa.Column('qtd_glosada', sa.Numeric(12, 2), nullable=True),
        schema=SCHEMA_NAME,
    )
    op.add_column(
        'registros_glosa',
        sa.Column('valor_glosado', sa.Numeric(12, 2), nullable=True),
        schema=SCHEMA_NAME,
    )
    op.add_column(
        'registros_glosa',
        sa.Column('dt_recurso', sa.Date(), nullable=True),
        schema=SCHEMA_NAME,
    )
    op.add_column(
        'registros_glosa',
        sa.Column('dt_pagamento', sa.Date(), nullable=True),
        schema=SCHEMA_NAME,
    )


def downgrade() -> None:
    op.drop_column(
        'registros_glosa',
        'dt_pagamento',
        schema=SCHEMA_NAME,
    )
    op.drop_column(
        'registros_glosa',
        'dt_recurso',
        schema=SCHEMA_NAME,
    )
    op.drop_column(
        'registros_glosa',
        'valor_glosado',
        schema=SCHEMA_NAME,
    )
    op.drop_column(
        'registros_glosa',
        'qtd_glosada',
        schema=SCHEMA_NAME,
    )
    op.drop_column(
        'registros_glosa',
        'processo_recurso',
        schema=SCHEMA_NAME,
    )
