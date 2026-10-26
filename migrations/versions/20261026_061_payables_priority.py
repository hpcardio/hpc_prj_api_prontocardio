"""Five criticality levels and monthly payable budgets (additive migration)."""

import sqlalchemy as sa
from alembic import op

revision = '20261026_061'
down_revision = '20261015_060'
branch_labels = None
depends_on = None
SCHEMA = 'api_prontocardio'


def upgrade():
    # Existing critical suppliers deliberately remain NULL for manual review.
    for column in (
        sa.Column('nivel_criticidade', sa.SmallInteger(), nullable=True),
        sa.Column('justificativa_criticidade', sa.Text(), nullable=True),
        sa.Column('usuario_criticidade_id', sa.Integer(), nullable=True),
        sa.Column('data_classificacao', sa.DateTime(), nullable=True),
    ):
        op.add_column('contas_pagar_tratamentos', column, schema=SCHEMA)
    op.create_check_constraint(
        'ck_contas_pagar_nivel',
        'contas_pagar_tratamentos',
        'nivel_criticidade BETWEEN 1 AND 5',
        schema=SCHEMA,
    )
    op.create_table(
        'contas_pagar_orcamentos',
        sa.Column('competencia', sa.Date(), primary_key=True),
        sa.Column('valor', sa.Numeric(15, 2), nullable=False),
        sa.Column('usuario_id', sa.Integer(), nullable=False),
        sa.Column(
            'data_atualizacao',
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("timezone('America/Sao_Paulo', now())"),
        ),
        sa.CheckConstraint(
            'valor >= 0', name='ck_contas_pagar_orcamento_valor'
        ),
        sa.CheckConstraint(
            'EXTRACT(DAY FROM competencia) = 1',
            name='ck_contas_pagar_competencia',
        ),
        schema=SCHEMA,
    )


def downgrade():
    op.drop_table('contas_pagar_orcamentos', schema=SCHEMA)
    op.drop_constraint(
        'ck_contas_pagar_nivel', 'contas_pagar_tratamentos', schema=SCHEMA
    )
    for name in (
        'data_classificacao',
        'usuario_criticidade_id',
        'justificativa_criticidade',
        'nivel_criticidade',
    ):
        op.drop_column('contas_pagar_tratamentos', name, schema=SCHEMA)
