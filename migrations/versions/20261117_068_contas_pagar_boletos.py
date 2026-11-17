# ruff: noqa: E501
"""Contas a Pagar: boletos (duplicatas) das NFS-e recebidas.

DDL idempotente; o mesmo SQL está em docs/oracle/contas_pagar_boletos.sql
para aplicação manual (produção não roda migrations no startup).
"""

from alembic import op

revision = '20261117_068'
down_revision = '20261117_067'
branch_labels = None
depends_on = None

UPGRADE_SQL = """
CREATE TABLE IF NOT EXISTS api_prontocardio.contas_pagar_boletos (
    id BIGSERIAL PRIMARY KEY,
    prestador_cnpj VARCHAR(14) NOT NULL,
    numero_nfse VARCHAR(30) NOT NULL,
    cd_multi_empresa INTEGER,
    nome_arquivo VARCHAR(255) NOT NULL,
    arquivo BYTEA NOT NULL,
    sha256 CHAR(64) NOT NULL,
    tipo VARCHAR(15) NOT NULL CHECK (tipo IN ('bancario', 'arrecadacao')),
    linha_digitavel VARCHAR(48) NOT NULL,
    codigo_barras VARCHAR(44) NOT NULL,
    banco VARCHAR(3),
    valor NUMERIC(15, 2) NOT NULL CHECK (valor > 0),
    vencimento DATE NOT NULL,
    valor_impresso NUMERIC(15, 2),
    nosso_numero VARCHAR(40),
    cnpjs_texto TEXT,
    origem_leitura VARCHAR(10) NOT NULL CHECK (origem_leitura IN ('pdf', 'manual')),
    divergencias TEXT NOT NULL DEFAULT '[]',
    enviado_por INTEGER REFERENCES api_prontocardio.usuarios_api (id) ON DELETE SET NULL,
    enviado_em TIMESTAMP NOT NULL DEFAULT timezone('America/Sao_Paulo', now()),
    UNIQUE (prestador_cnpj, numero_nfse, linha_digitavel),
    UNIQUE (prestador_cnpj, numero_nfse, sha256)
);
CREATE INDEX IF NOT EXISTS ix_cp_boletos_nota
    ON api_prontocardio.contas_pagar_boletos (prestador_cnpj, numero_nfse);
"""

DOWNGRADE_SQL = 'DROP TABLE IF EXISTS api_prontocardio.contas_pagar_boletos;'


def upgrade():
    op.execute(UPGRADE_SQL)


def downgrade():
    op.execute(DOWNGRADE_SQL)
