# ruff: noqa: E501
"""Contas a Pagar: NFS-e de outros municípios cadastradas pelo PDF (DANFSe).

DDL idempotente; o mesmo SQL está em docs/oracle/contas_pagar_nfse_manuais.sql.
"""

from alembic import op

revision = '20261119_072'
down_revision = '20261118_071'
branch_labels = None
depends_on = None

UPGRADE_SQL = """
CREATE TABLE IF NOT EXISTS api_prontocardio.contas_pagar_nfse_manuais (
        id bigserial PRIMARY KEY,
        status varchar(12) NOT NULL CHECK (status IN ('rascunho', 'ativa', 'excluida')),
        origem_leitura varchar(12) NOT NULL CHECK (origem_leitura IN ('danfse', 'manual')),
        chave_acesso varchar(60),
        prestador_municipio varchar(120),
        prestador_codigo_municipio varchar(10),
        numero_nfse text, codigo_verificacao_nfse text, competencia_nfse text, codigo_natureza_operacao text, prestador_cnpj text, prestador_razao_social text, prestador_nome_fantasia text, prestador_inscricao_municipal text, prestador_endereco text, prestador_numero_endereco text, prestador_complemento_endereco text, prestador_bairro text, prestador_cep text, prestador_uf text, prestador_email text, prestador_telefone text, tomador_cnpj text, tomador_razao_social text, tomador_endereco text, tomador_numero_endereco text, tomador_bairro text, tomador_cep text, tomador_uf text, tomador_email text, discriminacao text, codigo_item_lista_servico text, codigo_cnae text, codigo_tributacao text, codigo_municipio_servico text, valor_servicos text, valor_deducoes text, valor_pis text, valor_cofins text, valor_inss text, valor_ir text, valor_csll text, outras_retencoes text, iss_retido text, valor_iss text, valor_iss_retido text, base_calculo text, aliquota text, desconto_incondicionado text, desconto_condicionado text, valor_liquido_nfse text,
        data_hora timestamptz,
        leitura jsonb,
        nome_arquivo varchar(255) NOT NULL,
        arquivo bytea NOT NULL,
        sha256 char(64) NOT NULL,
        criado_por integer NOT NULL,
        criado_em timestamptz NOT NULL DEFAULT now(),
        confirmado_por integer,
        confirmado_em timestamptz,
        excluido_por integer,
        excluido_em timestamptz,
        motivo_exclusao text
    );
CREATE UNIQUE INDEX IF NOT EXISTS contas_pagar_nfse_manuais_ativa
        ON api_prontocardio.contas_pagar_nfse_manuais (prestador_cnpj, numero_nfse) WHERE status = 'ativa';
"""

DOWNGRADE_SQL = """
DROP TABLE IF EXISTS api_prontocardio.contas_pagar_nfse_manuais;
"""


def upgrade() -> None:
    op.execute(UPGRADE_SQL)


def downgrade() -> None:
    op.execute(DOWNGRADE_SQL)
