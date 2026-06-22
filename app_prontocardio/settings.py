from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file='.env', env_file_encoding='utf-8', extra='ignore'
    )

    ORACLE_DATABASE_URL: str
    DATABASE_URL: str | None = None
    POSTGRES_SCHEMA: str
    RUN_MIGRATIONS_ON_STARTUP: bool = True
    SECRET_KEY: str
    ALGORITHM: str
    FRONTEND_BASE_URL: str = 'http://localhost:8003'
    SMTP_HOST: str | None = None
    SMTP_PORT: int = 587
    SMTP_USERNAME: str | None = None
    SMTP_PASSWORD: str | None = None
    SMTP_FROM_EMAIL: str = 'nao-responda@prontocardio.com.br'
    SMTP_USE_TLS: bool = True
