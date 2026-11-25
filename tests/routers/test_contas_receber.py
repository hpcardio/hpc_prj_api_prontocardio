# ruff: noqa: PLR2004, E501
"""Fluxo do Contas a Receber (entrega 1: leitura)."""

from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app_prontocardio.permissions import TELAS_PADRAO, normalizar_telas
from app_prontocardio.routers import contas_receber as rota
from app_prontocardio.services.contas_receber_fluxo import (
    ProcessoPortal,
    aliquotas_por_convenio,
    aplicar_estimativa,
    bases_das_remessas,
    casar_portal,
    montar_item,
    notas_por_nup,
    ratear_nfs,
    resumo_funil,
)

HOJE = date(2026, 11, 24)
OUT = date(2026, 10, 1)


def _remessa(
    cd, valor, competencia=OUT, convenio=46, fechamento=date(2026, 11, 3)
):
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
    assert (
        sugestoes[2]['situacao'] == sugestoes[3]['situacao'] == 'a_confirmar'
    )
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
    assert [e['status'] for e in item['etapas']] == [
        'concluida',
        'atual',
        'futura',
        'futura',
    ]


def test_item_com_protocolo_automatico_aguarda_nfse():
    sugestao = {
        'situacao': 'automatico',
        'candidatos': [_processo('A', '100.00')],
    }

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
            'id': 7,
            'numero': '2600044294',
            'origem': 'manual',
            'situacao': 'vinculado',
            'valor_cobrado': None,
            'valor_liberado': None,
            'data_protocolo': date(2026, 11, 5),
            'solicitacao_nota_em': date(2026, 11, 10),
            'solicitacao_nota_obs': None,
        },
        'nfse': {
            'numero': '30400',
            'prestador_cnpj': '08711085000128',
            'data_emissao': date(2026, 11, 12),
            'valor': Decimal('100.00'),
            'origem': 'manual',
        }
        if nfse
        else None,
        'recebimentos': [
            {
                'id': 1,
                'data': date(2026, 11, 20),
                'valor': Decimal('500.00'),
                'valor_alocado': Decimal('100.00'),
                'historico': 'TED',
                'origem': 'extrato',
            }
        ]
        if recebido
        else [],
    }


def test_item_com_nfse_aguarda_recebimento_desde_a_emissao():
    item = montar_item(_remessa(1, '100.00'), _vinculo(), None, HOJE)

    assert item['etapa_atual'] == 'recebimento'
    assert item['dias_na_etapa'] == 12
    assert item['solicitacao_nota'] is True


def test_item_recebido_conclui_o_fluxo():
    item = montar_item(
        _remessa(1, '100.00'), _vinculo(recebido=True), None, HOJE
    )

    assert item['etapa_atual'] == 'concluido'
    assert item['dias_na_etapa'] is None
    assert item['valor_recebido'] == Decimal('100.00')


def test_funil_soma_quantidade_e_valor_por_etapa():
    itens = [
        montar_item(_remessa(1, '100.00'), None, None, HOJE),
        montar_item(_remessa(2, '50.00'), _vinculo(), None, HOJE),
        montar_item(_remessa(3, '25.00'), _vinculo(recebido=True), None, HOJE),
        montar_item(
            _remessa(4, '10.00'),
            None,
            {
                'situacao': 'a_confirmar',
                'candidatos': [
                    _processo('C', '10.00'),
                    _processo('D', '10.00'),
                ],
            },
            HOJE,
        ),
    ]

    resumo = resumo_funil(itens)

    por_etapa = {
        e['chave']: (e['quantidade'], e['valor']) for e in resumo['etapas']
    }
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
    monkeypatch.setattr(
        rota, 'processos_portal', lambda _p: {46: [_processo('A', '100.00')]}
    )
    monkeypatch.setattr(rota, 'vinculos_gravados', lambda _p: {})
    monkeypatch.setattr(rota, 'notas_de_grupo', lambda _p: {})
    monkeypatch.setattr(rota, '_hoje', lambda: HOJE)

    resposta = rota.fluxo(
        oracle=None,
        postgres=None,
        _usuario=None,
        convenio=46,
        etapa='nfse',
        competencia_de=None,
        competencia_ate=None,
        q=None,
        a_confirmar=False,
        ordenar_por='parado',
        page=1,
        page_size=10,
    )

    assert [i['cd_remessa'] for i in resposta['itens']] == [1]
    assert resposta['total'] == 1
    assert resposta['resumo']['total_remessas'] == 2
    assert [c['codigo'] for c in resposta['convenios']] == [12, 46]


