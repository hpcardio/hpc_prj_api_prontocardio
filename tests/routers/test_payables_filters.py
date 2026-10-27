# ruff: noqa: PLR2004
from copy import deepcopy
from datetime import date, timedelta
from decimal import Decimal

import pytest
from fastapi import HTTPException

from app_prontocardio.routers import contas_pagar as cp
from tests.routers.test_contas_pagar import FakeSession


@pytest.fixture
def data(monkeypatch):
    today = date(2026, 10, 27)
    monkeypatch.setattr(cp, '_hoje_hospital', lambda: today)
    titles = []
    for parcel, code, days, value in [
        (1, 10, 130, '20'),
        (2, 10, 15, '100'),
        (3, 20, 15, '900'),
        (4, 30, 130, '1000'),
        (5, 'sem-cadastro-bm9tZTpJU1M', 15, '600'),
    ]:
        titles.append(
            dict(
                codigo_fornecedor=code,
                nome_fornecedor=f'Fornecedor {code}',
                codigo_parcela=parcel,
                codigo_contas_pagar=parcel,
                data_vencimento=today - timedelta(days=days),
                data_emissao=today,
                data_lancamento=today,
                valor_total=Decimal(value),
                valor_honrado_oracle=Decimal('0'),
                sem_cadastro=isinstance(code, str),
            )
        )
    groups = cp._agrupar_fornecedores(titles, {})
    monkeypatch.setattr(
        cp, '_consultar_oracle', lambda *a, **k: deepcopy(groups)
    )
    monkeypatch.setattr(
        cp,
        '_consultar_titulos_oracle',
        lambda s, codes, **kw: [
            deepcopy(t) for t in titles if t['codigo_fornecedor'] in codes
        ],
    )
    monkeypatch.setattr(cp, '_pagamentos_manuais', lambda s: {})
    monkeypatch.setattr(
        cp,
        '_tratamentos',
        lambda s: {
            10: {'critico': True, 'nivel_criticidade': 5},
            20: {'critico': True, 'nivel_criticidade': 4},
        },
    )
    monkeypatch.setattr(cp, '_registrar_snapshot', lambda *a: None)
    monkeypatch.setattr(cp, '_historico', lambda s: [])
    monkeypatch.setattr(
        cp, '_valor_vencido_inicio_acompanhamento', lambda s: None
    )
    return groups


def listing(**kwargs):
    return cp.listar_contas_pagar(
        None,
        FakeSession([]),
        None,
        **{
            'page': 1,
            'page_size': 20,
            'exportar': True,
            **kwargs,
        },
    )


def test_exact_accumulated_selection_without_duplicates(data):
    result = listing(fornecedores_ids='10,20,10')
    assert [g['codigo_fornecedor'] for g in result['fornecedores']] == [10, 20]
    assert listing(fornecedores_ids='999')['total'] == 0
    result = listing(fornecedores_ids='20,sem-cadastro-bm9tZTpJU1M')
    assert {str(g['codigo_fornecedor']) for g in result['fornecedores']} == {
        '20',
        'sem-cadastro-bm9tZTpJU1M',
    }


def test_order_by_largest_overdue_before_pagination_and_export(data):
    result = listing(
        ordenar='maior_valor_vencido', exportar=False, page_size=2
    )
    assert [g['codigo_fornecedor'] for g in result['fornecedores']] == [30, 20]
    assert result['total_pages'] == 2
    exported = listing(ordenar='maior_valor_vencido')
    assert [g['valor_total_vencido'] for g in exported['fornecedores']] == [
        '1000.00',
        '900.00',
        '600.00',
        '120.00',
    ]


def test_quadrant_matches_title_age_not_largest_supplier_age(data):
    result = listing(fornecedores_ids='10,20', quadrante=5)
    assert result['total'] == 1
    assert [
        t['codigo_parcela'] for t in result['fornecedores'][0]['titulos']
    ] == [2]
    matrix = cp.consultar_matriz(
        None, FakeSession([]), None, fornecedores_ids='10,20', quadrante=5
    )
    assert matrix['quantidade'] == 1
    assert matrix['valor'] == '100.00'
    assert matrix['linhas'][0]['celulas'][4]['quantidade'] == 1
    assert matrix['pendentes']['quantidade'] == 0


@pytest.mark.parametrize(
    ('level', 'age'), [('4', 'todos'), ('todos', '5'), ('pendente', 'todos')]
)
def test_conflicting_quadrant_filters_have_no_results(data, level, age):
    assert (
        listing(quadrante=5, nivel_criticidade=level, faixa_atraso=age)[
            'total'
        ]
        == 0
    )
    matrix = cp.consultar_matriz(
        None,
        FakeSession([]),
        None,
        quadrante=5,
        nivel_criticidade=level,
        faixa_atraso=age,
    )
    assert matrix['quantidade'] == 0


def test_options_are_not_restricted_by_selected_suppliers_or_search(data):
    result = listing(
        fornecedores_ids='20', q='Fornecedor 20', incluir_opcoes=True
    )
    assert result['total'] == 1
    assert len(result['opcoes_fornecedores']) == 4
    assert 'opcoes_fornecedores' not in listing()


@pytest.mark.parametrize('quadrant', range(1, 26))
def test_all_quadrant_coordinates(quadrant):
    level, age, compatible = cp._resolver_quadrante('todos', 'todos', quadrant)
    assert (5 - int(level)) * 5 + (6 - int(age)) == quadrant
    assert compatible


@pytest.mark.parametrize(
    'value', ['10;DROP', '10,../20', 'sem-cadastro-<script>']
)
def test_invalid_identity_is_rejected(value):
    with pytest.raises(HTTPException) as exc:
        cp._selecionar_codigos(value)
    assert exc.value.status_code == 422
