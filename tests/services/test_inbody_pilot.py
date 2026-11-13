from datetime import datetime
from http import HTTPStatus
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from app_prontocardio.services.ecg_oracle_worklist import (
    OracleWorklistClient,
    OracleWorklistError,
)
from pilot_inbody_api import create_app

TOKEN = 'x' * 40
HEADERS = {'X-InBody-Integration-Token': TOKEN}


def _engine(rows):
    engine = MagicMock()
    conexao = engine.connect.return_value.__enter__.return_value
    conexao.execute.return_value.mappings.return_value.all.return_value = rows
    return engine, conexao


def _linha_status(**extra):
    linha = {
        'order_number': '10',
        'order_item_id': '20',
        'attendance_number': '30',
        'patient_id': '40',
        'procedure_code': '597',
        'realized': 0,
        'pending': 1,
        'realized_at': None,
    }
    linha.update(extra)
    return linha


def test_token_curto_impede_subir_o_piloto():
    with pytest.raises(RuntimeError):
        create_app(MagicMock(), 'curto')


def test_piloto_exige_token():
    cliente = TestClient(create_app(MagicMock(), TOKEN))
    resposta = cliente.get('/inbody/worklist/health')
    assert resposta.status_code == HTTPStatus.UNAUTHORIZED


def test_piloto_health_e_sem_cache():
    falso = MagicMock()
    cliente = TestClient(create_app(falso, TOKEN))
    resposta = cliente.get('/inbody/worklist/health', headers=HEADERS)
    assert resposta.status_code == HTTPStatus.OK
    assert resposta.headers['cache-control'] == 'no-store'
    falso.health.assert_called_once()


def test_piloto_converte_timeout_do_mv():
    falso = MagicMock()
    falso.health.side_effect = OracleWorklistError('MV_TIMEOUT')
    cliente = TestClient(create_app(falso, TOKEN))
    resposta = cliente.get('/inbody/worklist/health', headers=HEADERS)
    assert resposta.status_code == HTTPStatus.GATEWAY_TIMEOUT


def test_status_do_item_le_um_unico_item():
    engine, conexao = _engine([_linha_status()])
    cliente = OracleWorklistClient(engine, procedure_code=597)
    status = cliente.get_item_status('10', '20', '30')
    assert status.pending is True
    assert status.realized is False
    parametros = conexao.execute.call_args.args[1]
    assert parametros == {
        'order_number': 10,
        'order_item_id': 20,
        'attendance_number': 30,
        'procedure_code': 597,
    }


def test_status_do_item_rejeita_resultado_ambiguo():
    engine, _ = _engine([_linha_status(), _linha_status()])
    cliente = OracleWorklistClient(engine, procedure_code=597)
    with pytest.raises(OracleWorklistError) as erro:
        cliente.get_item_status('10', '20', '30')
    assert erro.value.code == 'MV_INVALID_RESULT'


def test_status_do_item_rejeita_identificador_invalido():
    engine, _ = _engine([])
    cliente = OracleWorklistClient(engine, procedure_code=597)
    with pytest.raises(OracleWorklistError) as erro:
        cliente.get_item_status('abc', '20', '30')
    assert erro.value.code == 'MV_INVALID_IDENTIFIER'


def test_status_realizado():
    realizado = _linha_status(
        realized=1, pending=0, realized_at=datetime(2026, 9, 1)
    )
    engine, _ = _engine([realizado])
    cliente = OracleWorklistClient(engine, 597)
    status = cliente.get_item_status('10', '20', '30')
    assert status.realized is True


LINHA_BUSCA_INVALIDA = {
    'accession_number': None,
    'patient_id': '40',
    'patient_name': 'PACIENTE FICTICIO',
    'scheduled_at': None,
    'patient_birth_date': None,
    'patient_sex': 'M',
    'procedure_code': '597',
    'procedure_description': 'BIOIMPEDANCIOMETRIA',
    'requested_procedure_id': '30',
    'scheduled_procedure_step_id': '20',
}


def test_busca_rejeita_linha_invalida_quando_configurado():
    engine, _ = _engine([LINHA_BUSCA_INVALIDA])
    with pytest.raises(OracleWorklistError):
        OracleWorklistClient(
            engine, 597, reject_invalid_rows=True
        ).search(MagicMock(identifier='1', search_type=MagicMock()))


def test_busca_ignora_linha_invalida_por_padrao():
    engine, _ = _engine([LINHA_BUSCA_INVALIDA])
    resultado = OracleWorklistClient(engine, 597).search(
        MagicMock(identifier='1', search_type=MagicMock())
    )
    assert resultado == []