def test_busca_encontra_por_protocolo_e_competencia_invalida_e_recusada(
    monkeypatch,
):
    monkeypatch.setattr(
        rota, 'remessas_mv', lambda _o: [_remessa(1, '100.00')]
    )
    monkeypatch.setattr(
        rota,
        'processos_portal',
        lambda _p: {46: [_processo('2600044294', '100.00')]},
    )
    monkeypatch.setattr(rota, 'vinculos_gravados', lambda _p: {})
    monkeypatch.setattr(rota, 'notas_de_grupo', lambda _p: {})
    monkeypatch.setattr(rota, '_hoje', lambda: HOJE)
    kwargs = dict(
        oracle=None,
        postgres=None,
        _usuario=None,
        convenio=None,
        etapa=None,
        competencia_ate=None,
        a_confirmar=False,
        ordenar_por='parado',
        page=1,
        page_size=10,
    )

    assert rota.fluxo(competencia_de=None, q='44294', **kwargs)['total'] == 1
    with pytest.raises(HTTPException) as erro:
        rota.fluxo(competencia_de='10/2026', q=None, **kwargs)
    assert erro.value.status_code == 422


def test_acesso_exige_tela_ou_perfil_de_gestao():
    assert rota.acesso_fluxo(SimpleNamespace(perfil='ti', telas_permitidas=[]))
    assert rota.acesso_fluxo(
        SimpleNamespace(
            perfil='usuario', telas_permitidas=['contas_receber_fluxo']
        )
    )
    with pytest.raises(HTTPException):
        rota.acesso_fluxo(
            SimpleNamespace(perfil='usuario', telas_permitidas=[])
        )


def test_sql_das_remessas_soma_as_contas_e_exclui_particular():
    sql = rota.SQL_REMESSAS_MV.text
    assert "r.sn_fechada = 'S'" in sql
    assert 'dbamv.reg_fat' in sql
    assert 'dbamv.reg_amb' in sql
    assert 'NOT IN (3)' in sql


def test_tela_do_fluxo_e_valida_e_restrita():
    assert normalizar_telas(['contas_receber_fluxo', 'indicadores']) == [
        'indicadores',
        'contas_receber_fluxo',
    ]
    assert 'contas_receber_fluxo' not in TELAS_PADRAO


def test_exportar_devolve_todas_as_remessas_sem_paginar(monkeypatch):
    remessas = [_remessa(cd, '10.00') for cd in range(1, 26)]
    remessas[0]['dt_entrega'] = date(2026, 11, 3)
    remessas[0]['dt_prevista_pgto'] = date(2026, 12, 18)
    monkeypatch.setattr(rota, 'remessas_mv', lambda _o: remessas)
    monkeypatch.setattr(rota, 'processos_portal', lambda _p: {})
    monkeypatch.setattr(rota, 'vinculos_gravados', lambda _p: {})
    monkeypatch.setattr(rota, 'notas_de_grupo', lambda _p: {})
    monkeypatch.setattr(rota, '_hoje', lambda: HOJE)
    kwargs = dict(
        oracle=None,
        postgres=None,
        _usuario=None,
        convenio=None,
        etapa=None,
        competencia_de=None,
        competencia_ate=None,
        q=None,
        a_confirmar=False,
        ordenar_por='parado',
        page=1,
        page_size=10,
    )

    assert len(rota.fluxo(**kwargs)['itens']) == 10
    todos = rota.fluxo(exportar=True, **kwargs)['itens']
    assert len(todos) == 25
    item = next(i for i in todos if i['cd_remessa'] == 1)
    assert item['dt_entrega'] == date(2026, 11, 3)
    assert item['dt_prevista_pgto'] == date(2026, 12, 18)


def test_sql_traz_datas_do_mv_e_liquido_da_nfse():
    assert 'r.dt_entrega_da_fatura AS dt_entrega' in rota.SQL_REMESSAS_MV.text
    assert 'r.dt_prevista_para_pgto AS dt_prevista_pgto' in (
        rota.SQL_REMESSAS_MV.text
    )
    assert 'valor_liquido_nfse' in rota.SQL_VINCULOS.text


def test_vinculos_leem_o_portal_na_hora_e_a_entrada_no_protocolo():
    sql = rota.SQL_VINCULOS.text
    assert 'COALESCE(pt.valor_liberado, p.valor_liberado)' in sql
    assert 'COALESCE(p.data_protocolo, pt.data_entrada)' in sql
    assert "'Entrada no Protocolo:'" in sql
    assert 'TO_DATE(' in rota.SQL_PORTAL[rota.CD_CONVENIO_ISSEC].text


