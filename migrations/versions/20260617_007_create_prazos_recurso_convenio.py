"""create prazos recurso convenio

Revision ID: 20260617_007
Revises: 20260616_006
Create Date: 2026-06-17 11:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '20260617_007'
down_revision: Union[str, Sequence[str], None] = '20260616_006'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA_NAME = 'api_prontocardio'


def upgrade() -> None:
    op.create_table(
        'prazos_recurso_convenio',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('cd_convenio', sa.Integer(), nullable=False),
        sa.Column('convenio', sa.String(), nullable=False),
        sa.Column('dias_para_recurso', sa.Integer(), nullable=False),
        sa.Column(
            'data_atualizacao',
            sa.DateTime(),
            server_default=sa.text("timezone('America/Sao_Paulo', now())"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint(
            'cd_convenio',
            name='uq_prazos_recurso_cd_convenio',
        ),
        schema=SCHEMA_NAME,
    )


def downgrade() -> None:
    op.drop_table('prazos_recurso_convenio', schema=SCHEMA_NAME)
