-- Contas a Pagar (fase 5): extratos bancários enviados pelo financeiro.
-- PostgreSQL do Receita Certa. Idempotente. Mesmo SQL da migration 20261123_074.
CREATE TABLE IF NOT EXISTS api_prontocardio.contas_pagar_extratos (
        id bigserial PRIMARY KEY,
        sha256 char(64) NOT NULL UNIQUE,
        nome_arquivo varchar(255) NOT NULL,
        arquivo bytea NOT NULL,
        banco integer NOT NULL,
        agencia varchar(10) NOT NULL,
        conta varchar(20) NOT NULL,
        conta_dv varchar(2),
        cd_con_cor integer,
        cd_multi_empresa integer,
        periodo_inicio date,
        periodo_fim date,
        saldo_inicial numeric(15,2),
        saldo_final numeric(15,2),
        conferido boolean NOT NULL,
        avisos text,
        qtd_linhas integer NOT NULL,
        enviado_por integer NOT NULL,
        enviado_por_nome varchar(200),
        criado_em timestamptz NOT NULL DEFAULT now()
    );
CREATE INDEX IF NOT EXISTS contas_pagar_extratos_conta
        ON api_prontocardio.contas_pagar_extratos (cd_con_cor, periodo_fim);
CREATE TABLE IF NOT EXISTS api_prontocardio.contas_pagar_extrato_linhas (
        id bigserial PRIMARY KEY,
        extrato_id bigint NOT NULL REFERENCES api_prontocardio.contas_pagar_extratos (id) ON DELETE CASCADE,
        ordem integer NOT NULL,
        data date NOT NULL,
        historico varchar(300) NOT NULL,
        documento varchar(60),
        valor numeric(15,2) NOT NULL,
        chave char(64) NOT NULL,
        repetida boolean NOT NULL DEFAULT false
    );
CREATE INDEX IF NOT EXISTS contas_pagar_extrato_linhas_extrato
        ON api_prontocardio.contas_pagar_extrato_linhas (extrato_id, ordem);
CREATE INDEX IF NOT EXISTS contas_pagar_extrato_linhas_chave
        ON api_prontocardio.contas_pagar_extrato_linhas (chave);
