# ruff: noqa: PLR2004, E501
"""Títulos em acordo: dívida vencida renegociada no MV (Prorrogar/Parcelar).

O acordo soma na dívida vencida, em faixa própria, e sai do "a vencer". O
saldo a negociar, os dias de atraso e a série histórica seguem só o atraso
real.
"""

from datetime import date
from decimal import Decimal

from app_prontocardio.routers import contas_pagar

HOJE = date(2026, 11, 23)
BASE = {
    'codigo_fornecedor': 1616,
    'nome_fornecedor': 'Fornecedor de teste',
    'codigo_contas_pagar': 92102,
    'numero_documento': '11025',
    'descricao_conta': 'Compras',
    'numero_parcela': 1,
    'tipo_quitacao': 'comprometido',
    'valor_honrado_oracle': Decimal('0'),
}


def _titulos():
    return [
        # Em atraso, sem acordo.
        {
            **BASE,
            'codigo_parcela': 1,
            'valor_total': Decimal('1360'),
            'data_vencimento': date(2026, 11, 19),
        },
        # Acordo cujo novo vencimento também já passou: conta como atraso.
        {
            **BASE,
            'codigo_parcela': 2,
            'valor_total': Decimal('780'),
            'data_vencimento': date(2026, 11, 19),
            'data_vencimento_original': date(2026, 4, 10),
            'data_acordo': date(2026, 11, 12),
        },
        # Parcela nova do "Parcelar", herda o vencimento original.
        {
            **BASE,
            'codigo_parcela': 3,
            'valor_total': Decimal('580'),
            'numero_parcela': 2,
            'data_vencimento': date(2026, 12, 21),
            'data_vencimento_original': date(2026, 4, 10),
            'data_acordo': date(2026, 11, 12),
            'acordo_parcela_nova': True,
        },
        # A vencer, sem acordo.
        {
            **BASE,
            'codigo_parcela': 4,
            'valor_total': Decimal('100'),
            'data_vencimento': date(2027, 1, 20),
        },
    ]


def test_acordo_a_vencer_soma_na_divida_vencida_em_faixa_propria(monkeypatch):
    monkeypatch.setattr(contas_pagar, '_hoje_hospital', lambda: HOJE)

    fornecedor = contas_pagar._agrupar_fornecedores(_titulos(), {})[0]

    assert fornecedor['valor_vencido_em_atraso'] == Decimal('2140')
    assert fornecedor['valor_em_acordo'] == Decimal('580')
    assert fornecedor['valor_total_vencido'] == Decimal('2720')
    assert fornecedor['valor_corrente'] == Decimal('100')
    assert fornecedor['titulos_em_acordo'] == 1
    assert fornecedor['titulos_vencidos'] == 2
    # Os dias de atraso seguem o vencimento atual.
    assert fornecedor['dias_atraso'] == 4
    situacoes = {
        t['codigo_parcela']: t['situacao_acordo']
        for t in fornecedor['titulos']
    }
    assert situacoes == {1: None, 2: 'em_atraso', 3: 'a_vencer', 4: None}


def test_saldo_a_negociar_ignora_o_que_ja_esta_em_acordo(monkeypatch):
    monkeypatch.setattr(contas_pagar, '_hoje_hospital', lambda: HOJE)
    fornecedor = contas_pagar._agrupar_fornecedores(_titulos(), {})[0]

    contas_pagar._enriquecer_fornecedores([fornecedor], {})

    assert fornecedor['valor_vencido'] == Decimal('2720')
    assert fornecedor['saldo_negociar'] == Decimal('2140')


def test_cards_separam_acordo_de_a_vencer(monkeypatch):
    monkeypatch.setattr(contas_pagar, '_hoje_hospital', lambda: HOJE)
    titulos = contas_pagar._agrupar_fornecedores(_titulos(), {})[0]['titulos']

    def codigos(componente):
        return sorted(
            t['codigo_parcela']
            for t in contas_pagar._titulos_do_componente(
                titulos, componente, False
            )
        )

    assert codigos('vencida') == [1, 2, 3]
    assert codigos('em_acordo') == [3]
    assert codigos('a_vencer') == [4]
    assert codigos('saldo_negociar') == [1, 2]


def test_fornecedor_antigo_sem_campos_de_acordo_continua_valido():
    item = {
        'codigo_fornecedor': 1,
        'valor_total_vencido': Decimal('50'),
        'dias_atraso': 3,
    }
    contas_pagar._enriquecer_fornecedores([item], {})
    assert item['valor_vencido_em_atraso'] == Decimal('50')
    assert item['valor_em_acordo'] == Decimal('0')
    assert item['saldo_negociar'] == Decimal('50')


def test_consultas_identificam_acordo_pelo_historico_e_pelo_parcelamento():
    for consulta in (
        contas_pagar.ORACLE_FORNECEDORES_QUERY,
        contas_pagar.ORACLE_TITULOS_QUERY,
        contas_pagar.ORACLE_TITULOS_TODOS_QUERY,
    ):
        sql = consulta.text
        assert 'dbamv.historico_prorrogacao' in sql
        assert "l.ds_coluna = 'VL_DUPLICATA'" in sql
        assert 'hv.vencimento_original < TRUNC(hv.data_acordo)' in sql
        assert (
            'LEFT JOIN acordos a ON a.cd_itcon_pag = v.codigo_parcela_pk'
            in sql
        )
    assert 'AS valor_em_acordo' in contas_pagar.ORACLE_FORNECEDORES_QUERY.text
    assert 'p.vencimento_original,\n       p.data_acordo' in (
        contas_pagar.ORACLE_TITULOS_QUERY.text
    )
