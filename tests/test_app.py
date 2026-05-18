from http import HTTPStatus

from sqlalchemy import select, text

from app_prontocardio.models import ModelContaAtendimento


def test_root(cliente):

    response = cliente.get('/')

    assert response.status_code == HTTPStatus.OK
    assert response.json() == {
        'message': 'Olá Mundo! API Hospital Prontocardio'
    }


def test_oracle_conn(session):
    rows = session.execute(
        text('SELECT * FROM v$version')
    ).all()

    # breackpoint()

    assert rows


def test_conta_atendimento_found(cliente, session):
    # Buscar um cd_atendimento válido da view
    first_record = session.execute(
        select(ModelContaAtendimento).limit(1)
    ).scalar()

    if first_record:
        response = cliente.get(
            f'/conta_atendimento/{first_record.cd_atendimento}'
        )

        assert response.status_code == HTTPStatus.OK
        data = response.json()
        assert isinstance(data, dict)
        assert 'atendimentos' in data
        assert isinstance(data['atendimentos'], list)
        assert len(data['atendimentos']) > 0
        assert 'cd_atendimento' in data['atendimentos'][0]


def test_conta_atendimento_not_found(cliente):
    response = cliente.get('/conta_atendimento/999999999')

    assert response.status_code == HTTPStatus.NOT_FOUND
    assert 'não encontrado' in response.json()['detail']


def test_conta_atendimento_invalid_path(cliente):
    response = cliente.get('/conta_atendimento/0')

    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
