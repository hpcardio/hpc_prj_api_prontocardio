"""Separate Petrobras TISS import and appeal workspaces.

Additive only; no changes to Oracle or existing payer tables.
"""

import sqlalchemy as sa
from alembic import op

revision = '20261029_063'
down_revision = '20261027_062'
branch_labels = None
depends_on = None
SCHEMA = 'api_prontocardio'


def upgrade():
    op.create_table(
        'petrobras_protocolos',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('prestador', sa.String(14), nullable=False),
        sa.Column('numero', sa.String(12), nullable=False),
        sa.Column('cd_remessa', sa.Integer()),
        sa.Column('ativo', sa.Boolean(), nullable=False),
        sa.Column('ultima_sincronizacao', sa.DateTime(timezone=True)),
        sa.Column('proxima_sincronizacao', sa.DateTime(timezone=True)),
        sa.Column('erro', sa.String(200)),
        sa.UniqueConstraint(
            'prestador', 'numero', name='uq_petrobras_protocolo'
        ),
        schema=SCHEMA,
    )
    op.create_table(
        'petrobras_execucoes',
        sa.Column('competencia', sa.String(6)),
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('inicio', sa.DateTime(timezone=True), nullable=False),
        sa.Column('fim', sa.DateTime(timezone=True)),
        sa.Column('usuario_id', sa.Integer()),
        sa.Column('situacao', sa.String(30), nullable=False),
        sa.Column('quantidade', sa.Integer(), nullable=False),
        sa.Column('erro', sa.String(200)),
        schema=SCHEMA,
    )
    op.create_table(
        'petrobras_glosas',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('chave', sa.String(64), nullable=False, unique=True),
        sa.Column(
            'protocolo_id',
            sa.Integer(),
            sa.ForeignKey(f'{SCHEMA}.petrobras_protocolos.id'),
            nullable=False,
        ),
        sa.Column('demonstrativo', sa.String(20), nullable=False),
        sa.Column('guia_prestador', sa.String(20)),
        sa.Column('guia_operadora', sa.String(20)),
        sa.Column('tuss', sa.String(20)),
        sa.Column('fonte', sa.JSON(), nullable=False),
        sa.Column('valor_glosa', sa.Numeric(14, 2)),
        sa.Column('presente_na_fonte', sa.Boolean(), nullable=False),
        sa.Column('cruzamento', sa.String(30), nullable=False),
        sa.Column('oracle', sa.JSON()),
        sa.Column('divergencias', sa.JSON(), nullable=False),
        sa.Column('candidatos', sa.Integer(), nullable=False),
        sa.Column('erro_oracle', sa.String(200)),
        sa.Column('atualizado_em', sa.DateTime(timezone=True), nullable=False),
        sa.Column('justificativa', sa.Text(), nullable=False),
        sa.Column('versao', sa.Integer(), nullable=False),
        sa.Column('prazo', sa.Date()),
        sa.Column('origem_prazo', sa.String(500)),
        sa.Column('recurso_estado', sa.String(30), nullable=False),
        sa.Column('recurso_guia', sa.String(20)),
        sa.Column('recurso_protocolo', sa.String(12)),
        sa.Column('recurso_resultado', sa.JSON()),
        sa.Column('ultima_consulta_recurso', sa.DateTime(timezone=True)),
        schema=SCHEMA,
    )
    for column in [
        'protocolo_id',
        'guia_prestador',
        'tuss',
        'cruzamento',
        'recurso_estado',
    ]:
        op.create_index(
            'ix_petrobras_glosas_' + column,
            'petrobras_glosas',
            [column],
            schema=SCHEMA,
        )
    op.create_table(
        'petrobras_eventos',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column(
            'glosa_id',
            sa.Integer(),
            sa.ForeignKey(f'{SCHEMA}.petrobras_glosas.id'),
            nullable=False,
        ),
        sa.Column('criado_em', sa.DateTime(timezone=True), nullable=False),
        sa.Column('usuario_id', sa.Integer()),
        sa.Column('tipo', sa.String(40), nullable=False),
        sa.Column('dados', sa.JSON(), nullable=False),
        schema=SCHEMA,
    )
    op.create_index(
        'ix_petrobras_eventos_glosa_id',
        'petrobras_eventos',
        ['glosa_id'],
        schema=SCHEMA,
    )
    op.create_table(
        'petrobras_anexos',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column(
            'glosa_id',
            sa.Integer(),
            sa.ForeignKey(f'{SCHEMA}.petrobras_glosas.id'),
            nullable=False,
        ),
        sa.Column('nome', sa.String(200), nullable=False),
        sa.Column('mime', sa.String(100), nullable=False),
        sa.Column('conteudo', sa.LargeBinary(), nullable=False),
        sa.Column('sha256', sa.String(64), nullable=False),
        sa.Column('tipo_documento', sa.String(2), nullable=False),
        sa.Column('natureza_guia', sa.String(1), nullable=False),
        sa.Column('formato', sa.String(2), nullable=False),
        sa.Column('estado', sa.String(30), nullable=False),
        sa.Column('protocolo', sa.String(20)),
        sa.Column('criado_em', sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint('glosa_id', 'sha256', name='uq_petrobras_anexo'),
        schema=SCHEMA,
    )
    op.create_index(
        'ix_petrobras_anexos_glosa_id',
        'petrobras_anexos',
        ['glosa_id'],
        schema=SCHEMA,
    )


def downgrade():
    for name in [
        'petrobras_anexos',
        'petrobras_eventos',
        'petrobras_glosas',
        'petrobras_execucoes',
        'petrobras_protocolos',
    ]:
        op.drop_table(name, schema=SCHEMA)
