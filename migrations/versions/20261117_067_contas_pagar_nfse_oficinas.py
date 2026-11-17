# ruff: noqa: E501
"""Contas a Pagar: regras fornecedor->oficina, usuários por oficina e usuário MV.

O DDL é idempotente (IF NOT EXISTS) porque, em produção, ``alembic_version``
está em uma revisão (20261110_066) que não existe neste repositório e as
migrations não rodam no startup. O mesmo SQL está em
``docs/oracle/contas_pagar_nfse_oficinas.sql`` para aplicação manual.
"""

from alembic import op

revision = '20261117_067'
down_revision = '20261104_065'
branch_labels = None
depends_on = None

UPGRADE_SQL = """
CREATE TABLE IF NOT EXISTS api_prontocardio.contas_pagar_regras_oficina (
    cnpj VARCHAR(14) NOT NULL,
    cd_multi_empresa INTEGER NOT NULL,
    cd_fornecedor BIGINT,
    nm_fornecedor VARCHAR(200),
    cd_oficina INTEGER,
    ds_oficina VARCHAR(120),
    status VARCHAR(20) NOT NULL
        CHECK (status IN ('definida', 'a_classificar')),
    origem VARCHAR(20) NOT NULL
        CHECK (origem IN ('historico', 'manual')),
    qtd_entradas INTEGER NOT NULL DEFAULT 0,
    participacao NUMERIC(5, 4),
    motivo TEXT,
    usuario_id INTEGER REFERENCES api_prontocardio.usuarios_api (id)
        ON DELETE SET NULL,
    atualizado_em TIMESTAMP NOT NULL
        DEFAULT timezone('America/Sao_Paulo', now()),
    CHECK (status <> 'definida' OR cd_oficina IS NOT NULL),
    PRIMARY KEY (cnpj, cd_multi_empresa)
);
CREATE INDEX IF NOT EXISTS ix_cp_regras_oficina_status
    ON api_prontocardio.contas_pagar_regras_oficina (status, cd_oficina);

CREATE TABLE IF NOT EXISTS api_prontocardio.contas_pagar_usuarios_oficina (
    usuario_id INTEGER NOT NULL
        REFERENCES api_prontocardio.usuarios_api (id) ON DELETE CASCADE,
    cd_oficina INTEGER NOT NULL,
    ds_oficina VARCHAR(120),
    concedido_por INTEGER REFERENCES api_prontocardio.usuarios_api (id)
        ON DELETE SET NULL,
    criado_em TIMESTAMP NOT NULL
        DEFAULT timezone('America/Sao_Paulo', now()),
    PRIMARY KEY (usuario_id, cd_oficina)
);

CREATE TABLE IF NOT EXISTS api_prontocardio.usuarios_mv (
    usuario_id INTEGER PRIMARY KEY
        REFERENCES api_prontocardio.usuarios_api (id) ON DELETE CASCADE,
    cd_usuario_mv VARCHAR(30) NOT NULL UNIQUE,
    nm_usuario_mv VARCHAR(200),
    vinculado_por INTEGER REFERENCES api_prontocardio.usuarios_api (id)
        ON DELETE SET NULL,
    vinculado_em TIMESTAMP NOT NULL
        DEFAULT timezone('America/Sao_Paulo', now())
);
"""

DOWNGRADE_SQL = """
DROP TABLE IF EXISTS api_prontocardio.usuarios_mv;
DROP TABLE IF EXISTS api_prontocardio.contas_pagar_usuarios_oficina;
DROP TABLE IF EXISTS api_prontocardio.contas_pagar_regras_oficina;
"""


def upgrade():
    op.execute(UPGRADE_SQL)


def downgrade():
    op.execute(DOWNGRADE_SQL)
