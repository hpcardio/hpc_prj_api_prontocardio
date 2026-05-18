from http import HTTPStatus

# from fastapi.testclient import TestClient
from sqlalchemy import text

# from app_prontocardio.app import app


def test_root(cliente):

    # cliente = TestClient(app)

    response = cliente.get('/')

    assert response.status_code == HTTPStatus.OK
    assert response.json() == {
        'message': 'Olá Mundo! API Hospital Prontocardio'
    }


def test_oracle_conn(session):
    rows = session.execute(
        text('SELECT * FROM v$version')
    ).all()

    # breakpoint()

    assert rows
