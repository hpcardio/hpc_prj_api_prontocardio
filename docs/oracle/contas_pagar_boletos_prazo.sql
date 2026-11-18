-- Contas a Pagar: regra das 72h (3 dias úteis) entre o envio do boleto e o vencimento.
-- PostgreSQL do Receita Certa. Idempotente. Mesmo SQL da migration 20261118_070.
ALTER TABLE api_prontocardio.contas_pagar_boletos
    ADD COLUMN IF NOT EXISTS prazo_minimo DATE,
    ADD COLUMN IF NOT EXISTS fora_do_prazo BOOLEAN NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS justificativa_atraso TEXT;
COMMENT ON COLUMN api_prontocardio.contas_pagar_boletos.prazo_minimo IS
    'Primeiro vencimento aceito sem justificativa: envio + 72h contadas só em dias úteis';
COMMENT ON COLUMN api_prontocardio.contas_pagar_boletos.justificativa_atraso IS
    'Motivo informado pelo setor quando o boleto vence antes do prazo mínimo';
