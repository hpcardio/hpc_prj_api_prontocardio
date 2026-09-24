"""add explicit scheduling origin to users

Revision ID: 20260827_050
Revises: 20260827_049
Create Date: 2026-08-27 00:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from app_prontocardio.settings import Settings

revision: str = '20260827_050'
down_revision: Union[str, Sequence[str], None] = '20260827_049'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA_NAME = Settings().POSTGRES_SCHEMA


def upgrade() -> None:
    op.add_column(
        'usuarios_api',
        sa.Column(
            'origem_agendamento',
            sa.String(30),
            nullable=False,
            server_default='NAO_IDENTIFICADA',
        ),
        schema=SCHEMA_NAME,
    )
    op.create_check_constraint(
        'ck_usuarios_api_origem_agendamento',
        'usuarios_api',
        "origem_agendamento IN ('MEU_PRONTOCARDIO','PRONTOREDE','PRONTOCHECKUP','CALL_CENTER','MV','NAO_IDENTIFICADA')",
        schema=SCHEMA_NAME,
    )


def downgrade() -> None:
    # Exercitado somente pelo ciclo PostgreSQL descartável do CI. O runbook de
    # produção mantém rollback operacional por flags, sem downgrade Alembic.
    op.drop_constraint(
        'ck_usuarios_api_origem_agendamento',
        'usuarios_api',
        type_='check',
        schema=SCHEMA_NAME,
    )
    op.drop_column(
        'usuarios_api',
        'origem_agendamento',
        schema=SCHEMA_NAME,
    )
