"""Adiciona login nominal e troca obrigatória no primeiro acesso."""

from alembic import op
import sqlalchemy as sa


revision = '20261117_066'
down_revision = '20261104_065'
branch_labels = None
depends_on = None


SCHEMA = 'api_prontocardio'


def upgrade() -> None:
    op.add_column(
        'usuarios_api',
        sa.Column('nome_usuario', sa.String(length=80), nullable=True),
        schema=SCHEMA,
    )
    op.add_column(
        'usuarios_api',
        sa.Column(
            'troca_senha_obrigatoria',
            sa.Boolean(),
            nullable=False,
            server_default=sa.text('false'),
        ),
        schema=SCHEMA,
    )
    op.execute(
        sa.text(
            f'''WITH nomes AS (
                SELECT id,
                       lower(split_part(email, '@', 1)) AS base,
                       row_number() OVER (
                           PARTITION BY lower(split_part(email, '@', 1))
                           ORDER BY id
                       ) AS ordem
                  FROM {SCHEMA}.usuarios_api
            )
            UPDATE {SCHEMA}.usuarios_api AS u
               SET nome_usuario = CASE
                   WHEN nomes.ordem = 1 THEN nomes.base
                   ELSE nomes.base || '_' || u.id::text
               END
              FROM nomes
             WHERE nomes.id = u.id'''
        )
    )
    op.alter_column(
        'usuarios_api', 'nome_usuario', nullable=False, schema=SCHEMA
    )
    op.create_unique_constraint(
        'uq_usuarios_api_nome_usuario',
        'usuarios_api',
        ['nome_usuario'],
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_constraint(
        'uq_usuarios_api_nome_usuario',
        'usuarios_api',
        schema=SCHEMA,
        type_='unique',
    )
    op.drop_column('usuarios_api', 'troca_senha_obrigatoria', schema=SCHEMA)
    op.drop_column('usuarios_api', 'nome_usuario', schema=SCHEMA)
