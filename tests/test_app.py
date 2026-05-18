from http import HTTPStatus

from fastapi.testclient import TestClient

from app_prontocardio.app import app


def test_root():

    cliente = TestClient(app)

    response = cliente.get('/')

    assert response.status_code == HTTPStatus.OK
    assert response.json() == {'message': 'Olá Mundo! API Hospital Prontocardio'}
