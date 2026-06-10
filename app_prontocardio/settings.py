from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file='.env', env_file_encoding='utf-8', extra='ignore'
    )

    ORACLE_DATABASE_URL: str
    DATABASE_URL: str | None = None
    POSTGRES_SCHEMA: str
    SECRET_KEY: str
    ALGORITHM: str
