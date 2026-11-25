-- Login do Receita Certa pelo CPF (além de email e nome de usuário).
-- Idempotente. Aplicar no PostgreSQL (schema api_prontocardio) antes do deploy.
ALTER TABLE api_prontocardio.usuarios_api
    ADD COLUMN IF NOT EXISTS cpf VARCHAR(11);

CREATE UNIQUE INDEX IF NOT EXISTS usuarios_api_cpf_uk
    ON api_prontocardio.usuarios_api (cpf)
 WHERE cpf IS NOT NULL;
