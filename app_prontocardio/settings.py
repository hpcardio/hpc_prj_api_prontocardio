from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file='.env', env_file_encoding='utf-8', extra='ignore'
    )

    ORACLE_DATABASE_URL: str = Field(
        validation_alias=AliasChoices('ORACLE_DATABASE_URL', 'DATABASE_URL')
    )
    POSTGRES_DATABASE_URL: str | None = None
    POSTGRES_SCHEMA: str = 'api_prontocardio'
