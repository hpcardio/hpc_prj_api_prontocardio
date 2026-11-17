-- Vínculo usuário Receita Certa x login MV: origem e proxy Oracle. PostgreSQL do Receita Certa.
-- Idempotente. Mesmo SQL da migration 20261117_069.
ALTER TABLE api_prontocardio.usuarios_mv
    ADD COLUMN IF NOT EXISTS origem VARCHAR(10) NOT NULL DEFAULT 'manual',
    ADD COLUMN IF NOT EXISTS validado_cas_em TIMESTAMP,
    ADD COLUMN IF NOT EXISTS proxy_concedido_em TIMESTAMP;
