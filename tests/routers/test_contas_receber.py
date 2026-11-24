# ruff: noqa: PLR2004, E501
"""Fluxo do Contas a Receber (entrega 1: leitura)."""

from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app_prontocardio.routers import contas_receber as rota
from app_prontocardio.services.contas_receber_fluxo import (
    ProcessoPortal,
    casar_portal,
    montar_item,
    resumo_funil,
)

HOJE = date(2026, 11, 24)
OUT = date(2026, 10, 1)


def _remessa(cd, valor, competencia=OUT, convenio=46, fechamento=date(2026, 11, 3)):
    return {
        'cd_remessa': cd,
        'cd_convenio': convenio,
        'convenio': 'ISSEC' if convenio == 46 else 'CAMED',
        'competencia': competencia,
        'dt_fechamento': fechamento,
        'valor': Decimal(valor),
        'qt_contas': 3,
    }


def _processo(numero, valor, competencia=OUT):
    return ProcessoPortal(numero, competencia, Decimal(valor), Decimal(valor))


def test_casamento_automatico_so_com_par_unico_nos_dois_sentidos():
    remessas = [
        _remessa(1, '100.00'),
        _remessa(2, '250.00'),
        _remessa(3, '250.00'),  # mesma competência e valor da 2
        _remessa(4, '300.00'),
        _remessa(5, '999.00'),
    ]
    processos = [
        _processo('A', '100.00'),
        _processo('B', '250.00'),
        _processo('C', '300.00'),
        _processo('D', '300.00'),  # dois processos para a remessa 4
        _processo('E', '100.00', competencia=date(2026, 9, 1)),
    ]

    sugestoes = casar_portal(remessas, processos, set(), set())

    assert sugestoes[1]['situacao'] == 'automatico'
    assert [p.numero for p in sugestoes[1]['candidatos']] == ['A']
    assert sugestoes[2]['situacao'] == sugestoes[3]['situacao'] == 'a_confirmar'
    assert sugestoes[4]['situacao'] == 'a_confirmar'
    assert [p.numero for p in sugestoes[4]['candidatos']] == ['C', 'D']
    assert 5 not in sugestoes


def test_casamento_ignora_remessa_e_protocolo_ja_vinculados():
    remessas = [_remessa(1, '100.00'), _remessa(2, '100.00')]
    processos = [_processo('A', '100.00'), _processo('B', '100.00')]

    # A remessa 1 já tem o protocolo B gravado: sobra o par único 2 <-> A.
    sugestoes = casar_portal(remessas, processos, {1}, {'B'})

    assert list(sugestoes) == [2]
    assert sugestoes[2]['situacao'] == 'automatico'


def test_item_sem_protocolo_aguarda_protocolo_e_conta_dias_desde_o_fechamento():
    item = montar_item(_remessa(1, '100.00'), None, None, HOJE)

    assert item['etapa_atual'] == 'protocolo'
    assert item['pendencia'] == 'Aguardando protocolo'
    assert item['dias_na_etapa'] == 21
    assert [e['status'] for e in item['etapas']] == ['concluida', 'atual', 'futura', 'futura']


def test_item_com_protocolo_automatico_aguarda_nfse():
    sugestao = {'situacao': 'automatico', 'candidatos': [_processo('A', '100.00')]}

    item = montar_item(_remessa(1, '100.00'), None, sugestao, HOJE)

    assert item['etapa_atual'] == 'nfse'
    assert item['protocolo']['numero'] == 'A'
    assert item['protocolo']['situacao'] == 'automatico'
    assert item['protocolo_candidatos'] == []


def test_item_a_confirmar_mostra_candidatos_e_continua_sem_protocolo():
    sugestao = {
        'situacao': 'a_confirmar',
        'candidatos': [_processo('C', '300.00'), _processo('D', '300.00')],
    }

    item = montar_item(_remessa(4, '300.00'), None, sugestao, HOJE)

    assert item['etapa_atual'] == 'protocolo'
    assert [c['numero'] for c in item['protocolo_candidatos']] == ['C', 'D']


def _vinculo(nfse=True, recebido=False):
    return {
        'protocolo': {
            'id': 7, 'numero': '2600044294', 'origem': 'manual', 'situacao': 'vinculado',
            'valor_cobrado': None, 'valor_liberado': None,
            'data_protocolo': date(2026, 11, 5),
            'solicitacao_nota_em': date(2026, 11, 10), 'solicitacao_nota_obs': None,
        },
        'nfse': {'numero': '30400', 'prestador_cnpj': '08711085000128',
                 'data_emissao': date(2026, 11, 12), 'valor': Decimal('100.00'),
                 'origem': 'manual'} if nfse else None,
        'recebimentos': [{'id': 1, 'data': date(2026, 11, 20), 'valor': Decimal('500.00'),
                          'valor_alocado': Decimal('100.00'), 'historico': 'TED', 'origem': 'extrato'}]
        if recebido else [],
    }


