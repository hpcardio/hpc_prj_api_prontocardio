# ruff: noqa: E501
"""Contas a Pagar: registro de importações no MV e desfazer pelo Receita Certa.

DDL idempotente; o mesmo SQL está em docs/oracle/contas_pagar_importacoes_desfazer.sql.
"""

from alembic import op

revision = '20261118_071'
down_revision = '20261118_070'
branch_labels = None
depends_on = None

UPGRADE_SQL = """
CREATE TABLE IF NOT EXISTS api_prontocardio.contas_pagar_importacoes (
    id bigserial PRIMARY KEY,
    prestador_cnpj varchar(14) NOT NULL,
    numero_nfse varchar(30) NOT NULL,
    cd_multi_empresa integer,
    status varchar(20) NOT NULL,
    usuario_id integer NOT NULL,
    cd_usuario_mv varchar(30),
    assinatura varchar(64),
    cd_ent_serv bigint,
    cd_itent_serv bigint,
    cd_con_pag bigint,
    plano jsonb,
    erro text,
    iniciado_em timestamptz NOT NULL DEFAULT now(),
    concluido_em timestamptz
);
CREATE UNIQUE INDEX IF NOT EXISTS contas_pagar_importacoes_ativa
    ON api_prontocardio.contas_pagar_importacoes (prestador_cnpj, numero_nfse)
 WHERE status IN ('em_andamento', 'importada');
ALTER TABLE api_prontocardio.contas_pagar_importacoes
    ADD COLUMN IF NOT EXISTS desfeita_em timestamptz,
    ADD COLUMN IF NOT EXISTS desfeita_por integer,
    ADD COLUMN IF NOT EXISTS cd_usuario_mv_desfazer varchar(30),
    ADD COLUMN IF NOT EXISTS motivo_desfazer text,
    ADD COLUMN IF NOT EXISTS desfazer_detalhe jsonb;
"""

DOWNGRADE_SQL = """
ALTER TABLE api_prontocardio.contas_pagar_importacoes
    DROP COLUMN IF EXISTS desfazer_detalhe,
    DROP COLUMN IF EXISTS motivo_desfazer,
    DROP COLUMN IF EXISTS cd_usuario_mv_desfazer,
    DROP COLUMN IF EXISTS desfeita_por,
    DROP COLUMN IF EXISTS desfeita_em;
"""


def upgrade() -> None:
    op.execute(UPGRADE_SQL)


def downgrade() -> None:
    op.execute(DOWNGRADE_SQL)
