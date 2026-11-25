from http import HTTPStatus
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

from jwt import decode

from app_prontocardio.routers import autenticacao
from app_prontocardio.settings import Settings

HOSTINGER_SMTP_PORT = 465
SMTP_TIMEOUT_SECONDS = 10


def _settings_teste(**kwargs):
    return Settings(
        ORACLE_DATABASE_URL='oracle+oracledb://usuario:senha@host:1521/db',
        POSTGRES_SCHEMA='api_prontocardio_test',
        SECRET_KEY='secret-test',
        ALGORITHM='HS256',
        **kwargs,
    )


def test_configuracao_smtp_hostinger_usa_ssl_e_aliases():
    settings = _settings_teste(
        SMTP_HOST='smtp.hostinger.com',
        SMTP_PORT=465,
        SMTP_USER='usuario_smtp@exemplo.local',
        SMTP_PASSWORD='senha_do_email',
        SMTP_FROM='TI Hospital Prontocardio <usuario_smtp@exemplo.local>',
    )

    assert settings.smtp_username == 'usuario_smtp@exemplo.local'
    assert settings.smtp_from_email.startswith('TI Hospital Prontocardio')
    assert settings.smtp_use_ssl is True
    assert settings.smtp_use_tls is False


def test_link_redefinicao_usa_url_explicitada_do_frontend(monkeypatch):
    settings = _settings_teste(
        FRONTEND_BASE_URL='https://api.exemplo.local',
        FRONTEND_PASSWORD_RESET_URL=(
            'https://app.exemplo.local'
            '/autenticacao/redefinir-senha#token={token}'
        ),
    )

    monkeypatch.setattr(autenticacao, 'settings', settings)

    assert autenticacao._montar_link_redefinicao('abc_123') == (
        'https://app.exemplo.local/autenticacao/redefinir-senha#token=abc_123'
    )


def test_envio_redefinicao_usa_smtp_ssl_com_hostinger(monkeypatch):
    settings = _settings_teste(
        FRONTEND_BASE_URL='https://api.exemplo.local',
        FRONTEND_PASSWORD_RESET_URL=(
            'https://app.exemplo.local'
            '/autenticacao/redefinir-senha#token={token}'
        ),
        SMTP_HOST='smtp.hostinger.com',
        SMTP_PORT=465,
        SMTP_USER='usuario_smtp@exemplo.local',
        SMTP_PASSWORD='senha_do_email',
        SMTP_FROM='TI Hospital Prontocardio <usuario_smtp@exemplo.local>',
    )
    smtp_instances = []

    class SMTPSSLTeste:
        def __init__(self, host, port, timeout):
            self.host = host
            self.port = port
            self.timeout = timeout
            self.starttls_chamado = False
            self.login_args = None
            self.mensagem = None
            smtp_instances.append(self)

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def starttls(self):
            self.starttls_chamado = True

        def login(self, username, password):
            self.login_args = (username, password)

        def send_message(self, mensagem):
            self.mensagem = mensagem

    def smtp_starttls_inesperado(*args, **kwargs):
        raise AssertionError('SMTP sem SSL nao deveria ser usado')

    monkeypatch.setattr(autenticacao, 'settings', settings)
    monkeypatch.setattr(autenticacao.smtplib, 'SMTP_SSL', SMTPSSLTeste)
    monkeypatch.setattr(autenticacao.smtplib, 'SMTP', smtp_starttls_inesperado)

    autenticacao._enviar_email_redefinicao('usuario@teste.com', 'token')

    smtp = smtp_instances[0]
    assert smtp.host == 'smtp.hostinger.com'
    assert smtp.port == HOSTINGER_SMTP_PORT
    assert smtp.timeout == SMTP_TIMEOUT_SECONDS
    assert smtp.starttls_chamado is False
    assert smtp.login_args == (
        'usuario_smtp@exemplo.local',
        'senha_do_email',
    )
    assert smtp.mensagem['From'].startswith('TI Hospital Prontocardio')
    assert smtp.mensagem['To'] == 'usuario@teste.com'

    conteudo = smtp.mensagem.get_content()
    assert (
        'https://app.exemplo.local/autenticacao/redefinir-senha#token=token'
    ) in conteudo
    assert '?token=' not in conteudo
    assert 'https://api.exemplo.local' not in conteudo


def test_token_painel_medicos_tem_audiencia_e_expiracao(
    session, usuario_teste
):
    usuario_teste.perfil = 'leitura'
    session.commit()

    response = autenticacao.autenticar_usuario_painel_medicos(
        SimpleNamespace(
            username=usuario_teste.email,
            password=usuario_teste.senha_limpa,
        ),
        session,
    )

    payload = decode(
        response['access_token'],
        autenticacao.settings.SECRET_KEY,
        algorithms=[autenticacao.settings.ALGORITHM],
        audience='painel_leitos_medicos',
    )
    assert payload['sub'] == usuario_teste.email
    assert payload['exp'] > payload.get('iat', 0)


