from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

SMTP_SSL_PORT = 465
AE_TITLE_MAX_LENGTH = 16
INTEGRATION_TOKEN_MIN_LENGTH = 32


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file='.env', env_file_encoding='utf-8', extra='ignore'
    )

    ORACLE_DATABASE_URL: str
    ORACLE_READONLY_DATABASE_URL: str | None = None
    ORACLE_READONLY_POOL_SIZE: int = 4
    ORACLE_READONLY_MAX_OVERFLOW: int = 0
    ORACLE_THICK_MODE: bool = True
    ORACLE_CLIENT_LIB_DIR: str | None = None
    DATABASE_URL: str | None = None
    POSTGRES_SCHEMA: str
    RUN_MIGRATIONS_ON_STARTUP: bool = True
    APP_ENV: str = 'development'
    EXPECTED_POSTGRES_HOST: str | None = None
    EXPECTED_POSTGRES_DATABASE: str | None = None
    SECRET_KEY: str
    ALGORITHM: str
    FRONTEND_BASE_URL: str = 'http://localhost:8080'
    FRONTEND_PASSWORD_RESET_URL: str | None = None
    CORS_ALLOWED_ORIGINS: str = ''
    SMTP_HOST: str | None = None
    SMTP_PORT: int = 587
    SMTP_USERNAME: str | None = None
    SMTP_USER: str | None = None
    SMTP_PASSWORD: str | None = None
    SMTP_FROM_EMAIL: str = 'nao-responda@prontocardio.com.br'
    SMTP_FROM: str | None = None
    SMTP_USE_TLS: bool = True
    SMTP_USE_SSL: bool | None = None
    AIRFLOW_NFSE_BASE_URL: str | None = None
    AIRFLOW_NFSE_DAG_ID: str = 'emissao_nfse'
    AIRFLOW_NFSE_DAG_RUNS_PATH: str = '/api/v1/dags/{dag_id}/dagRuns'
    AIRFLOW_NFSE_TOKEN: str | None = None
    AIRFLOW_NFSE_USERNAME: str | None = None
    AIRFLOW_NFSE_PASSWORD: str | None = None
    AIRFLOW_NFSE_TIMEOUT_SECONDS: float = 15.0
    AIRFLOW_NFSE_VERIFY_SSL: bool = True
    WHATSAPP_WEBHOOK_VERIFY_TOKEN: str = 'meuprontocardio_whatsapp_2026'
    WHATSAPP_GRAPH_API_VERSION: str = 'v25.0'
    WHATSAPP_PHONE_NUMBER_ID: str | None = None
    WHATSAPP_ACCESS_TOKEN: str | None = None
    WHATSAPP_AUTO_REPLY_TEXT: str | None = None
    WHATSAPP_COMPROVANTE_ENABLED: bool = False
    WHATSAPP_COMPROVANTE_TEMPLATE: str = 'confirmacao_agendamento_rede'
    WHATSAPP_COMPROVANTE_MAX_BYTES: int = 5 * 1024 * 1024
    WHATSAPP_TEMPLATE_PATIENT_OTP: str = 'meu_prontocardio_codigo_acesso'
    WHATSAPP_TEMPLATE_PATIENT_OTP_LANGUAGE: str = 'pt_BR'
    PATIENT_OTP_TTL_SECONDS: int = 300
    PATIENT_OTP_RESEND_SECONDS: int = 60
    PATIENT_OTP_MAX_ATTEMPTS: int = 5
    PATIENT_OTP_MAX_REQUESTS_15_MIN: int = 5
    PATIENT_SESSION_TTL_SECONDS: int = 3600
    PATIENT_AUTH_LOCAL_TEST_MODE: bool = False
    PATIENT_AUTH_LOCAL_MEMORY_STORE: bool = False
    PATIENT_AUTH_LOCAL_SEND_WHATSAPP: bool = False
    PATIENT_AUTH_TEST_CODE: str | None = None
    AGENDAMENTO_SENHA_INTERNA: str = ''
    AGENDAMENTO_REGISTRAR_ORIGEM: bool = False
    AGENDAMENTO_HISTORICO_INTERNO: bool = False
    AGENDAMENTO_PERFIS_PERMITIDOS: str = ''
    AGENDAMENTO_TELAS_PERMITIDAS: str = ''
    COOKIE_SECURE: bool = False
    EVOLUCAO_MV_WRITE_ENABLED: bool = False
    EVOLUCAO_MV_ALLOWED_ATENDIMENTOS: str = '339070'
    EVOLUCAO_MV_USUARIO: str = 'DBAMV'
    EVOLUCAO_MV_CD_TIPO_DOCUMENTO: int = 36
    EVOLUCAO_MV_CD_OBJETO: int = 511
    EVOLUCAO_MV_HORA_VALIDADE: int = 14
    ECG_MWL_HOST: str = Field(default='192.168.4.31', min_length=1)
    ECG_MWL_PORT: int = Field(default=1010, ge=1, le=65535)
    ECG_MWL_CALLED_AE: str = 'SERVER_WL'
    ECG_MWL_CALLING_AE: str = 'HPC_ECG_GW'
    ECG_MWL_ATTENDANCE_TAG: str = '0040,1001'
    ECG_MWL_CONNECT_TIMEOUT_SECONDS: float = Field(
        default=5.0, ge=0.1, le=30.0
    )
    ECG_MWL_RESPONSE_TIMEOUT_SECONDS: float = Field(
        default=10.0, ge=0.1, le=30.0
    )
    ECG_MWL_MAX_RESULTS: int = Field(default=10, ge=1, le=10)
    ECG_MWL_MAX_CONCURRENCY: int = Field(default=2, ge=1, le=4)
    ECG_MV_PROCEDURE_CODE: int = Field(default=236, gt=0)
    ECG_WORKLIST_INTEGRATION_TOKEN: SecretStr | None = None

    @field_validator('ECG_WORKLIST_INTEGRATION_TOKEN', mode='before')
    @classmethod
    def validar_token_integracao_ecg(cls, valor):
        if valor is None or (isinstance(valor, str) and not valor.strip()):
            return None
        texto = (
            valor.get_secret_value()
            if isinstance(valor, SecretStr)
            else str(valor)
        )
        if len(texto) < INTEGRATION_TOKEN_MIN_LENGTH:
            raise ValueError(
                'Token de integração ECG deve ter no mínimo 32 caracteres.'
            )
        return valor

    @field_validator('ECG_MWL_CALLED_AE', 'ECG_MWL_CALLING_AE')
    @classmethod
    def validar_ae_title(cls, valor: str) -> str:
        valor = valor.strip()
        if (
            not 1 <= len(valor) <= AE_TITLE_MAX_LENGTH
            or not valor.isascii()
            or not valor.isprintable()
            or '\\' in valor
        ):
            raise ValueError('AE Title deve ter 1 a 16 caracteres ASCII.')
        return valor

    @field_validator('ECG_MWL_ATTENDANCE_TAG')
    @classmethod
    def validar_tag_atendimento_mwl(cls, valor: str) -> str:
        if valor not in {'0040,1001', '0040,0009'}:
            raise ValueError('Tag de atendimento MWL não permitida.')
        return valor

    @model_validator(mode='after')
    def validar_banco_de_producao(self):
        if self.APP_ENV.strip().casefold() not in {'prod', 'production'}:
            return self

        if not self.DATABASE_URL:
            raise ValueError('DATABASE_URL é obrigatória em produção.')
        if not self.EXPECTED_POSTGRES_HOST:
            raise ValueError(
                'EXPECTED_POSTGRES_HOST é obrigatório em produção.'
            )
        if not self.EXPECTED_POSTGRES_DATABASE:
            raise ValueError(
                'EXPECTED_POSTGRES_DATABASE é obrigatório em produção.'
            )

        destino = urlsplit(self.DATABASE_URL)
        host_atual = (destino.hostname or '').strip().casefold()
        host_esperado = self.EXPECTED_POSTGRES_HOST.strip().casefold()
        banco_atual = destino.path.lstrip('/').strip().casefold()
        banco_esperado = self.EXPECTED_POSTGRES_DATABASE.strip().casefold()
        if host_atual != host_esperado or banco_atual != banco_esperado:
            raise ValueError(
                'DATABASE_URL não aponta para o PostgreSQL oficial: '
                f'esperado {host_esperado}/{banco_esperado}, '
                f'recebido {host_atual}/{banco_atual}.'
            )

        return self

    @property
    def smtp_username(self) -> str | None:
        return self.SMTP_USERNAME or self.SMTP_USER

    @property
    def smtp_from_email(self) -> str:
        return self.SMTP_FROM or self.SMTP_FROM_EMAIL

    @property
    def smtp_use_ssl(self) -> bool:
        if self.SMTP_USE_SSL is not None:
            return self.SMTP_USE_SSL
        return self.SMTP_PORT == SMTP_SSL_PORT

    @property
    def smtp_use_tls(self) -> bool:
        return False if self.smtp_use_ssl else self.SMTP_USE_TLS

    @property
    def frontend_password_reset_url(self) -> str:
        if self.FRONTEND_PASSWORD_RESET_URL:
            url = self.FRONTEND_PASSWORD_RESET_URL.strip()
            if url:
                return url

        return (
            f'{self.FRONTEND_BASE_URL.rstrip("/")}'
            '/autenticacao/redefinir-senha'
        )

    @property
    def cors_allowed_origins(self) -> list[str]:
        origins = [
            origin.strip().rstrip('/')
            for origin in self.CORS_ALLOWED_ORIGINS.split(',')
            if origin.strip()
        ]
        if origins:
            return origins

        return [self.FRONTEND_BASE_URL.rstrip('/')]

    @staticmethod
    def _lista_configurada(valor: str) -> set[str]:
        return {
            item.strip().casefold()
            for item in valor.split(',')
            if item.strip()
        }

    @property
    def agendamento_perfis_permitidos(self) -> set[str]:
        return self._lista_configurada(self.AGENDAMENTO_PERFIS_PERMITIDOS)

    @property
    def agendamento_telas_permitidas(self) -> set[str]:
        return self._lista_configurada(self.AGENDAMENTO_TELAS_PERMITIDAS)

    @property
    def evolucao_mv_atendimentos_permitidos(self) -> set[int]:
        return {
            int(codigo.strip())
            for codigo in self.EVOLUCAO_MV_ALLOWED_ATENDIMENTOS.split(',')
            if codigo.strip()
        }
