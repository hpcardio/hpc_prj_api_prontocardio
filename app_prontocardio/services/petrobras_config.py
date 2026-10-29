from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

CNPJ_LENGTH = 14
ANS_LENGTH = 6


class PetrobrasConfig(BaseSettings):
    model_config = SettingsConfigDict(env_prefix='PETROBRAS_', extra='ignore')
    enabled: bool = False
    transmission_enabled: bool = False
    login: SecretStr = SecretStr('')
    password: SecretStr = SecretStr('')
    prestador_cnpj: str = ''
    codigo_prestador: str = ''
    registro_ans: str = '422631'
    convenio_id: int = 37
    interval_minutes: int = Field(default=360, ge=15, le=10080)
    timeout_seconds: int = Field(default=30, ge=5, le=120)
    max_attachment_bytes: int = Field(default=5242880, ge=1024, le=10485760)
    # Mapping must be confirmed before automatic matching; no interchangeable
    # aliases.
    discovery_months: int = Field(default=3, ge=1, le=12)
    oracle_guia: str = 'pendente'

    @property
    def identificador_prestador(self):
        return self.codigo_prestador or self.prestador_cnpj

    def require_connection(self):
        if not self.enabled:
            raise ValueError('Integração Petrobras desabilitada.')
        if (
            not self.login.get_secret_value()
            or not self.password.get_secret_value()
        ):
            raise ValueError('Credenciais Petrobras não configuradas.')
        if (
            len(self.prestador_cnpj) != CNPJ_LENGTH
            or not self.prestador_cnpj.isdigit()
        ):
            raise ValueError('CNPJ do prestador não configurado.')
        if (
            len(self.registro_ans) != ANS_LENGTH
            or not self.registro_ans.isdigit()
        ):
            raise ValueError('Registro ANS inválido.')
