-- Contas a Receber: percentuais de retenção por convênio (tela "Configuração
-- por Convênio"). Postgres (schema api_prontocardio). DDL idempotente; o mesmo
-- SQL está na migration 20261125_078_contas_receber_retencoes_convenio.

CREATE TABLE IF NOT EXISTS api_prontocardio.cr_convenio_retencoes (
        cd_convenio integer PRIMARY KEY,
        convenio varchar(120) NOT NULL,
        iss numeric(6,3) NOT NULL DEFAULT 0,
        ir numeric(6,3) NOT NULL DEFAULT 0,
        pis numeric(6,3) NOT NULL DEFAULT 0,
        cofins numeric(6,3) NOT NULL DEFAULT 0,
        csll numeric(6,3) NOT NULL DEFAULT 0,
        observacao varchar(300),
        atualizado_por integer,
        atualizado_por_nome varchar(200),
        atualizado_em timestamptz NOT NULL DEFAULT now(),
        CONSTRAINT cr_convenio_retencoes_faixa CHECK (
            iss BETWEEN 0 AND 100 AND ir BETWEEN 0 AND 100 AND pis BETWEEN 0 AND 100
            AND cofins BETWEEN 0 AND 100 AND csll BETWEEN 0 AND 100
        )
    );
