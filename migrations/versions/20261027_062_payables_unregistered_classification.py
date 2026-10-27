"""Audit criticality classification for payees without an MV supplier code."""

import sqlalchemy as sa
from alembic import op

revision = '20261027_062'
down_revision = '20261026_061'
branch_labels = None
depends_on = None
SCHEMA = 'api_prontocardio'
TABLE = 'contas_pagar_classificacoes_sem_cadastro'


def upgrade():
    op.create_table(
        TABLE,
        sa.Column('id', sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column('identificador', sa.String(2000), nullable=False),
        sa.Column('nome_fornecedor', sa.Text(), nullable=False),
        sa.Column('nivel_criticidade', sa.SmallInteger(), nullable=False),
        sa.Column('justificativa', sa.Text(), nullable=False),
        sa.Column('usuario_id', sa.Integer(), nullable=False),
        sa.Column(
            'data_classificacao',
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("timezone('America/Sao_Paulo', now())"),
        ),
        sa.ForeignKeyConstraint(
            ['usuario_id'], [f'{SCHEMA}.usuarios_api.id'], ondelete='RESTRICT'
        ),
        sa.CheckConstraint(
            'nivel_criticidade BETWEEN 1 AND 5',
            name='ck_cp_sem_cadastro_nivel',
        ),
        sa.CheckConstraint(
            'length(btrim(justificativa)) BETWEEN 1 AND 2000',
            name='ck_cp_sem_cadastro_justificativa',
        ),
        sa.CheckConstraint(
            "identificador LIKE 'sem-cadastro-%'",
            name='ck_cp_sem_cadastro_identificador',
        ),
        schema=SCHEMA,
    )
    op.create_index(
        'ix_cp_sem_cadastro_ultima',
        TABLE,
        ['identificador', 'id'],
        schema=SCHEMA,
    )


def downgrade():
    op.drop_table(TABLE, schema=SCHEMA)
