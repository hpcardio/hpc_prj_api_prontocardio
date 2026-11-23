"""adiciona prazo de interesse às ofertas da agenda médica

Revision ID: 20261119_066
Revises: 20261104_065
Create Date: 2026-11-19 16:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '20261119_066'
down_revision: Union[str, Sequence[str], None] = '20261104_065'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None
SCHEMA = 'api_prontocardio'


def upgrade() -> None:
    op.add_column(
        'agenda_medica_ofertas',
        sa.Column('prazo_interesse', sa.DateTime(), nullable=True),
        schema=SCHEMA,
    )
    op.create_index(
        'ix_agenda_medica_ofertas_prazo_interesse',
        'agenda_medica_ofertas',
        ['prazo_interesse'],
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_index(
        'ix_agenda_medica_ofertas_prazo_interesse',
        table_name='agenda_medica_ofertas',
        schema=SCHEMA,
    )
    op.drop_column(
        'agenda_medica_ofertas', 'prazo_interesse', schema=SCHEMA
    )