def test_protocolo_automatico_usa_a_entrada_no_portal():
    processo = ProcessoPortal(
        '2600042216',
        OUT,
        Decimal('1968.18'),
        Decimal('1328.30'),
        date(2026, 11, 13),
        'LIBERADO',
    )
    sugestao = {'situacao': 'automatico', 'candidatos': [processo]}

    item = montar_item(_remessa(20586, '1968.18'), None, sugestao, HOJE)

    assert item['protocolo']['data_protocolo'] == date(2026, 11, 13)
    assert item['protocolo']['situacao_portal'] == 'LIBERADO'
    assert item['dias_na_etapa'] == 11


def _item_com_nf(cd, valor, liquido, convenio=46):
    return {
        'cd_remessa': cd,
        'cd_convenio': convenio,
        'valor': Decimal(valor),
        'protocolo': None,
        'nfse': {'valor': Decimal(valor), 'valor_liquido': Decimal(liquido)},
    }


def test_retencao_estimada_pela_aliquota_do_convenio():
    historico = [_item_com_nf(i, '1000.00', '958.00') for i in range(6)]
    historico.append(_item_com_nf(99, '1000.00', '911.50'))  # fora do padrão
    sem_nf_liberado = {
        'cd_remessa': 200,
        'cd_convenio': 46,
        'valor': Decimal('1968.18'),
        'protocolo': {
            'valor_cobrado': Decimal('1968.18'),
            'valor_liberado': Decimal('1328.30'),
        },
        'nfse': None,
    }
    sem_nf_bruto = {
        'cd_remessa': 201,
        'cd_convenio': 46,
        'valor': Decimal('825.00'),
        'protocolo': None,
        'nfse': None,
    }
    outro_convenio = {
        'cd_remessa': 300,
        'cd_convenio': 12,
        'valor': Decimal('10.00'),
        'protocolo': None,
        'nfse': None,
    }
    itens = [*historico, sem_nf_liberado, sem_nf_bruto, outro_convenio]

    aliquotas = aliquotas_por_convenio(itens)
    aplicar_estimativa(itens, aliquotas)

    assert aliquotas == {46: Decimal('4.20')}
    assert sem_nf_liberado['estimativa'] == {
        'aliquota': Decimal('4.20'),
        'base': Decimal('1328.30'),
        'origem_base': 'liberado',
        'retencoes': Decimal('55.79'),
        'liquido': Decimal('1272.51'),
    }
    assert sem_nf_bruto['estimativa']['origem_base'] == 'bruto'
    assert sem_nf_bruto['estimativa']['retencoes'] == Decimal('34.65')
    assert outro_convenio['estimativa'] is None
    assert historico[0]['estimativa'] is None  # NF real não é estimada


def test_aliquota_exige_amostra_e_consenso():
    poucos = [_item_com_nf(i, '1000.00', '958.00') for i in range(4)]
    dividido = [_item_com_nf(i, '1000.00', '958.00') for i in range(5)] + [
        _item_com_nf(10 + i, '1000.00', '970.00') for i in range(5)
    ]
    assert aliquotas_por_convenio(poucos) == {}
    assert aliquotas_por_convenio(dividido) == {}


def test_nf_de_grupo_e_rateada_pelo_liberado_de_cada_remessa():
    nf_grupo = {
        'numero': '30083',
        'prestador_cnpj': '08711085000128',
        'valor': Decimal('572156.82'),
        'valor_liquido': Decimal('548126.24'),
    }
    remessa = {
        'cd_remessa': 20157,
        'cd_convenio': 46,
        'valor': Decimal('2118.58'),
        'protocolo': {'valor_liberado': Decimal('2118.58')},
        'nfse': dict(nf_grupo),
    }
    outra = {
        'cd_remessa': 20158,
        'cd_convenio': 46,
        'valor': Decimal('5000.00'),
        'protocolo': None,
        'nfse': dict(nf_grupo),
    }
    exclusiva = {
        'cd_remessa': 16402,
        'cd_convenio': 46,
        'valor': Decimal('7190.00'),
        'protocolo': None,
        'nfse': {
            'numero': '25618',
            'prestador_cnpj': '08711085000128',
            'valor': Decimal('7190.00'),
            'valor_liquido': Decimal('6888.02'),
        },
    }

    ratear_nfs([remessa, outra, exclusiva])

    rateio = remessa['nf_rateio']
    assert rateio['compartilhada'] is True
    assert rateio['remessas_na_nf'] == 2
    assert rateio['aliquota'] == Decimal('4.20')
    assert rateio['valor'] == Decimal('2118.58')
    assert rateio['retencoes'] == Decimal('88.98')
    assert rateio['liquido'] == Decimal('2029.60')
    assert outra['nf_rateio']['origem_base'] == 'bruto'
    assert outra['nf_rateio']['valor'] == Decimal('5000.00')
    assert exclusiva['nf_rateio'] == {
        'compartilhada': False,
        'valor': Decimal('7190.00'),
        'retencoes': Decimal('301.98'),
        'liquido': Decimal('6888.02'),
        'aliquota': Decimal('4.20'),
    }


