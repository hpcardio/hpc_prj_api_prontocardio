"""pronto escala: plantoes, registros, justificativas e auditoria

Revision ID: 20261027_062
Revises: 20261026_061
Create Date: 2026-10-27 10:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '20261027_062'
down_revision: Union[str, Sequence[str], None] = '20261026_061'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None
SCHEMA = 'api_prontocardio'


def upgrade() -> None:
    op.create_table(
        'pronto_escala_parametros',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column(
            'antecedencia_checkin_min',
            sa.Integer(),
            nullable=False,
            server_default='60',
        ),
        sa.Column(
            'atraso_checkin_min',
            sa.Integer(),
            nullable=False,
            server_default='30',
        ),
        sa.Column(
            'antecedencia_checkout_min',
            sa.Integer(),
            nullable=False,
            server_default='0',
        ),
        sa.Column(
            'atraso_checkout_min',
            sa.Integer(),
            nullable=False,
            server_default='120',
        ),
        sa.Column('atualizado_por_id', sa.Integer()),
        sa.Column('atualizado_por_nome', sa.String(200)),
        sa.Column(
            'atualizado_em', sa.DateTime(), server_default=sa.func.now()
        ),
        schema=SCHEMA,
    )
    op.create_table(
        'pronto_escala_plantoes',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('cd_prestador', sa.Integer(), nullable=False),
        sa.Column('nm_prestador', sa.String(200), nullable=False),
        sa.Column('setor', sa.String(160), nullable=False),
        sa.Column('inicio', sa.DateTime(), nullable=False),
        sa.Column('fim', sa.DateTime(), nullable=False),
        sa.Column('criado_por_id', sa.Integer(), nullable=False),
        sa.Column('criado_por_nome', sa.String(200), nullable=False),
        sa.Column('cd_agenda_central', sa.Integer()),
        sa.Column(
            'origem', sa.String(30), nullable=False, server_default='MANUAL'
        ),
        sa.Column(
            'status',
            sa.String(20),
            nullable=False,
            server_default='PROGRAMADO',
        ),
        sa.Column('criado_em', sa.DateTime(), server_default=sa.func.now()),
        schema=SCHEMA,
    )
    op.create_table(
        'pronto_escala_registros',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('plantao_id', sa.Integer(), nullable=False),
        sa.Column('tipo', sa.String(10), nullable=False),
        sa.Column('registrado_em', sa.DateTime(), nullable=False),
        sa.Column('usuario_id', sa.Integer(), nullable=False),
        sa.Column('usuario_nome', sa.String(200), nullable=False),
        sa.Column(
            'status',
            sa.String(20),
            nullable=False,
            server_default='REGISTRADO',
        ),
        sa.UniqueConstraint('plantao_id', 'tipo'),
        schema=SCHEMA,
    )
    op.create_table(
        'pronto_escala_justificativas',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('plantao_id', sa.Integer(), nullable=False),
        sa.Column('tipo', sa.String(10), nullable=False),
        sa.Column('texto', sa.Text(), nullable=False),
        sa.Column('usuario_id', sa.Integer(), nullable=False),
        sa.Column('usuario_nome', sa.String(200), nullable=False),
        sa.Column(
            'status', sa.String(20), nullable=False, server_default='PENDENTE'
        ),
        sa.Column('decisao_por_id', sa.Integer()),
        sa.Column('decisao_por_nome', sa.String(200)),
        sa.Column('decisao_em', sa.DateTime()),
        sa.Column('motivo_decisao', sa.Text()),
        sa.Column('criado_em', sa.DateTime(), server_default=sa.func.now()),
        sa.UniqueConstraint('plantao_id', 'tipo'),
        schema=SCHEMA,
    )
    op.create_table(
        'pronto_escala_eventos',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('acao', sa.String(60), nullable=False),
        sa.Column('usuario_id', sa.Integer(), nullable=False),
        sa.Column('usuario_nome', sa.String(200), nullable=False),
        sa.Column('plantao_id', sa.Integer()),
        sa.Column('justificativa_id', sa.Integer()),
        sa.Column('detalhe', sa.Text()),
        sa.Column('criado_em', sa.DateTime(), server_default=sa.func.now()),
        schema=SCHEMA,
    )
    for tabela, colunas in {
        'pronto_escala_plantoes': (
            'cd_prestador',
            'setor',
            'inicio',
            'fim',
            'cd_agenda_central',
            'status',
        ),
        'pronto_escala_registros': ('plantao_id',),
        'pronto_escala_justificativas': ('plantao_id', 'status'),
        'pronto_escala_eventos': ('acao', 'plantao_id', 'justificativa_id'),
    }.items():
        for coluna in colunas:
            op.create_index(
                f'ix_{tabela}_{coluna}', tabela, [coluna], schema=SCHEMA
            )


def downgrade() -> None:
    for tabela in (
        'pronto_escala_eventos',
        'pronto_escala_justificativas',
        'pronto_escala_registros',
        'pronto_escala_plantoes',
        'pronto_escala_parametros',
    ):
        op.drop_table(tabela, schema=SCHEMA)
