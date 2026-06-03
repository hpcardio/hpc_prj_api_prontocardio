from http import HTTPStatus
from unittest.mock import Mock

import app_prontocardio.routers.autenticacao as autenticacao_router
from app_prontocardio.database import get_session_postgres
from app_prontocardio.models import Usuario


def _novo_usuario() -> Usuario:
    usuario = Usuario(
        nome='Tobias',
        email='tobias@example.com',
        senha='hash-existente',
    )
    usuario.id = 2
    return usuario


def test_token_retorna_access_token_quando_credenciais_validas(
    cliente,
    override_dependencies,
    monkeypatch,
):
    # Confirma login bem-sucedido quando usuário e senha são válidos.
    db_session = Mock()
    db_session.scalar.return_value = _novo_usuario()

    def override_session_postgres():
        yield db_session

    override_dependencies[get_session_postgres] = override_session_postgres
    monkeypatch.setattr(
        autenticacao_router,
        'valida_senha_cru_x_senha_hash_db',
        lambda senha_cru, senha_hash: True,
    )
    monkeypatch.setattr(
        autenticacao_router,
        'criar_token',
        lambda claim: 'token-fixo',
    )

    response = cliente.post(
        '/autenticacao/token',
        data={'username': 'tobias@example.com', 'password': 'senha-correta'},
    )

    assert response.status_code == HTTPStatus.OK
    assert response.json() == {
        'access_token': 'token-fixo',
        'token_type': 'Bearer',
    }


def test_token_retorna_401_quando_usuario_nao_existe(
    cliente,
    override_dependencies,
):
    # Garante erro de autenticação quando o e-mail não está cadastrado.
    db_session = Mock()
    db_session.scalar.return_value = None

    def override_session_postgres():
        yield db_session

    override_dependencies[get_session_postgres] = override_session_postgres

    response = cliente.post(
        '/autenticacao/token',
        data={'username': 'naoexiste@example.com', 'password': 'qualquer'},
    )

    assert response.status_code == HTTPStatus.UNAUTHORIZED
    assert response.json()['detail'] == 'Email ou Senha incorretos'


def test_token_retorna_401_quando_senha_invalida(
    cliente,
    override_dependencies,
    monkeypatch,
):
    # Valida retorno 401 quando a senha informada não confere com o hash.
    db_session = Mock()
    db_session.scalar.return_value = _novo_usuario()

    def override_session_postgres():
        yield db_session

    override_dependencies[get_session_postgres] = override_session_postgres
    monkeypatch.setattr(
        autenticacao_router,
        'valida_senha_cru_x_senha_hash_db',
        lambda senha_cru, senha_hash: False,
    )

    response = cliente.post(
        '/autenticacao/token',
        data={'username': 'tobias@example.com', 'password': 'senha-errada'},
    )

    assert response.status_code == HTTPStatus.UNAUTHORIZED
    assert response.json()['detail'] == 'Email ou Senha incorretos'