def test_item_com_nfse_aguarda_recebimento_desde_a_emissao():
    item = montar_item(_remessa(1, '100.00'), _vinculo(), None, HOJE)

    assert item['etapa_atual'] == 'recebimento'
    assert item['dias_na_etapa'] == 12
    assert item['solicitacao_nota'] is True


def test_item_recebido_conclui_o_fluxo():
    item = montar_item(_remessa(1, '100.00'), _vinculo(recebido=True), None, HOJE)

    assert item['etapa_atual'] == 'concluido'
    assert item['dias_na_etapa'] is None
    assert item['valor_recebido'] == Decimal('100.00')


def test_funil_soma_quantidade_e_valor_por_etapa():
    itens = [
        montar_item(_remessa(1, '100.00'), None, None, HOJE),
        montar_item(_remessa(2, '50.00'), _vinculo(), None, HOJE),
        montar_item(_remessa(3, '25.00'), _vinculo(recebido=True), None, HOJE),
        montar_item(_remessa(4, '10.00'), None, {
            'situacao': 'a_confirmar',
            'candidatos': [_processo('C', '10.00'), _processo('D', '10.00')],
        }, HOJE),
    ]

    resumo = resumo_funil(itens)

    por_etapa = {e['chave']: (e['quantidade'], e['valor']) for e in resumo['etapas']}
    assert por_etapa == {
        'protocolo': (2, Decimal('110.00')),
        'nfse': (0, Decimal('0.00')),
        'recebimento': (1, Decimal('50.00')),
        'concluido': (1, Decimal('25.00')),
    }
    assert resumo['valor_total'] == Decimal('185.00')
    assert resumo['protocolos_a_confirmar'] == 1


def test_rota_filtra_pagina_e_funil_ignora_a_etapa(monkeypatch):
    remessas = [
        _remessa(1, '100.00'),
        _remessa(2, '250.00'),
        _remessa(9, '80.00', convenio=12),
    ]
    monkeypatch.setattr(rota, 'remessas_mv', lambda _o: remessas)
    monkeypatch.setattr(rota, 'processos_portal', lambda _p: {46: [_processo('A', '100.00')]})
    monkeypatch.setattr(rota, 'vinculos_gravados', lambda _p: {})
    monkeypatch.setattr(rota, '_hoje', lambda: HOJE)

    resposta = rota.fluxo(
        oracle=None, postgres=None, _usuario=None, convenio=46, etapa='nfse',
        competencia_de=None, competencia_ate=None, q=None, a_confirmar=False,
        ordenar_por='parado', page=1, page_size=10,
    )

    assert [i['cd_remessa'] for i in resposta['itens']] == [1]
    assert resposta['total'] == 1
    assert resposta['resumo']['total_remessas'] == 2
    assert [c['codigo'] for c in resposta['convenios']] == [12, 46]


def test_busca_encontra_por_protocolo_e_competencia_invalida_e_recusada(monkeypatch):
    monkeypatch.setattr(rota, 'remessas_mv', lambda _o: [_remessa(1, '100.00')])
    monkeypatch.setattr(rota, 'processos_portal', lambda _p: {46: [_processo('2600044294', '100.00')]})
    monkeypatch.setattr(rota, 'vinculos_gravados', lambda _p: {})
    monkeypatch.setattr(rota, '_hoje', lambda: HOJE)
    kwargs = dict(oracle=None, postgres=None, _usuario=None, convenio=None, etapa=None,
                  competencia_ate=None, a_confirmar=False, ordenar_por='parado',
                  page=1, page_size=10)

    assert rota.fluxo(competencia_de=None, q='44294', **kwargs)['total'] == 1
    with pytest.raises(HTTPException) as erro:
        rota.fluxo(competencia_de='10/2026', q=None, **kwargs)
    assert erro.value.status_code == 422


def test_acesso_exige_tela_ou_perfil_de_gestao():
    assert rota.acesso_fluxo(SimpleNamespace(perfil='ti', telas_permitidas=[]))
    assert rota.acesso_fluxo(
        SimpleNamespace(perfil='usuario', telas_permitidas=['contas_receber_fluxo'])
    )
    with pytest.raises(HTTPException):
        rota.acesso_fluxo(SimpleNamespace(perfil='usuario', telas_permitidas=[]))


def test_sql_das_remessas_soma_as_contas_e_exclui_particular():
    sql = rota.SQL_REMESSAS_MV.text
    assert "r.sn_fechada = 'S'" in sql
    assert 'dbamv.reg_fat' in sql
    assert 'dbamv.reg_amb' in sql
    assert 'NOT IN (3)' in sql
