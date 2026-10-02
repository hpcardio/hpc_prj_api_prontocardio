from datetime import date
from decimal import Decimal
from http import HTTPStatus

import pytest
from fastapi import HTTPException

from app_prontocardio.routers import repasse_medico


class Resultado:
    def __init__(self, rows):
        self._rows = rows

    def mappings(self):
        return self

    def all(self):
        return self._rows


class Sessao:
    def __init__(self, respostas):
        self.respostas = list(respostas)
        self.chamadas = []

    def execute(self, statement, params=None):
        self.chamadas.append((str(statement), params or {}))
        return Resultado(self.respostas.pop(0))


def linha(descricao='ANGIOTOMOGRAFIA CORONARIANA', valor='300.00'):
    return {
        'cd_repasse': 91,
        'cd_prestador': 245,
        'cd_prestador_destino': 245,
        'nm_prestador_destino': 'ALEXANDRE MOURAO FEITOSA',
        'cd_pro_fat': '41001230',
        'ds_procedimento': descricao,
        'quantidade': 1,
        'valor_repasse': Decimal(valor),
        'valor_desconto': Decimal('0'),
    }


def test_preview_exclui_teste_ergometrico_e_bloqueia_envio():
    sessao = Sessao([[linha(), linha('TESTE ERGOMÉTRICO', '120.00')]])

    preview = repasse_medico.montar_preview(
        sessao, date(2026, 9, 1), empresa=1
    )

    assert [item['procedimento'] for item in preview['itens']] == [
        'ANGIOTOMOGRAFIA CORONARIANA'
    ]
    assert preview['total_liquido'] == Decimal('300.00')
    assert preview['bloqueado'] is True
    assert preview['excluidos_teste_ergometrico']['quantidade'] == 1
    assert preview['excluidos_teste_ergometrico']['valor'] == Decimal(
        '120.00'
    )
    assert preview['token_confirmacao']


def test_envio_rejeita_preview_desatualizado_sem_chamar_procedure():
    sessao = Sessao([[linha()]])
    payload = repasse_medico.EnvioRepasseInput(
        competencia=date(2026, 9, 1),
        data_pagamento=date(2026, 10, 10),
        empresa=1,
        token_confirmacao='0' * 64,
    )

    with pytest.raises(HTTPException) as erro:
        repasse_medico.enviar_repasse(payload, object(), sessao)

    assert erro.value.status_code == HTTPStatus.CONFLICT
    assert len(sessao.chamadas) == 1


def test_envio_chama_a_procedure_existente_com_os_tres_parametros():
    primeira = Sessao([[linha()]])
    preview = repasse_medico.montar_preview(
        primeira, date(2026, 9, 1), empresa=1
    )
    sessao = Sessao([[linha()], []])
    payload = repasse_medico.EnvioRepasseInput(
        competencia=date(2026, 9, 1),
        data_pagamento=date(2026, 10, 10),
        empresa=1,
        token_confirmacao=preview['token_confirmacao'],
    )

    resposta = repasse_medico.enviar_repasse(payload, object(), sessao)

    sql_procedure, parametros = sessao.chamadas[1]
    assert 'DBAMV.PGTO_REP_GERAL' in sql_procedure
    assert parametros == {
        'competencia': date(2026, 9, 1),
        'data_pagamento': date(2026, 10, 10),
        'empresa': 1,
    }
    assert resposta['status'] == 'enviado'


def test_envio_bloqueia_quando_existe_teste_ergometrico_pendente():
    sessao = Sessao(
        [[linha(), linha('TESTE ERGOMETRICO', '120.00')]]
    )
    preview = repasse_medico.montar_preview(
        Sessao([[linha(), linha('TESTE ERGOMETRICO', '120.00')]]),
        date(2026, 9, 1),
        empresa=1,
    )
    payload = repasse_medico.EnvioRepasseInput(
        competencia=date(2026, 9, 1),
        data_pagamento=date(2026, 10, 10),
        empresa=1,
        token_confirmacao=preview['token_confirmacao'],
    )

    with pytest.raises(HTTPException) as erro:
        repasse_medico.enviar_repasse(payload, object(), sessao)

    assert erro.value.status_code == HTTPStatus.CONFLICT
    assert 'ergométrico' in erro.value.detail.lower()
    assert len(sessao.chamadas) == 1
