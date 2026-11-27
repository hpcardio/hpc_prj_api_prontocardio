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
    casar_por_guia,
    casar_portal,
    conferir_vinculos_pelo_portal,
    config_retencao,
    liquido_do_item,
    montar_item,
    notas_por_nup,
    ratear_nfs,
    resumo_funil,
    situacao_fluxo,
)
from app_prontocardio.services.contas_receber_guias import SenhasDasRemessas

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
        'nfs_autorizada': (0, Decimal('0.00')),
        'nao_autorizada': (0, Decimal('0.00')),
        'aguardando_recebimento': (1, Decimal('50.00')),
        'recebido_sem_conciliacao': (0, Decimal('0.00')),
        'recebido': (1, Decimal('25.00')),
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
    monkeypatch.setattr(rota, 'retencoes_configuradas', lambda _p: {})
    monkeypatch.setattr(rota, 'senhas_mv', lambda _r: {})
    monkeypatch.setattr(rota, 'versao_dos_dados', lambda _p: None)
    monkeypatch.setattr(rota, 'notas_do_portal', lambda _p: {})
    monkeypatch.setattr(rota, '_hoje', lambda: HOJE)

    resposta = rota.fluxo(
        oracle=None,
        postgres=None,
        _usuario=None,
        convenio=46,
        etapa='nao_autorizada',
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
    monkeypatch.setattr(rota, 'retencoes_configuradas', lambda _p: {})
    monkeypatch.setattr(rota, 'senhas_mv', lambda _r: {})
    monkeypatch.setattr(rota, 'versao_dos_dados', lambda _p: None)
    monkeypatch.setattr(rota, 'notas_do_portal', lambda _p: {})
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
    monkeypatch.setattr(rota, 'retencoes_configuradas', lambda _p: {})
    monkeypatch.setattr(rota, 'senhas_mv', lambda _r: {})
    monkeypatch.setattr(rota, 'versao_dos_dados', lambda _p: None)
    monkeypatch.setattr(rota, 'notas_do_portal', lambda _p: {})
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
        'fonte_aliquota': 'historico_nfs',
        'retencoes_detalhe': {},
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


BACEN = config_retencao({
    'iss': '3',
    'ir': '1.2',
    'pis': '0.65',
    'cofins': '3',
    'csll': '1',
})


def test_configuracao_do_convenio_define_a_retencao_estimada_e_o_rateio():
    sem_nf = {
        'cd_remessa': 1,
        'cd_convenio': 7,
        'valor': Decimal('1000.00'),
        'protocolo': None,
        'nfse': None,
    }
    grupo = {
        'numero': '9',
        'prestador_cnpj': 'x',
        'valor': Decimal('5000.00'),
        'valor_liquido': Decimal('4900.00'),
    }
    em_grupo = [
        {
            'cd_remessa': 10 + i,
            'cd_convenio': 7,
            'valor': Decimal('2500.00'),
            'protocolo': None,
            'nfse': dict(grupo),
        }
        for i in range(2)
    ]
    itens = [sem_nf, *em_grupo]
    configs = {7: BACEN}

    ratear_nfs(itens, None, configs)
    aplicar_estimativa(itens, {}, None, configs)

    assert BACEN['total'] == Decimal('8.85')
    assert sem_nf['estimativa']['aliquota'] == Decimal('8.85')
    assert sem_nf['estimativa']['fonte_aliquota'] == 'configuracao'
    assert sem_nf['estimativa']['retencoes'] == Decimal('88.50')
    assert sem_nf['estimativa']['retencoes_detalhe'] == {
        'iss': Decimal('30.00'),
        'ir': Decimal('12.00'),
        'pis': Decimal('6.50'),
        'cofins': Decimal('30.00'),
        'csll': Decimal('10.00'),
    }
    rateio = em_grupo[0]['nf_rateio']
    assert rateio['aliquota_nf'] == Decimal('2.00')
    assert rateio['aliquota'] == Decimal('8.85')
    assert rateio['retencoes'] == Decimal('221.25')
    assert rateio['fonte_aliquota'] == 'configuracao'


def test_convenio_configurado_com_zero_nao_retem():
    item = {
        'cd_remessa': 1,
        'cd_convenio': 9,
        'valor': Decimal('100.00'),
        'protocolo': None,
        'nfse': None,
    }
    zero = config_retencao({})
    aplicar_estimativa([item], {9: Decimal('3.00')}, None, {9: zero})
    assert item['estimativa']['retencoes'] == Decimal('0.00')
    assert item['estimativa']['liquido'] == Decimal('100.00')


class _Resultado:
    def __init__(self, linhas):
        self.linhas = linhas

    def mappings(self):
        return self

    def all(self):
        return self.linhas


class _Sessao:
    def __init__(self, respostas):
        self.respostas = list(respostas)
        self.execucoes = []
        self.commits = 0

    def execute(self, sql, params=None):
        self.execucoes.append((str(sql), params))
        return _Resultado(self.respostas.pop(0) if self.respostas else [])

    def commit(self):
        self.commits += 1


def test_lista_convenios_do_mv_com_os_percentuais_salvos():
    oracle = _Sessao([
        [
            {'cd_convenio': 7, 'nm_convenio': 'BACEN'},
            {'cd_convenio': 9, 'nm_convenio': 'BRADESCO'},
        ]
    ])
    postgres = _Sessao([
        [
            {
                'cd_convenio': 7,
                'convenio': 'BACEN',
                'iss': Decimal('3'),
                'ir': Decimal('1.2'),
                'pis': Decimal('0.65'),
                'cofins': Decimal('3'),
                'csll': Decimal('1'),
                'observacao': None,
                'atualizado_por_nome': 'Financeiro',
                'atualizado_em': None,
            },
        ]
    ])

    linhas = rota.listar_retencoes_convenio(oracle, postgres, None)[
        'convenios'
    ]

    assert linhas[0]['total'] == Decimal('8.85')
    assert linhas[0]['configurado'] is True
    assert linhas[1]['configurado'] is False
    assert linhas[1]['total'] == Decimal('0')


def test_salva_so_o_que_mudou_e_audita():
    postgres = _Sessao([
        [
            {
                'cd_convenio': 7,
                'convenio': 'BACEN',
                'iss': Decimal('3'),
                'ir': Decimal('1.2'),
                'pis': Decimal('0.65'),
                'cofins': Decimal('3'),
                'csll': Decimal('1'),
                'observacao': None,
            },
        ]
    ])
    usuario = SimpleNamespace(id=5, nome='Financeiro')
    payload = [
        rota.RetencaoConvenioInput(
            cd_convenio=7,
            convenio='BACEN',
            iss=3,
            ir=1.2,
            pis=0.65,
            cofins=3,
            csll=1,
        ),
        rota.RetencaoConvenioInput(
            cd_convenio=46, convenio='ISSEC', iss=3, ir=1.2
        ),
    ]

    resposta = rota.salvar_retencoes_convenio(payload, postgres, usuario)

    assert resposta == {'alterados': 1}
    assert postgres.commits == 1
    sqls = [sql for sql, _ in postgres.execucoes]
    assert (
        sum(
            'INSERT INTO api_prontocardio.cr_convenio_retencoes' in s
            for s in sqls
        )
        == 1
    )
    assert sum('cr_eventos' in s for s in sqls) == 1


def test_acesso_a_configuracao():
    assert rota.acesso_configuracao(
        SimpleNamespace(
            perfil='usuario', telas_permitidas=['configuracao_convenio']
        )
    )
    with pytest.raises(HTTPException):
        rota.acesso_configuracao(
            SimpleNamespace(
                perfil='usuario', telas_permitidas=['contas_receber_fluxo']
            )
        )


# ------------------------------------------------ funil com a página Notas Fiscais do portal



def test_situacao_do_funil_pelo_portal_e_pelos_vinculos():
    com_protocolo = {'protocolo': {'numero': 'P'}, 'nfse': None, 'recebimentos': [], 'solicitacao_nota': False}
    assert situacao_fluxo({**com_protocolo, 'protocolo': None}) == 'protocolo'
    assert situacao_fluxo(com_protocolo) == 'nao_autorizada'
    assert situacao_fluxo({**com_protocolo, 'solicitacao_nota': True}) == 'nfs_autorizada'
    assert situacao_fluxo({**com_protocolo, 'nfse': {'numero': '1'}}) == 'aguardando_recebimento'
    # Portal ISSEC: ENVIAR = convênio pediu a NF; sem ENVIAR = NF enviada.
    assert situacao_fluxo(com_protocolo, {'situacao': 'pendente', 'nf_a_enviar': True}) == 'nfs_autorizada'
    assert situacao_fluxo(com_protocolo, {'situacao': 'pendente', 'nf_a_enviar': False}) == 'aguardando_recebimento'
    # Pago no portal: recebido só com o crédito vinculado no extrato.
    assert situacao_fluxo(com_protocolo, {'situacao': 'paga'}) == 'recebido_sem_conciliacao'
    assert situacao_fluxo({**com_protocolo, 'recebimentos': [{'valor': 1}]}, {'situacao': 'paga'}) == 'recebido'


class _ResultadoPortal:
    def __init__(self, rows):
        self.rows = rows

    def scalar_one(self):
        return self.rows[0]

    def mappings(self):
        return self

    def all(self):
        return self.rows

    def first(self):
        return self.rows[0] if self.rows else None


class _SessaoPortal:
    def __init__(self, respostas):
        self.respostas = list(respostas)

    def execute(self, *_a, **_k):
        return _ResultadoPortal(self.respostas.pop(0))


def test_notas_do_portal_paga_vale_mais_que_pendente_e_sem_tabelas_fica_vazio():
    linhas = [
        {'processo': '2600040111', 'situacao': 'pendente', 'nf_a_enviar': True, 'valor_pago': '10.00', 'nup': 'G1', 'pago_desde': None, 'prioridade': 2},
        {'processo': '2600040111', 'situacao': 'paga', 'nf_a_enviar': False, 'valor_pago': '9.58', 'nup': 'G1', 'pago_desde': '2026-11-26', 'prioridade': 1},
        {'processo': '2600050000', 'situacao': 'pendente', 'nf_a_enviar': True, 'valor_pago': '5.00', 'nup': 'G2', 'pago_desde': None, 'prioridade': 2},
    ]
    todas = {'pagas': True, 'pendentes': True, 'processos': True}
    notas = rota.notas_do_portal(_SessaoPortal([[todas], linhas]))
    assert notas['2600040111']['situacao'] == 'paga'
    assert notas['2600040111']['valor'] == Decimal('9.58')
    assert notas['2600050000'] == {'situacao': 'pendente', 'nf_a_enviar': True, 'valor': Decimal('5.00'), 'nup': 'G2', 'pago_desde': None}
    nenhuma = {'pagas': False, 'pendentes': False, 'processos': False}
    assert rota.notas_do_portal(_SessaoPortal([[nenhuma]])) == {}


def test_sem_tabela_de_pendentes_usa_so_as_pagas():
    """Sem pendentes no portal o dlt não cria a tabela: as pagas continuam valendo."""
    sql = rota.sql_notas_portal({'pagas': True, 'pendentes': False, 'processos': True})
    assert 'notas_fiscais_pagas' in sql
    assert 'notas_fiscais_pendentes' not in sql
    assert sql.count('UNION ALL') == 1
    linhas = [{'processo': '2600040111', 'situacao': 'paga', 'nf_a_enviar': False, 'valor_pago': '9.58',
               'nup': 'G1', 'pago_desde': '2026-11-26', 'prioridade': 1}]
    notas = rota.notas_do_portal(_SessaoPortal([[{'pagas': True, 'pendentes': False, 'processos': True}], linhas]))
    assert notas['2600040111']['situacao'] == 'paga'


def test_pendentes_so_da_ultima_carga():
    sql = rota.sql_notas_portal({'pagas': True, 'pendentes': True, 'processos': True})
    assert sql.count('UNION ALL') == 3
    assert sql.count('_dlt_load_id = (') == 2
    assert 'status = 0' in sql
    assert rota.sql_notas_portal({'pagas': False, 'pendentes': True, 'processos': False}).count('UNION ALL') == 0


def test_rota_usa_a_situacao_do_portal(monkeypatch):
    monkeypatch.setattr(rota, 'remessas_mv', lambda _o: [_remessa(1, '100.00'), _remessa(2, '250.00')])
    monkeypatch.setattr(rota, 'processos_portal', lambda _p: {46: [_processo('A', '100.00'), _processo('B', '250.00')]})
    monkeypatch.setattr(rota, 'vinculos_gravados', lambda _p: {})
    monkeypatch.setattr(rota, 'notas_de_grupo', lambda _p: {})
    monkeypatch.setattr(rota, 'retencoes_configuradas', lambda _p: {})
    monkeypatch.setattr(rota, 'senhas_mv', lambda _r: {})
    monkeypatch.setattr(rota, 'versao_dos_dados', lambda _p: None)
    monkeypatch.setattr(rota, 'notas_do_portal', lambda _p: {
        'A': {'situacao': 'pendente', 'nf_a_enviar': True}, 'B': {'situacao': 'paga', 'nf_a_enviar': False}})
    monkeypatch.setattr(rota, '_hoje', lambda: HOJE)

    resposta = rota.fluxo(oracle=None, postgres=None, _usuario=None, convenio=None, etapa=None,
                          competencia_de=None, competencia_ate=None, q=None, a_confirmar=False,
                          ordenar_por='parado', page=1, page_size=10)

    por_remessa = {i['cd_remessa']: i for i in resposta['itens']}
    assert por_remessa[1]['situacao_fluxo'] == 'nfs_autorizada'
    assert por_remessa[1]['pendencia'] == 'NFS autorizadas'
    assert por_remessa[1]['nota_portal']['nf_a_enviar'] is True
    assert por_remessa[2]['situacao_fluxo'] == 'recebido_sem_conciliacao'
    assert por_remessa[2]['dias_na_etapa'] is None
    assert [s['chave'] for s in resposta['situacoes']] == [
        'protocolo', 'nfs_autorizada', 'nao_autorizada', 'aguardando_recebimento', 'recebido_sem_conciliacao', 'recebido']
    assert [s['rotulo'] for s in resposta['situacoes']] == [
        'Aguardando protocolo', 'NFS autorizadas', 'NFS não autorizadas', 'Aguardando recebimento',
        'Recebidas s/ conciliação bancária', 'Recebidas']


def _vinculo_planilha(numero, origem='planilha'):
    v = _vinculo(nfse=True)
    v['protocolo'] = {**v['protocolo'], 'numero': numero, 'origem': 'planilha', 'origem_vinculo': origem}
    return v


def test_vinculo_da_planilha_so_vale_se_o_portal_confirmar():
    """Pedido de 26/11: no ISSEC o protocolo vem do MV × portal; a planilha vira sugestão."""
    remessas = [
        _remessa(1, '100.00'),                      # portal igual
        _remessa(2, '60.00'), _remessa(3, '40.00'),  # duas remessas somam o protocolo B
        _remessa(4, '184.20'),                      # protocolo fora do portal (caso 19714)
        _remessa(5, '70.00'),                       # protocolo no portal com outro valor
        _remessa(6, '80.00'),                       # confirmado por alguém na tela
        _remessa(7, '90.00', convenio=12),          # convênio sem portal
    ]
    vinculos = {
        1: _vinculo_planilha('A'), 2: _vinculo_planilha('B'), 3: _vinculo_planilha('B'),
        4: _vinculo_planilha('X'), 5: _vinculo_planilha('C'), 6: _vinculo_planilha('Y', origem='manual'),
        7: _vinculo_planilha('Z'),
    }
    portal = {46: [_processo('A', '100.00'), _processo('B', '100.00'), _processo('C', '75.00')]}

    validos, sugestoes = conferir_vinculos_pelo_portal(remessas, vinculos, portal)

    assert set(validos) == {1, 2, 3, 6, 7}
    assert sugestoes[4]['numero'] == 'X'
    assert sugestoes[4]['motivo'] == 'Protocolo não encontrado no portal do convênio'
    assert sugestoes[5]['valor_cobrado'] == Decimal('75.00')
    assert 'outra competência ou valor' in sugestoes[5]['motivo']


def test_fluxo_mostra_a_planilha_como_sugestao_e_casa_pelo_portal():
    remessas = [_remessa(4, '184.20'), _remessa(8, '55.00')]
    vinculos = {4: _vinculo_planilha('2600035769'), 8: _vinculo_planilha('W')}
    portal = {46: [_processo('P', '55.00')]}  # a 8 casa com P (planilha dizia W)

    itens = {i['cd_remessa']: i for i in rota.montar_fluxo(remessas, portal, vinculos, HOJE)}

    assert itens[4]['protocolo'] is None
    assert itens[4]['nfse'] is None
    assert itens[4]['protocolo_planilha']['numero'] == '2600035769'
    assert itens[4]['situacao_fluxo'] == 'protocolo'
    assert itens[8]['protocolo']['numero'] == 'P'
    assert itens[8]['protocolo']['situacao'] == 'automatico'
    assert itens[8]['protocolo_planilha'] is None
    assert rota.filtrar(list(itens.values()), q='35769')[0]['cd_remessa'] == 4


def test_grupo_pago_vale_para_o_processo_pelo_nup_do_detalhe():
    """Caso 2600005457: grupo pago no portal sem a lista de membros detalhada."""
    sql = rota.sql_notas_portal({'pagas': True, 'pendentes': False, 'processos': True, 'processos_portal': True})
    assert 'JOIN raw_issec_portal.notas_fiscais_pagas p ON p.nup = pr.nup_grupo' in sql
    assert sql.count('UNION ALL') == 2
    todas = rota.sql_notas_portal({'pagas': True, 'pendentes': True, 'processos': True, 'processos_portal': True})
    assert todas.count('UNION ALL') == 5
    assert todas.count('_dlt_load_id = (') == 3
    # Processo de grupo: o valor pago é do grupo inteiro, não vai para a remessa.
    assert 'SELECT pr.processo, \'paga\', false, NULL::numeric' in sql
    sem_portal = rota.sql_notas_portal({'pagas': True, 'pendentes': False, 'processos': False, 'processos_portal': False})
    assert sem_portal.count('UNION ALL') == 0


def test_processo_em_analise_confirma_pelo_valor_e_nao_casa_sozinho():
    """Caso 20618 / 2600041925: "Em Análise" no portal, sem mês de produção."""
    remessas = [_remessa(20618, '9828.44'), _remessa(20619, '50.00')]
    em_analise = ProcessoPortal('2600041925', None, Decimal('9828.44'), None, situacao='Em Análise')
    outro = ProcessoPortal('2600041999', None, Decimal('50.00'), None, situacao='Em Análise')
    vinculos = {20618: _vinculo_planilha('2600041925')}
    vinculos[20618]['nfse'] = None
    vinculos[20618]['protocolo']['solicitacao_nota_em'] = None

    validos, sugestoes = conferir_vinculos_pelo_portal(remessas, vinculos, {46: [em_analise, outro]})
    assert set(validos) == {20618}
    assert not sugestoes

    # Sem número registrado, processo sem mês de produção não casa por valor.
    assert casar_portal(remessas, [outro], set(), set()) == {}

    itens = {i['cd_remessa']: i for i in rota.montar_fluxo(remessas, {46: [em_analise, outro]}, vinculos, HOJE)}
    assert itens[20618]['protocolo']['numero'] == '2600041925'
    assert itens[20618]['situacao_fluxo'] == 'nao_autorizada'


def test_valor_diferente_em_analise_continua_sugestao():
    remessas = [_remessa(20618, '9828.44')]
    em_analise = ProcessoPortal('2600041925', None, Decimal('1.00'), None)
    _, sugestoes = conferir_vinculos_pelo_portal(remessas, {20618: _vinculo_planilha('2600041925')}, {46: [em_analise]})
    assert sugestoes[20618]['numero'] == '2600041925'


def test_situacao_do_portal_usa_o_motivo_quando_vazia():
    assert "'Motivo da Situação:'" in rota._situacao_portal('ip')
    assert 'mes_producao IS NOT NULL' not in str(rota.SQL_PORTAL[rota.CD_CONVENIO_ISSEC])


def _com_guias(processo, *senhas):
    return ProcessoPortal(processo.numero, processo.competencia, processo.valor_cobrado,
                          processo.valor_liberado, data_entrada=processo.data_entrada, senhas=frozenset(senhas))


def test_guia_decide_o_processo_mesmo_com_competencia_diferente():
    """Caso 2600002999 / 16308: produção 12/2025 no portal, competência 01/2026 no MV."""
    jan = date(2026, 1, 1)
    remessa = {**_remessa(16308, '63.50', competencia=jan), 'senhas': frozenset({'0057P2P18', '0052ZDQIF'})}
    processo = _com_guias(_processo('2600002999', '63.50', competencia=date(2025, 12, 1)), '0057P2P18', '0052ZDQIF')
    outro = _com_guias(_processo('2600009999', '63.50', competencia=jan), '005XXXXXX')

    sugestao = casar_portal([remessa], [processo, outro], set(), set())[16308]
    assert sugestao['criterio'] == 'guia'
    assert sugestao['situacao'] == 'automatico'
    assert [p.numero for p in sugestao['candidatos']] == ['2600002999']

    # Vínculo da planilha: vale se as guias apontam o mesmo processo.
    validos, sugestoes = conferir_vinculos_pelo_portal([remessa], {16308: _vinculo_planilha('2600002999')}, {46: [processo, outro]})
    assert set(validos) == {16308}
    assert not sugestoes
    _, sugestoes = conferir_vinculos_pelo_portal([remessa], {16308: _vinculo_planilha('2600009999')}, {46: [processo, outro]})
    assert 'As guias da remessa estão no processo 2600002999' in sugestoes[16308]['motivo']

    item = rota.montar_fluxo([_remessa(16308, '63.50', competencia=jan)], {46: [processo, outro]}, {}, HOJE,
                             senhas={16308: frozenset({'0057P2P18', '0052ZDQIF'})})[0]
    assert item['protocolo']['numero'] == '2600002999'
    assert item['protocolo']['criterio'] == 'guia'
    assert 'senhas' not in item


def test_guia_com_mais_senhas_em_comum_e_empate_pelo_mes():
    out = date(2026, 10, 1)
    remessa = {**_remessa(1, '10.00'), 'senhas': frozenset({'A', 'B', 'C'})}
    um = _com_guias(_processo('P1', '10.00', competencia=out), 'A')
    dois = _com_guias(_processo('P2', '10.00', competencia=date(2026, 9, 1)), 'A', 'B')
    assert casar_por_guia([remessa], [um, dois])[1]['candidatos'][0].numero == 'P2'

    remessa = {**_remessa(1, '10.00'), 'senhas': frozenset({'A', 'B'})}
    empate = _com_guias(_processo('P3', '10.00', competencia=date(2026, 9, 1)), 'A')
    sugestao = casar_por_guia([remessa], [um, empate])[1]
    assert [p.numero for p in sugestao['candidatos']] == ['P1']  # mesmo mês da remessa

    sem_mes = _com_guias(_processo('P4', '10.00', competencia=date(2026, 8, 1)), 'A')
    sugestao = casar_por_guia([remessa], [empate, sem_mes])[1]
    assert sugestao['situacao'] == 'a_confirmar'


def test_guias_conhecidas_sem_nada_em_comum_nao_casam_por_valor():
    remessa = {**_remessa(1, '10.00'), 'senhas': frozenset({'A'})}
    processo = _com_guias(_processo('P', '10.00'), 'Z')
    assert casar_portal([remessa], [processo], set(), set()) == {}
    # Sem guias no portal (ex.: internamento), segue mês de produção + valor.
    sugestao = casar_portal([remessa], [_processo('P', '10.00')], set(), set())[1]
    assert sugestao['criterio'] == 'mes_valor'


def test_senhas_das_remessas_em_lotes_e_so_as_novas():
    chamadas = []

    def carregar(lote):
        chamadas.append(list(lote))
        return {cd: {f'S{cd}'} for cd in lote if cd != 3}

    cache = SenhasDasRemessas(carregar, em_segundo_plano=False)
    assert cache.obter([1, 2, 3]) == {1: frozenset({'S1'}), 2: frozenset({'S2'}), 3: frozenset()}
    cache.obter([1, 2, 3, 4])
    assert chamadas == [[1, 2, 3], [4]]


def test_senha_repetida_em_outro_mes_nao_basta():
    """Menos da metade das senhas da remessa no processo: não casa pela guia."""
    remessa = {**_remessa(1, '10.00'), 'senhas': frozenset({'A', 'B', 'C'})}
    seriado = _com_guias(_processo('P', '99.00', competencia=date(2026, 9, 1)), 'A', 'X', 'Y')
    assert casar_por_guia([remessa], [seriado]) == {}
    metade = {**_remessa(2, '10.00'), 'senhas': frozenset({'A', 'B'})}
    assert casar_por_guia([metade], [seriado])[2]['candidatos'][0].numero == 'P'


def test_cache_do_fluxo_remonta_quando_a_versao_muda(monkeypatch):
    montagens = []
    cache = rota._CacheFluxo()

    def montar():
        montagens.append(1)
        return [len(montagens)]

    assert cache.obter(('v1',), montar) == [1]
    assert cache.obter(('v1',), montar) == [1]  # filtros/cards: sem remontar
    assert cache.obter(('v2',), montar) == [2]  # vínculo alterado ou nova carga
    assert cache.obter(None, montar) == [3]  # sem versão: sempre monta
    monkeypatch.setattr(rota, 'FLUXO_TTL_SEGUNDOS', -1)
    cache.obter(('v3',), montar)
    cache.obter(('v3',), montar)
    assert len(montagens) == 5  # expirado: remonta


def test_versao_dos_dados_sem_tabelas_volta_none():
    class Falha:
        def execute(self, *_a, **_k):
            raise RuntimeError('sem _dlt_loads')

        def rollback(self):
            self.rolou = True

    sessao = Falha()
    assert rota.versao_dos_dados(sessao) is None
    assert sessao.rolou


def test_cards_de_recebimento_somam_o_valor_liquido():
    """Pedido de 27/11: aguardando recebimento e recebidas mostram o líquido."""
    def item(cd, situacao, **extra):
        return {'cd_remessa': cd, 'valor': Decimal('100.00'), 'situacao_fluxo': situacao,
                'protocolo_candidatos': [], **extra}

    itens = [
        item(1, 'aguardando_recebimento', nf_rateio={'liquido': Decimal('90.00')}, nfse={'valor_liquido': '999'}),
        item(2, 'aguardando_recebimento', nfse={'valor_liquido': '80.00'}),
        item(3, 'recebido_sem_conciliacao', estimativa={'liquido': Decimal('70.00')}),
        item(4, 'recebido'),  # sem NF nem estimativa: o total
        item(5, 'nao_autorizada', nfse={'valor_liquido': '10.00'}),  # card de total
    ]
    assert liquido_do_item(itens[0]) == Decimal('90.00')
    etapas = {e['chave']: e for e in resumo_funil(itens)['etapas']}
    assert etapas['aguardando_recebimento']['valor'] == Decimal('170.00')
    assert etapas['aguardando_recebimento']['base_valor'] == 'liquido'
    assert etapas['recebido_sem_conciliacao']['valor'] == Decimal('70.00')
    assert etapas['recebido']['valor'] == Decimal('100.00')
    assert etapas['nao_autorizada']['valor'] == Decimal('100.00')
    assert etapas['nao_autorizada']['base_valor'] == 'total'


def test_senhas_tentam_de_novo_e_registram_a_falha(caplog):
    tentativas = []

    def carregar(lote):
        tentativas.append(list(lote))
        if len(tentativas) == 1:
            raise RuntimeError('ORA-03113')
        return {cd: {'S'} for cd in lote}

    cache = SenhasDasRemessas(carregar, em_segundo_plano=False, espera_entre_tentativas=0)
    with caplog.at_level('WARNING', logger='uvicorn.error'):
        assert cache.obter([1, 2]) == {1: frozenset({'S'}), 2: frozenset({'S'})}
    assert len(tentativas) == 2
    assert 'tentativa 1 de 3' in caplog.text
    assert '2 remessas carregadas, 0 com falha' in caplog.text
    assert cache.faltam([1, 2, 3]) == 1


def test_lote_que_sempre_falha_nao_trava_a_carga():
    cache = SenhasDasRemessas(lambda lote: (_ for _ in ()).throw(RuntimeError('x')),
                              em_segundo_plano=False, espera_entre_tentativas=0)
    assert cache.obter([1]) == {}
    assert cache.faltam([1]) == 1
    # A próxima consulta tenta de novo (não fica "carregando" para sempre).
    assert cache.obter([1]) == {}


def test_carga_na_subida_so_no_servico_payables(monkeypatch):
    iniciadas = []
    monkeypatch.setattr(rota.threading, 'Thread', lambda **kw: iniciadas.append(kw) or SimpleNamespace(start=lambda: None))
    monkeypatch.delenv('PAYABLES_MV_CALL_TIMEOUT_MS', raising=False)
    rota.iniciar_carga_das_senhas()
    assert iniciadas == []
    monkeypatch.setenv('PAYABLES_MV_CALL_TIMEOUT_MS', '20000')
    rota.iniciar_carga_das_senhas()
    assert iniciadas[0]['name'] == 'senhas-subida'


def test_fluxo_informa_guias_carregando(monkeypatch):
    monkeypatch.setattr(rota, 'remessas_mv', lambda _o: [_remessa(1, '100.00')])
    monkeypatch.setattr(rota, 'processos_portal', lambda _p: {46: []})
    monkeypatch.setattr(rota, 'vinculos_gravados', lambda _p: {})
    monkeypatch.setattr(rota, 'notas_de_grupo', lambda _p: {})
    monkeypatch.setattr(rota, 'retencoes_configuradas', lambda _p: {})
    monkeypatch.setattr(rota, 'notas_do_portal', lambda _p: {})
    monkeypatch.setattr(rota, 'senhas_mv', lambda _r: {})
    monkeypatch.setattr(rota, 'versao_dos_dados', lambda _p: None)
    monkeypatch.setattr(rota, 'guias_carregando', lambda _r: True)
    resposta = rota.fluxo(oracle=None, postgres=None, _usuario=None, convenio=None, etapa=None,
                          competencia_de=None, competencia_ate=None, q=None, a_confirmar=False,
                          ordenar_por='parado', page=1, page_size=10)
    assert resposta['guias_carregando'] is True
