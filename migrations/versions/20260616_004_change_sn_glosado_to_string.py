"""change sn_glosado to string

Revision ID: 20260616_004
Revises: 20260615_003
Create Date: 2026-06-16 15:30:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '20260616_004'
down_revision: Union[str, Sequence[str], None] = '20260615_003'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA_NAME = 'api_prontocardio'


def upgrade() -> None:
    op.alter_column(
        'registros_glosa',
        'sn_glosado',
        schema=SCHEMA_NAME,
        existing_type=sa.Boolean(),
        type_=sa.String(),
        existing_nullable=False,
        server_default=sa.text("'true'"),
        postgresql_using=(
            "CASE WHEN sn_glosado THEN 'true' ELSE 'not' END"
        ),
    )
    op.alter_column(
        'registros_glosa',
        'data_criacao',
        schema=SCHEMA_NAME,
        existing_type=sa.DateTime(),
        existing_nullable=False,
        server_default=sa.text("timezone('America/Sao_Paulo', now())"),
    )


def downgrade() -> None:
    op.alter_column(
        'registros_glosa',
        'data_criacao',
        schema=SCHEMA_NAME,
        existing_type=sa.DateTime(),
        existing_nullable=False,
        server_default=sa.text('now()'),
    )
    op.alter_column(
        'registros_glosa',
        'sn_glosado',
        schema=SCHEMA_NAME,
        existing_type=sa.String(),
        type_=sa.Boolean(),
        existing_nullable=False,
        server_default=sa.text('true'),
        postgresql_using="sn_glosado = 'true'",
    )
