# ruff: noqa: PLR2004
from datetime import date, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app_prontocardio.payables_priority import (
    age_level,
    allocate_budget,
    build_matrix,
    classification,
)
from app_prontocardio.routers import contas_pagar as api


@pytest.mark.parametrize(
    ('days', 'expected'),
    [
        (-1, 0),
        (0, 0),
        (1, 1),
        (30, 1),
        (31, 2),
        (60, 2),
        (61, 3),
        (90, 3),
        (91, 4),
        (120, 4),
        (121, 5),
        (1671, 5),
    ],
)
def test_age_boundaries(days, expected):
    assert age_level(days) == expected


def test_institutional_budget_example():
    result = allocate_budget(Decimal('350000'))
    assert result[5, 5] == Decimal('42000')
    assert result[4, 4] == Decimal('26250')
    assert result[1, 1] == Decimal('1050')
    assert [
        sum(result[c, a] for a in range(1, 6)) for c in range(5, 0, -1)
    ] == [140000, 105000, 70000, 24500, 10500]
    assert [
        sum(result[c, a] for c in range(1, 6)) for a in range(5, 0, -1)
    ] == [105000, 87500, 70000, 52500, 35000]


@pytest.mark.parametrize(
    'amount', ['0', '0.01', '0.07', '10.13', '350000.01', '9999999999999.99']
)
def test_distribution_conserves_cents(amount):
    values = allocate_budget(Decimal(amount))
    assert sum(values.values()) == Decimal(amount)
    assert all(
        value >= 0 and value == value.quantize(Decimal('.01'))
        for value in values.values()
    )


def test_legacy_critical_supplier_is_not_silently_reclassified():
    assert classification({'critico': True})['nivel_criticidade'] is None
    assert classification({'critico': False})['nivel_criticidade'] == 1
    assert (
        classification({'critico': True, 'nivel_criticidade': 3})[
            'nivel_criticidade'
        ]
        == 3
    )


def title(days, balance):
    return {'dias_vencidos': days, 'saldo_a_pagar': Decimal(balance)}


def test_matrix_quadrant_colors_match_institutional_reference():
    result = build_matrix([], Decimal('0'))
    actual = {}
    for row in result['linhas']:
        for cell in row['celulas']:
            actual.setdefault(cell['cor'], []).append(cell['quadrante'])
    assert actual == {
        'red': [1, 2, 3, 4, 5, 6],
        'yellow': [7, 8, 9, 11],
        'blue': [12, 13, 14],
        'green': [10, 16, 17, 18, 19],
        'neutral': [15, 20, 21, 22, 23, 24, 25],
    }


def test_matrix_counts_titles_by_own_age_and_excludes_settled_today_future():
    result = build_matrix(
        [
            {
                'nivel_criticidade': 5,
                'titulos': [
                    title(130, '100'),
                    title(12, '40'),
                    title(0, '30'),
                    title(80, '0'),
                ],
            },
            {'nivel_criticidade': None, 'titulos': [title(45, '25')]},
        ],
        Decimal('350000'),
    )
    assert result['quantidade'] == 2
    assert result['valor'] == Decimal('140')
    assert result['linhas'][0]['celulas'][0]['quantidade'] == 1
    assert result['linhas'][0]['celulas'][4]['quantidade'] == 1
    assert result['pendentes'] == {'quantidade': 1, 'valor': Decimal('25')}
    assert result['cobertura'] == Decimal('140')
    assert result['sobra'] == Decimal('349860')


@pytest.mark.parametrize(
    'payload',
    [
        {},
        {'nivel_criticidade': 0, 'justificativa': 'risco'},
        {'nivel_criticidade': 6, 'justificativa': 'risco'},
        {'nivel_criticidade': 4, 'justificativa': '   '},
        {'nivel_criticidade': True, 'justificativa': 'risco'},
    ],
)
def test_invalid_classifications_are_rejected(payload):
    with pytest.raises(ValidationError):
        api.CriticidadeFornecedorInput(**payload)


def test_new_classification_is_audited_without_overwriting_payment():
    payload = api.CriticidadeFornecedorInput(
        nivel_criticidade=4, justificativa='  OPME essencial  '
    )
    session = Mock()
    api.atualizar_criticidade_fornecedor(
        10, payload, SimpleNamespace(id=7), session
    )
    statement, params = session.execute.call_args.args
    assert params['nivel'] == 4
    assert params['critico'] is True
    assert params['justificativa'] == 'OPME essencial'
    assert 'pagamento_imediato =' not in str(statement)
    assert 'usuario_criticidade_id' in str(statement)
    session.commit.assert_called_once()


