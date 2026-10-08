from datetime import date
from decimal import Decimal

import pytest

from app_prontocardio.routers import repasse_medico
from app_prontocardio.services.repasse_mv_financeiro import (
    CancelamentoBloqueado,
    classificar_titulo,
    token_cancelamento,
    validar_cancelamento,
)

TOKEN_SHA256_LENGTH = 64


class Resultado:
    def __init__(self, rows):
        self.rows = rows

    def mappings(self):
        return self

    def all(self):
        return self.rows


class Sessao:
    def __init__(self, rows):
        self.rows = rows

    def execute(self, _statement, _params=None):
        return Resultado(self.rows)


def _linha(descricao='ANGIOTOMOGRAFIA CORONARIANA', valor='300.00'):
    return {
        'cd_repasse': 91,
        'cd_prestador': 245,
        'cd_prestador_destino': 245,
        'nm_prestador_destino': 'PRESTADOR TESTE',
        'cd_pro_fat': '41001230',
        'ds_procedimento': descricao,
        'quantidade': 1,
        'valor_repasse': Decimal(valor),
        'valor_desconto': Decimal('0'),
    }


def _titulo(**parcela):
    dados_parcela = {
        'cd_itcon_pag': 10,
        'tp_quitacao': 'P',
        'sn_baixada': 'N',
        'vl_pago': Decimal('0'),
        'qtd_pagamentos': 0,
        'cd_agrupamento': None,
    }
    dados_parcela.update(parcela)
    return {
        'cd_con_pag': 100,
        'cd_repasse': 91,
        'qtd_repasses': 1,
        'parcelas': [dados_parcela],
    }


def test_preview_exclui_teste_ergometrico_e_bloqueia_envio():
    sessao = Sessao(
        [_linha(), _linha('TESTE ERGOMÉTRICO', '120.00')]
    )

    preview = repasse_medico.montar_preview(
        sessao, date(2026, 9, 1), empresa=1
    )

    assert [item['procedimento'] for item in preview['itens']] == [
        'ANGIOTOMOGRAFIA CORONARIANA'
    ]
    assert preview['total_liquido'] == Decimal('300.00')
    assert preview['bloqueado'] is True
    assert preview['excluidos_teste_ergometrico'] == {
        'quantidade': 1,
        'valor': Decimal('120.00'),
    }
    assert len(preview['atendimentos']) == 1
    assert len(preview['atendimentos_teste_ergometrico']) == 1
    assert (
        preview['atendimentos_teste_ergometrico'][0]['procedimento']
        == 'TESTE ERGOMÉTRICO'
    )
    assert preview['token_confirmacao']


@pytest.mark.parametrize(
    ('alteracao', 'estado'),
    [
        ({}, 'previsto'),
        ({'vl_pago': Decimal('1')}, 'pago'),
        ({'sn_baixada': 'S'}, 'baixado'),
        ({'cd_agrupamento': 20}, 'agrupado'),
        ({'tp_quitacao': 'C'}, 'cancelado'),
    ],
)
def test_classifica_estado_financeiro_do_titulo(alteracao, estado):
    assert classificar_titulo(_titulo(**alteracao)) == estado


def test_cancelamento_valida_repasse_e_estado_previsto():
    titulo = _titulo()

    assert validar_cancelamento(titulo, 91) is True
    assert len(token_cancelamento(titulo)) == TOKEN_SHA256_LENGTH

    with pytest.raises(CancelamentoBloqueado):
        validar_cancelamento(titulo, 92)


def test_cancelamento_bloqueia_titulo_pago():
    with pytest.raises(CancelamentoBloqueado, match='pago'):
        validar_cancelamento(_titulo(vl_pago=Decimal('1')), 91)