def test_nf_maior_que_a_remessa_tambem_e_rateada():
    item = {
        'cd_remessa': 1,
        'cd_convenio': 46,
        'valor': Decimal('100.00'),
        'protocolo': None,
        'nfse': {
            'numero': '1',
            'valor': Decimal('900.00'),
            'valor_liquido': None,
        },
    }
    ratear_nfs([item])
    assert item['nf_rateio']['compartilhada'] is True
    assert item['nf_rateio']['valor'] == Decimal('100.00')
    assert item['nf_rateio']['retencoes'] is None


def test_liberado_do_processo_e_dividido_entre_as_remessas_do_protocolo():
    protocolo = {
        'numero': '2600003000',
        'valor_cobrado': Decimal('1000.00'),
        'valor_liberado': Decimal('900.00'),
    }
    itens = [
        {
            'cd_remessa': cd,
            'cd_convenio': 46,
            'valor': Decimal(valor),
            'protocolo': dict(protocolo),
            'nfse': None,
        }
        for cd, valor in ((1, '600.00'), (2, '400.00'))
    ]

    bases = bases_das_remessas(itens)

    assert bases[1] == (Decimal('540.00'), 'liberado_proporcional')
    assert bases[2] == (Decimal('360.00'), 'liberado_proporcional')
    aplicar_estimativa(itens, {46: Decimal('4.20')}, bases)
    assert itens[0]['estimativa']['retencoes'] == Decimal('22.68')


def test_processo_do_grupo_recebe_a_nf_do_nup_e_a_retencao_rateada():
    notas = notas_por_nup([
        {
            'numero': '30083',
            'prestador_cnpj': '08711085000128',
            'data_emissao': date(2026, 11, 5),
            'valor': Decimal('572156.82'),
            'valor_liquido': Decimal('548126.24'),
            'discriminacao': 'REF AS DESPESAS... NUP 46042042895202610',
        }
    ])
    processo = ProcessoPortal(
        '2600036705',
        date(2026, 8, 1),
        Decimal('2118.58'),
        Decimal('2118.58'),
        date(2026, 9, 28),
        None,
        '46042042895202610',
    )
    sugestao = {'situacao': 'automatico', 'candidatos': [processo]}
    outro = ProcessoPortal(
        '2600036706',
        date(2026, 8, 1),
        Decimal('5000.00'),
        Decimal('4000.00'),
        None,
        None,
        '46042042895202610',
    )
    itens = [
        montar_item(
            _remessa(20157, '2118.58', competencia=date(2026, 8, 1)),
            None,
            sugestao,
            HOJE,
            notas,
        ),
        montar_item(
            _remessa(20158, '5000.00', competencia=date(2026, 8, 1)),
            None,
            {'situacao': 'automatico', 'candidatos': [outro]},
            HOJE,
            notas,
        ),
    ]
    ratear_nfs(itens)

    primeiro, segundo = itens
    assert primeiro['nfse']['numero'] == segundo['nfse']['numero'] == '30083'
    assert primeiro['nfse']['origem'] == 'nup_grupo'
    assert primeiro['etapa_atual'] == 'recebimento'
    assert primeiro['nf_rateio']['retencoes'] == Decimal('88.98')
    assert primeiro['nf_rateio']['liquido'] == Decimal('2029.60')
    assert segundo['nf_rateio']['valor'] == Decimal('4000.00')
    assert segundo['nf_rateio']['retencoes'] == Decimal('168.00')


def test_nf_gravada_tem_prioridade_sobre_a_do_grupo():
    notas = notas_por_nup([
        {
            'numero': '1',
            'data_emissao': date(2026, 1, 1),
            'valor': Decimal('1'),
            'valor_liquido': Decimal('1'),
            'discriminacao': 'NUP 46042042895202610',
        },
    ])
    vinculo = {
        'protocolo': {
            'id': 1,
            'numero': '2600036705',
            'origem': 'planilha',
            'situacao': 'vinculado',
            'valor_cobrado': None,
            'valor_liberado': None,
            'data_protocolo': None,
            'nup_grupo': '46042042895202610',
            'solicitacao_nota_em': None,
            'solicitacao_nota_obs': None,
        },
        'nfse': {'numero': '30083', 'origem': 'planilha'},
        'recebimentos': [],
    }
    item = montar_item(_remessa(20157, '10.00'), vinculo, None, HOJE, notas)
    assert item['nfse']['numero'] == '30083'
