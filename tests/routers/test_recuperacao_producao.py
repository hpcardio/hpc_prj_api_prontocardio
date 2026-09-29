from datetime import date, datetime
from http import HTTPStatus

import pytest
from fastapi import HTTPException

from app_prontocardio.agendamento_duplicidade import (
    duplicidade_exige_confirmacao,
)
from app_prontocardio.routers.biq import consultar_altas_hospitalares
from app_prontocardio.routers.biq_admissoes_eletivas import build_payload
from app_prontocardio.routers.painel_hemodinamica import health, resumo

VALOR_UNITARIO = 70.0
QUANTIDADE_ALTAS = 2
VALOR_DUAS_ALTAS = 140.0


class ResultadoMapeado:
    def __init__(self, rows):
        self.rows = rows

    def mappings(self):
        return self

    def all(self):
        return self.rows


class SessaoFake:
    def __init__(self, rows):
        self.rows = rows
        self.params = None

    def execute(self, _query, params):
        self.params = params
        return ResultadoMapeado(self.rows)


def test_duplicidade_exige_confirmacao_explicita():
    duplicado = {'cd_it_agenda_central': 123}

    assert duplicidade_exige_confirmacao(duplicado, False) is True
    assert duplicidade_exige_confirmacao(duplicado, True) is False
    assert duplicidade_exige_confirmacao(None, False) is False


def test_admissoes_eletivas_filtra_documento_e_totaliza_medico():
    rows = [
        {
            'cd_prestador': 10,
            'nm_prestador': 'MEDICO TESTE',
            'crm': '123',
            'dh_fechamento': datetime(2026, 9, 1, 8),
            'sn_fechado': 'S',
            'tp_status': 'FECHADO',
            'texto': (
                '### ADMISSÃO MÉDICA PARA PROCEDIMENTO ELETIVO ### detalhes'
            ),
        },
        {
            'cd_prestador': 10,
            'nm_prestador': 'MEDICO TESTE',
            'crm': '123',
            'dh_fechamento': datetime(2026, 9, 1, 9),
            'sn_fechado': 'N',
            'tp_status': 'ABERTO',
            'texto': (
                '### ADMISSÃO MÉDICA PARA PROCEDIMENTO ELETIVO ### detalhes'
            ),
        },
    ]

    payload = build_payload(rows, date(2026, 9, 1), date(2026, 9, 2))

    assert payload['total_admissoes'] == 1
    assert payload['valor_total'] == VALOR_UNITARIO
    assert payload['medicos'] == [
        {
            'cd_prestador': 10,
            'nm_prestador': 'MEDICO TESTE',
            'crm': '123',
            'quantidade_admissoes': 1,
            'valor_total': 70.0,
        }
    ]


def test_altas_hospitalares_totaliza_por_medico():
    session = SessaoFake(
        [
            {
                'cd_prestador': 10,
                'nm_prestador': 'MEDICO TESTE',
                'crm': '123',
                'dt_alta': datetime(2026, 9, 1, 10),
            },
            {
                'cd_prestador': 10,
                'nm_prestador': 'MEDICO TESTE',
                'crm': '123',
                'dt_alta': datetime(2026, 9, 2, 10),
            },
        ]
    )

    payload = consultar_altas_hospitalares(
        object(), date(2026, 9, 1), date(2026, 9, 2), session
    )

    assert session.params['data_fim_exclusiva'] == date(2026, 9, 3)
    assert payload['total_altas'] == QUANTIDADE_ALTAS
    assert payload['valor_total'] == VALOR_DUAS_ALTAS
    assert payload['medicos'][0]['quantidade_altas'] == QUANTIDADE_ALTAS
    assert payload['medicos'][0]['valor_total'] == VALOR_DUAS_ALTAS


def test_painel_hemodinamica_health_e_periodo_invalido():
    assert health() == {
        'status': 'ok',
        'servico': 'painel-hemodinamica',
    }

    with pytest.raises(HTTPException) as exc_info:
        resumo(date(2026, 9, 2), date(2026, 9, 1))

    assert exc_info.value.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert exc_info.value.detail == 'Período inválido'
