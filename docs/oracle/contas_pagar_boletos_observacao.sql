-- Observação do setor ao enviar a NFS-e ao financeiro (do que se trata a nota).
-- Idempotente. Aplicar no PostgreSQL (schema api_prontocardio) antes do deploy.
ALTER TABLE api_prontocardio.contas_pagar_boletos
    ADD COLUMN IF NOT EXISTS observacao TEXT;
