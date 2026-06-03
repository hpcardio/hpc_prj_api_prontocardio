import pytest
from fastapi.testclient import TestClient

from app_prontocardio.app import app
from app_prontocardio.models import Usuario


@pytest.fixture
def cliente():
    with TestClient(app) as client:
        yield client


@pytest.fixture
def override_dependencies():
    app.dependency_overrides = {}
    yield app.dependency_overrides
    app.dependency_overrides = {}


@pytest.fixture
def usuario_autenticado() -> Usuario:
    usuario = Usuario(
        nome='Usuario Teste',
        email='usuario.teste@example.com',
        senha='hash-senha',
    )
    usuario.id = 1
    return usuario
