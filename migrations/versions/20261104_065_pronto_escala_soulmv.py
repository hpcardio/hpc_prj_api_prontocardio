"""pronto escala: sincronizacao idempotente com agendas do SoulMV

Revision ID: 20261104_065
Revises: 20261104_064
Create Date: 2026-11-04 10:05:00.000000
"""

from typing import Sequence, Union

from alembic import op

revision: str = '20261104_065'
down_revision: Union[str, Sequence[str], None] = '20261104_064'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None
SCHEMA = 'api_prontocardio'


def upgrade() -> None:
    op.create_unique_constraint(
        'uq_pronto_escala_plantoes_cd_agenda_central',
        'pronto_escala_plantoes',
        ['cd_agenda_central'],
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_constraint(
        'uq_pronto_escala_plantoes_cd_agenda_central',
        'pronto_escala_plantoes',
        schema=SCHEMA,
        type_='unique',
    )
