-- Contas a Pagar: registro de importações no MV (criado pela API) + desfazer pelo Receita Certa.
-- PostgreSQL do Receita Certa. Idempotente. Mesmo SQL da migration 20261118_071.
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
