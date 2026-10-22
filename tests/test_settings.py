import pytest
from pydantic import ValidationError

from app_prontocardio.settings import Settings


def _settings_producao(**overrides):
    valores = {
        'ORACLE_DATABASE_URL': 'oracle+oracledb://teste:teste@oracle/teste',
        'DATABASE_URL': (
            'postgresql+psycopg://usuario:senha@db.oficial.local:5432/'
            'prontocardio_db'
        ),
        'POSTGRES_SCHEMA': 'api_prontocardio',
        'SECRET_KEY': 'segredo-de-teste',
        'ALGORITHM': 'HS256',
        'APP_ENV': 'production',
        'EXPECTED_POSTGRES_HOST': 'db.oficial.local',
        'EXPECTED_POSTGRES_DATABASE': 'prontocardio_db',
    }
    valores.update(overrides)
    return Settings(**valores)


def test_producao_aceita_somente_banco_declarado_como_oficial():
    settings = _settings_producao()

    assert settings.EXPECTED_POSTGRES_HOST == 'db.oficial.local'
    assert settings.EXPECTED_POSTGRES_DATABASE == 'prontocardio_db'


@pytest.mark.parametrize(
    ('database_url', 'mensagem'),
    [
        (
            'postgresql+psycopg://usuario:senha@railway.local/railway',
            'DATABASE_URL não aponta para o PostgreSQL oficial',
        ),
        (
            'postgresql+psycopg://usuario:senha@db.oficial.local/railway',
            'DATABASE_URL não aponta para o PostgreSQL oficial',
        ),
    ],
)
def test_producao_recusa_host_ou_banco_divergente(
    database_url,
    mensagem,
):
    with pytest.raises(ValidationError, match=mensagem):
        _settings_producao(DATABASE_URL=database_url)


def test_producao_exige_identidade_do_banco_oficial():
    with pytest.raises(
        ValidationError,
        match='EXPECTED_POSTGRES_HOST é obrigatório em produção',
    ):
        _settings_producao(EXPECTED_POSTGRES_HOST=None)


@pytest.mark.parametrize(
    'url',
    [
        None,
        '',
        'http://localhost:8765/?reset=1#token={token}',
        'http://127.0.0.1/reset',
        'http://[::1]/reset',
        'http://0.0.0.0/reset',
        '/reset',
        'ftp://example.com/reset',
    ],
)
def test_reset_producao_recusa_destino_ausente_ou_local(url):
    settings = _settings_producao(FRONTEND_PASSWORD_RESET_URL=url)
    with pytest.raises(ValueError, match='FRONTEND_PASSWORD_RESET_URL'):
        _ = settings.frontend_password_reset_url


def test_reset_producao_preserva_url_publica():
    url = 'http://receitacerta.hospitalprontocardio.com.br/redefinir-senha/'
    settings = _settings_producao(FRONTEND_PASSWORD_RESET_URL=url)
    assert settings.frontend_password_reset_url == url


def test_reset_desenvolvimento_preserva_fallback_local():
    settings = _settings_producao(
        APP_ENV='development',
        FRONTEND_PASSWORD_RESET_URL=None,
        FRONTEND_BASE_URL='http://localhost:8080',
    )
    assert (
        settings.frontend_password_reset_url
        == 'http://localhost:8080/autenticacao/redefinir-senha'
    )