def setup_titles(monkeypatch):
    today = date(2026, 10, 26)
    monkeypatch.setattr(api, '_hoje_hospital', lambda: today)
    raw = [
        {
            'codigo_fornecedor': 10,
            'nome_fornecedor': 'Fornecedor A',
            'codigo_parcela': code,
            'valor_total': Decimal('100'),
            'valor_honrado_oracle': Decimal('0'),
            'data_vencimento': today - timedelta(days=days),
            'data_lancamento': None,
            'data_emissao': None,
        }
        for code, days in [(1, 130), (2, 15), (3, 0)]
    ]
    monkeypatch.setattr(
        api,
        '_consultar_oracle',
        lambda *args, **kwargs: api._agrupar_fornecedores(raw, {}),
    )
    monkeypatch.setattr(
        api,
        '_consultar_titulos_oracle',
        lambda *args, **kwargs: raw if args[1] else [],
    )
    monkeypatch.setattr(api, '_pagamentos_manuais', lambda session: {})
    monkeypatch.setattr(
        api,
        '_tratamentos',
        lambda session: {10: {'critico': True, 'nivel_criticidade': 4}},
    )
    monkeypatch.setattr(api, '_historico', lambda session: [])
    monkeypatch.setattr(
        api, '_valor_vencido_inicio_acompanhamento', lambda session: None
    )


def test_drilldown_and_export_select_title_age_not_supplier_oldest(
    monkeypatch,
):
    setup_titles(monkeypatch)
    result = api.listar_contas_pagar(
        None,
        Mock(),
        Mock(),
        page=1,
        page_size=20,
        nivel_criticidade='4',
        faixa_atraso='1',
        somente_vencidos=True,
        exportar=True,
    )
    assert result['total'] == 1
    assert [
        t['codigo_parcela'] for t in result['fornecedores'][0]['titulos']
    ] == [2]
    assert result['fornecedores'][0]['dias_atraso'] == 130
    assert result['fornecedores'][0]['valor_total_vencido'] == '200.00'


def test_budget_and_matrix_http_contract_validation_and_auth(monkeypatch):
    setup_titles(monkeypatch)
    app = FastAPI()
    app.include_router(api.router)
    session = Mock()
    session.execute.return_value.mappings.return_value.all.return_value = []
    app.dependency_overrides[api.get_session_postgres] = lambda: session
    oracle_session = Mock()
    app.dependency_overrides[api.get_session_oracle] = lambda: oracle_session
    client = TestClient(app)
    root = '/app_glosas/financeiro/contas-a-pagar'
    assert client.get(root + '/matriz').status_code in (401, 403)
    app.dependency_overrides[api.valida_token_usuario_atual] = lambda: (
        SimpleNamespace(id=7)
    )
    assert (
        client.put(
            root + '/orcamento/2026-10-01', json={'valor': '-1'}
        ).status_code
        == 422
    )
    assert (
        client.put(
            root + '/orcamento/2026-10-01', json={'valor': '1.001'}
        ).status_code
        == 422
    )
    assert (
        client.put(
            root + '/orcamento/2026-10-02', json={'valor': '350000'}
        ).status_code
        == 422
    )
    assert (
        client.put(
            root + '/orcamento/2026-10-01', json={'valor': '350000.01'}
        ).status_code
        == 200
    )
    session.commit.assert_called_once()
    result = client.get(root + '/matriz?competencia=2026-10-01').json()
    assert len(result['linhas']) == 5
    assert result['quantidade'] == 2
    assert Decimal(str(result['valor'])) == Decimal('200')
    assert result['orcamento_cadastrado'] is False


def test_priority_orders_level_then_age_and_preserves_legacy_top():
    items = [
        {
            'critico': True,
            'nivel_criticidade': 3,
            'dias_atraso': 400,
            'valor_total_vencido': 100,
        },
        {
            'critico': True,
            'nivel_criticidade': 5,
            'dias_atraso': 5,
            'valor_total_vencido': 100,
        },
        {
            'critico': True,
            'nivel_criticidade': None,
            'dias_atraso': 20,
            'valor_total_vencido': 100,
        },
    ]
    assert sorted(items, key=api._chave_prioridade_fornecedor) == [
        items[2],
        items[1],
        items[0],
    ]


def test_matrix_filters_and_manual_payments_reconcile(monkeypatch):
    setup_titles(monkeypatch)
    monkeypatch.setattr(
        api,
        '_pagamentos_manuais',
        lambda session: {
            1: [{'valor_pago': Decimal('25')}],
            2: [{'valor_pago': Decimal('100')}],
        },
    )
    session = Mock()
    session.execute.return_value.mappings.return_value.all.return_value = [
        {'valor': Decimal('350000.01'), 'usuario_nome': 'Financeiro'}
    ]
    result = api.consultar_matriz(
        None, session, Mock(), nivel_criticidade='4', faixa_atraso='5'
    )
    assert result['quantidade'] == 1
    assert result['valor'] == '75.00'
    assert result['orcamento'] == '350000.01'
    empty = api.consultar_matriz(None, session, Mock(), nivel_criticidade='5')
    assert empty['quantidade'] == 0
    assert empty['valor'] == '0.00'
    assert empty['orcamento'] == '350000.01'
