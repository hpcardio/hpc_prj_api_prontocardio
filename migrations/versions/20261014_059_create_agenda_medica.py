"""create agenda medica tables

Revision ID: 20261014_059
Revises: 20261002_058
Create Date: 2026-10-14 09:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '20261014_059'
down_revision: Union[str, Sequence[str], None] = '20261002_058'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA_NAME = 'api_prontocardio'


def _indices(tabela: str, colunas: tuple[str, ...]) -> None:
    for coluna in colunas:
        op.create_index(
            f'ix_{tabela}_{coluna}', tabela, [coluna], schema=SCHEMA_NAME
        )


def upgrade() -> None:
    op.create_table(
        'agenda_medica_vinculos',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('usuario_id', sa.Integer(), nullable=False),
        sa.Column('cd_prestador', sa.Integer(), nullable=False),
        sa.Column('criado_por', sa.String(length=200), nullable=False),
        sa.Column('criado_em', sa.DateTime(), server_default=sa.func.now()),
        sa.UniqueConstraint('usuario_id'),
        schema=SCHEMA_NAME,
    )
    _indices('agenda_medica_vinculos', ('cd_prestador',))

    op.create_table(
        'agenda_medica_ofertas',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('cd_escala_modelo', sa.Integer(), nullable=False),
        sa.Column('data_agenda', sa.Date(), nullable=False),
        sa.Column('especialidade', sa.String(length=120), nullable=False),
        sa.Column('hr_inicio', sa.String(length=5), nullable=False),
        sa.Column('hr_fim', sa.String(length=5), nullable=False),
        sa.Column('cd_unidade_atendimento', sa.Integer(), nullable=False),
        sa.Column('criado_por_id', sa.Integer(), nullable=False),
        sa.Column('criado_por_nome', sa.String(length=200), nullable=False),
        sa.Column(
            'ds_unidade_atendimento', sa.String(length=120), nullable=True
        ),
        sa.Column('observacao', sa.Text(), nullable=True),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('criado_em', sa.DateTime(), server_default=sa.func.now()),
        schema=SCHEMA_NAME,
    )
    _indices(
        'agenda_medica_ofertas', ('cd_escala_modelo', 'data_agenda', 'status')
    )

    op.create_table(
        'agenda_medica_solicitacoes',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('tipo', sa.String(length=20), nullable=False),
        sa.Column('cd_prestador', sa.Integer(), nullable=False),
        sa.Column('criado_por_id', sa.Integer(), nullable=False),
        sa.Column('criado_por_nome', sa.String(length=200), nullable=False),
        sa.Column('oferta_id', sa.Integer(), nullable=True),
        sa.Column('cd_agenda_central', sa.Integer(), nullable=True),
        sa.Column('motivo', sa.Text(), nullable=True),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('decisao_por_id', sa.Integer(), nullable=True),
        sa.Column('decisao_por_nome', sa.String(length=200), nullable=True),
        sa.Column('decisao_em', sa.DateTime(), nullable=True),
        sa.Column('resultado_mv', sa.JSON(), nullable=True),
        sa.Column('criado_em', sa.DateTime(), server_default=sa.func.now()),
        schema=SCHEMA_NAME,
    )
    _indices(
        'agenda_medica_solicitacoes',
        ('tipo', 'cd_prestador', 'oferta_id', 'status'),
    )

    op.create_table(
        'agenda_medica_eventos',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('acao', sa.String(length=60), nullable=False),
        sa.Column('usuario_id', sa.Integer(), nullable=False),
        sa.Column('usuario_nome', sa.String(length=200), nullable=False),
        sa.Column('oferta_id', sa.Integer(), nullable=True),
        sa.Column('solicitacao_id', sa.Integer(), nullable=True),
        sa.Column('detalhe', sa.Text(), nullable=True),
        sa.Column('criado_em', sa.DateTime(), server_default=sa.func.now()),
        schema=SCHEMA_NAME,
    )
    _indices('agenda_medica_eventos', ('acao', 'solicitacao_id'))


def downgrade() -> None:
    for tabela in (
        'agenda_medica_eventos',
        'agenda_medica_solicitacoes',
        'agenda_medica_ofertas',
        'agenda_medica_vinculos',
    ):
        op.drop_table(tabela, schema=SCHEMA_NAME)
