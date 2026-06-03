from http import HTTPStatus
from unittest.mock import Mock

from app_prontocardio.database import get_session_oracle


def test_read_root_retorna_mensagem_boas_vindas(cliente):
    # Verifica o endpoint público inicial do router livre.
    response = cliente.get('/livre/')

    assert response.status_code == HTTPStatus.OK
    assert response.json() == {
        'message': 'Bem-vindo à API Hospital Prontocardio'
    }


def test_consultar_versao_oracle_retorna_lista(cliente, override_dependencies):
    # Garante a serialização da versão do Oracle retornada pela sessão.
    db_session = Mock()
    query_result = Mock()
    mapped_result = Mock()
    mapped_result.all.return_value = [
        {'banner': 'Oracle Database 19c Enterprise Edition'}
    ]
    query_result.mappings.return_value = mapped_result
    db_session.execute.return_value = query_result

    def override_session_oracle():
        yield db_session

    override_dependencies[get_session_oracle] = override_session_oracle

    response = cliente.get('/livre/consultar_versao_oracle/')

    assert response.status_code == HTTPStatus.OK
    assert response.json() == [
        {'banner': 'Oracle Database 19c Enterprise Edition'}
    ]
