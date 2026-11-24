-- Contas a Receber: acompanhamento remessa -> protocolo -> NFS -> recebimento.
-- Postgres (schema api_prontocardio). DDL idempotente; o mesmo SQL está na
-- migration 20261124_076_contas_receber_fluxo.

CREATE TABLE IF NOT EXISTS api_prontocardio.cr_protocolos (
        id bigserial PRIMARY KEY,
        cd_convenio integer NOT NULL,
        numero varchar(60) NOT NULL,
        origem varchar(20) NOT NULL,
        competencia date,
        valor_cobrado numeric(15,2),
        valor_liberado numeric(15,2),
        data_protocolo date,
        solicitacao_nota_em date,
        solicitacao_nota_obs varchar(400),
        solicitacao_nota_por integer,
        criado_por integer,
        criado_em timestamptz NOT NULL DEFAULT now(),
        atualizado_por integer,
        atualizado_em timestamptz,
        CONSTRAINT cr_protocolos_numero UNIQUE (cd_convenio, numero)
    );
-- Uma remessa tem um protocolo; um protocolo pode reunir várias remessas.
CREATE TABLE IF NOT EXISTS api_prontocardio.cr_protocolo_remessas (
        cd_remessa bigint PRIMARY KEY,
        protocolo_id bigint NOT NULL REFERENCES api_prontocardio.cr_protocolos (id) ON DELETE CASCADE,
        origem varchar(20) NOT NULL,
        criado_por integer,
        criado_em timestamptz NOT NULL DEFAULT now()
    );
CREATE INDEX IF NOT EXISTS cr_protocolo_remessas_protocolo
        ON api_prontocardio.cr_protocolo_remessas (protocolo_id);
-- Um protocolo tem uma NFS; uma NFS pode cobrir vários protocolos.
CREATE TABLE IF NOT EXISTS api_prontocardio.cr_protocolo_nfse (
        protocolo_id bigint PRIMARY KEY REFERENCES api_prontocardio.cr_protocolos (id) ON DELETE CASCADE,
        prestador_cnpj varchar(14),
        numero_nfse varchar(20) NOT NULL,
        data_emissao date,
        valor numeric(15,2),
        row_hash varchar(64),
        origem varchar(20) NOT NULL,
        criado_por integer,
        criado_em timestamptz NOT NULL DEFAULT now(),
        atualizado_por integer,
        atualizado_em timestamptz
    );
CREATE INDEX IF NOT EXISTS cr_protocolo_nfse_numero
        ON api_prontocardio.cr_protocolo_nfse (numero_nfse, prestador_cnpj);
-- Recebimento financeiro (lançamento de extrato) e as NFS que ele quita.
CREATE TABLE IF NOT EXISTS api_prontocardio.cr_recebimentos (
        id bigserial PRIMARY KEY,
        extrato_linha_id bigint REFERENCES api_prontocardio.contas_pagar_extrato_linhas (id) ON DELETE RESTRICT,
        data date NOT NULL,
        valor numeric(15,2) NOT NULL,
        cd_con_cor integer,
        historico varchar(300),
        origem varchar(20) NOT NULL,
        criado_por integer,
        criado_em timestamptz NOT NULL DEFAULT now()
    );
CREATE TABLE IF NOT EXISTS api_prontocardio.cr_recebimento_nfse (
        recebimento_id bigint NOT NULL REFERENCES api_prontocardio.cr_recebimentos (id) ON DELETE CASCADE,
        numero_nfse varchar(20) NOT NULL,
        prestador_cnpj varchar(14) NOT NULL DEFAULT '',
        valor_alocado numeric(15,2),
        PRIMARY KEY (recebimento_id, numero_nfse, prestador_cnpj)
    );
CREATE INDEX IF NOT EXISTS cr_recebimento_nfse_numero
        ON api_prontocardio.cr_recebimento_nfse (numero_nfse, prestador_cnpj);
-- Auditoria de cada vínculo (criar, editar, remover).
CREATE TABLE IF NOT EXISTS api_prontocardio.cr_eventos (
        id bigserial PRIMARY KEY,
        entidade varchar(30) NOT NULL,
        chave varchar(60) NOT NULL,
        acao varchar(20) NOT NULL,
        dados_anteriores jsonb,
        dados_novos jsonb,
        usuario_id integer,
        usuario_nome varchar(200),
        criado_em timestamptz NOT NULL DEFAULT now()
    );
CREATE INDEX IF NOT EXISTS cr_eventos_chave
        ON api_prontocardio.cr_eventos (entidade, chave);
