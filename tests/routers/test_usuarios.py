from http import HTTPStatus
from unittest.mock import Mock

import app_prontocardio.routers.usuarios as usuarios_router
from app_prontocardio.database import get_session_postgres
from app_prontocardio.models import Usuario
from app_prontocardio.security import valida_token_usuario_atual


def _novo_usuario(nome: str, email: str, user_id: int) -> Usuario:
    usuario = Usuario(nome=nome, email=email, senha='hash-inicial')
    usuario.id = user_id
    return usuario


def test_consultar_usuario_retorna_lista_sem_senha(
    cliente,
    override_dependencies,
    usuario_autenticado,
):
    # Garante que a listagem retorna usuários públicos sem expor senha.
    db_session = Mock()
    db_session.scalars.return_value.all.return_value = [
        _novo_usuario('Tobias', 'tobias@example.com', 2)
    ]

    def override_session_postgres():
        yield db_session

    override_dependencies[get_session_postgres] = override_session_postgres
    override_dependencies[valida_token_usuario_atual] = lambda: (
        usuario_autenticado
    )

    response = cliente.get('/usuarios/?offset=0&limit=10')

    assert response.status_code == HTTPStatus.OK
    assert response.json() == {
        'usuarios': [
            {'id': 2, 'nome': 'Tobias', 'email': 'tobias@example.com'}
        ]
    }


def test_consultar_usuario_retorna_401_sem_token(
    cliente,
    override_dependencies,
):
    # Garante que endpoint protegido exige Authorization Bearer válido.
    db_session = Mock()

    def override_session_postgres():
        yield db_session

    override_dependencies[get_session_postgres] = override_session_postgres

    response = cliente.get('/usuarios/?offset=0&limit=10')

    assert response.status_code == HTTPStatus.UNAUTHORIZED
    assert response.json()['detail'] == 'Not authenticated'
    db_session.scalars.assert_not_called()


def test_criar_usuario_retorna_201_com_usuario_publico(
    cliente,
    override_dependencies,
    monkeypatch,
):
    # Valida criação de usuário com hash de senha e resposta sem senha.
    db_session = Mock()
    db_session.scalar.return_value = None

    def refresh_usuario(usuario):
        usuario.id = 99

    db_session.refresh.side_effect = refresh_usuario

    def override_session_postgres():
        yield db_session

    override_dependencies[get_session_postgres] = override_session_postgres
    monkeypatch.setattr(
        usuarios_router,
        'gera_hash_senha',
        lambda senha: 'hash-123',
    )

    response = cliente.post(
        '/usuarios/usuarios/',
        json={
            'nome': 'Novo Usuario',
            'email': 'novo@example.com',
            'senha': 'senha-plana',
        },
    )

    assert response.status_code == HTTPStatus.CREATED
    assert response.json() == {
        'id': 99,
        'nome': 'Novo Usuario',
        'email': 'novo@example.com',
    }


def test_alterar_usuario_retorna_403_quando_usuario_nao_tem_permissao(
    cliente,
    override_dependencies,
):
    # Impede alteração quando o usuário autenticado tenta editar outro id.
    db_session = Mock()

    def override_session_postgres():
        yield db_session

    override_dependencies[get_session_postgres] = override_session_postgres
    override_dependencies[valida_token_usuario_atual] = lambda: _novo_usuario(
        'Logado', 'logado@example.com', 1
    )

    response = cliente.put(
        '/usuarios/usuarios/2',
        json={
            'nome': 'Nome Alterado',
            'email': 'alterado@example.com',
            'senha': 'nova-senha',
        },
    )

    assert response.status_code == HTTPStatus.FORBIDDEN
    assert response.json()['detail'] == 'Usuário sem permissão!!'


def test_deletar_usuario_retorna_mensagem_sucesso(
    cliente,
    override_dependencies,
):
    # Confirma deleção quando o usuário autenticado remove o próprio registro.
    usuario_logado = _novo_usuario('Logado', 'logado@example.com', 10)
    db_session = Mock()

    def override_session_postgres():
        yield db_session

    override_dependencies[get_session_postgres] = override_session_postgres
    override_dependencies[valida_token_usuario_atual] = lambda: usuario_logado

    response = cliente.delete('/usuarios/usuarios/10')

    assert response.status_code == HTTPStatus.OK
    assert response.json() == {'message': 'Usuário Excluído!'}
    db_session.delete.assert_called_once_with(usuario_logado)