def test_link_receita_certa_entrega_token_por_query(monkeypatch):
    settings = _settings_teste(
        FRONTEND_PASSWORD_RESET_URL='http://receitacerta.hospitalprontocardio.com.br/redefinir-senha/'
    )
    monkeypatch.setattr(autenticacao, 'settings', settings)
    link = urlsplit(autenticacao._montar_link_redefinicao('token+teste/='))
    assert link.hostname == 'receitacerta.hospitalprontocardio.com.br'
    assert link.path == '/redefinir-senha/'
    assert parse_qs(link.query) == {'token': ['token+teste/=']}
    assert not link.fragment


TOKEN_TESTE = 'token-de-teste-com-mais-de-32-caracteres-123'


def test_redefinir_senha_por_email_encerra_a_troca_obrigatoria(
    cliente, session, usuario_teste
):
    """Quem redefine pelo link do e-mail já escolheu a própria senha.

    17/11/2026: usuário com senha temporária ficava travado mesmo depois de
    redefinir pelo e-mail.
    """
    from datetime import datetime, timedelta, timezone  # noqa: PLC0415
    from hashlib import sha256  # noqa: PLC0415

    from app_prontocardio.models import (  # noqa: PLC0415
        TokenRedefinicaoSenha,
    )

    usuario_teste.troca_senha_obrigatoria = True
    session.add(TokenRedefinicaoSenha(
        usuario_id=usuario_teste.id,
        token_hash=sha256(TOKEN_TESTE.encode()).hexdigest(),
        expira_em=datetime.now(timezone.utc) + timedelta(hours=1),
    ))
    session.commit()

    resposta = cliente.post(
        '/autenticacao/redefinir-senha',
        json={'token': TOKEN_TESTE, 'nova_senha': 'NovaSenha#2026'},
    )
    assert resposta.status_code == HTTPStatus.OK

    session.refresh(usuario_teste)
    assert usuario_teste.troca_senha_obrigatoria is False
    login = cliente.post(
        '/autenticacao/token',
        data={'username': usuario_teste.email, 'password': 'NovaSenha#2026'},
    )
    assert login.status_code == HTTPStatus.OK


def test_login_aceita_cpf_com_ou_sem_pontuacao_e_login_mv(cliente, session):
    from app_prontocardio.models import Usuario  # noqa: PLC0415
    from app_prontocardio.security import gera_hash_senha  # noqa: PLC0415

    session.add(Usuario(
        nome='Colaborador Setor', email='rh000938@mv.receitacerta.local',
        senha=gera_hash_senha('prontocardio'), nome_usuario='RH000938',
        cpf='52998224725',
    ))
    session.commit()

    for login in ('529.982.247-25', '52998224725', 'rh000938', 'RH000938'):
        resposta = cliente.post(
            '/autenticacao/token',
            data={'username': login, 'password': 'prontocardio'},
        )
        assert resposta.status_code == HTTPStatus.OK, login

    errado = cliente.post(
        '/autenticacao/token',
        data={'username': '529.982.247-26', 'password': 'prontocardio'},
    )
    assert errado.status_code == HTTPStatus.UNAUTHORIZED


def test_cpf_do_login_so_reconhece_onze_digitos():
    assert autenticacao._cpf_do_login('529.982.247-25') == '52998224725'
    assert autenticacao._cpf_do_login('RH000938') is None
    assert autenticacao._cpf_do_login('1234') is None
    assert autenticacao._cpf_do_login('ana@x.com') is None


def test_usuario_antigo_entra_pelo_login_mv_vinculado(cliente, session):
    from sqlalchemy import text  # noqa: PLC0415

    from app_prontocardio.models import Usuario  # noqa: PLC0415
    from app_prontocardio.security import gera_hash_senha  # noqa: PLC0415

    usuario = Usuario(
        nome='Usuário Antigo', email='antigo@x.com',
        senha=gera_hash_senha('minha-senha'), nome_usuario='Antigo',
    )
    session.add(usuario)
    session.commit()
    session.execute(text(
        'CREATE TABLE usuarios_mv (usuario_id INTEGER, cd_usuario_mv TEXT)'
    ))
    session.execute(
        text("INSERT INTO usuarios_mv VALUES (:id, 'RH000938')"),
        {'id': usuario.id},
    )
    session.commit()

    for login in ('RH000938', 'rh000938', 'Antigo'):
        resposta = cliente.post(
            '/autenticacao/token',
            data={'username': login, 'password': 'minha-senha'},
        )
        assert resposta.status_code == HTTPStatus.OK, login
    outro = cliente.post(
        '/autenticacao/token',
        data={'username': 'RH000111', 'password': 'minha-senha'},
    )
    assert outro.status_code == HTTPStatus.UNAUTHORIZED
