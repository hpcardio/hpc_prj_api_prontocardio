"""create repasse mv audit trail

Revision ID: 20261002_058
Revises: 20260918_057
Create Date: 2026-10-02 20:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '20261002_058'
down_revision: Union[str, Sequence[str], None] = '20260918_057'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA_NAME = 'api_prontocardio'


def upgrade() -> None:
    op.create_table(
        'repasse_mv_operacoes',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('operacao_id', sa.String(length=36), nullable=False),
        sa.Column('acao', sa.String(length=20), nullable=False),
        sa.Column('estado', sa.String(length=30), nullable=False),
        sa.Column('usuario_id', sa.Integer(), nullable=False),
        sa.Column('usuario_nome', sa.String(length=255), nullable=False),
        sa.Column('usuario_email', sa.String(length=255), nullable=False),
        sa.Column('cd_con_pag', sa.BigInteger(), nullable=True),
        sa.Column('nr_documento', sa.String(length=100), nullable=True),
        sa.Column('cd_repasse', sa.BigInteger(), nullable=True),
        sa.Column('competencia', sa.Date(), nullable=True),
        sa.Column('cd_multi_empresa', sa.Integer(), nullable=True),
        sa.Column('usuario_mv', sa.String(length=100), nullable=True),
        sa.Column('motivo', sa.Text(), nullable=True),
        sa.Column('estado_anterior', sa.JSON(), nullable=True),
        sa.Column('estado_posterior', sa.JSON(), nullable=True),
        sa.Column('mensagem', sa.String(length=500), nullable=True),
        sa.Column('criado_em', sa.DateTime(), server_default=sa.func.now()),
        sa.Column('concluido_em', sa.DateTime(), nullable=True),
        sa.UniqueConstraint('operacao_id'),
        schema=SCHEMA_NAME,
    )
    for coluna in (
        'cd_con_pag',
        'cd_repasse',
        'competencia',
        'usuario_id',
        'estado',
    ):
        op.create_index(
            f'ix_repasse_mv_operacoes_{coluna}',
            'repasse_mv_operacoes',
            [coluna],
            schema=SCHEMA_NAME,
        )
    op.create_index(
        'uq_repasse_mv_envio_ativo',
        'repasse_mv_operacoes',
        ['competencia', 'cd_multi_empresa'],
        unique=True,
        schema=SCHEMA_NAME,
        postgresql_where=sa.text(
            "acao = 'enviar' AND estado IN "
            "('em_processamento', 'reconciliacao_pendente')"
        ),
    )


def downgrade() -> None:
    op.drop_table('repasse_mv_operacoes', schema=SCHEMA_NAME)
