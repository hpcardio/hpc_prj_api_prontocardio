# ruff: noqa: PLR2004
from copy import deepcopy
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app_prontocardio.payables_priority import build_matrix
from app_prontocardio.routers import contas_pagar as cp
from tests.routers.test_contas_pagar import FakeSession


def title(code, name, parcel, reference=None):
    return dict(
        codigo_fornecedor=code,
        nome_fornecedor=name,
        referencia_sem_cadastro=reference,
        codigo_parcela_pk=parcel,
        codigo_contas_pagar=parcel + 100,
        numero_documento='NF-1',
        descricao_conta='Teste',
        numero_parcela=1,
        data_lancamento=date(2026, 10, 1),
        data_emissao=date(2026, 10, 1),
        valor_duplicata=Decimal('100'),
        valor_nota_fiscal=Decimal('100'),
        data_vencimento=date(2026, 10, 20),
        tipo_quitacao='comprometido',
        valor_honrado_oracle=Decimal('20'),
    )


@pytest.fixture
def sample(monkeypatch):
    monkeypatch.setattr(cp, '_hoje_hospital', lambda: date(2026, 10, 27))
    rows = [
        title(10, 'CSRF', 1),
        title(None, 'CSRF', 2, 'nome:CSRF'),
        title(None, 'CSRF', 3, 'nome:CSRF'),
        title(None, 'MOESIO', 4, 'nome:MOESIO'),
        title(None, None, 5, 'conta:105'),
        title(None, None, 6, 'conta:106'),
    ]
    keys = list(dict.fromkeys(cp._identidade_fornecedor(row) for row in rows))
    titles = cp._consultar_titulos_oracle(FakeSession([rows]), keys)
    groups = cp._agrupar_fornecedores(titles, {})
    aggregates = []
    for group in groups:
        row = next(
            r
            for r in rows
            if cp._identidade_fornecedor(r) == group['codigo_fornecedor']
        )
        aggregates.append({
            **group,
            'codigo_fornecedor': row['codigo_fornecedor'],
            'referencia_sem_cadastro': row['referencia_sem_cadastro'],
        })
    return rows, keys, titles, groups, aggregates


def test_null_names_stay_separate_and_registered_identity_is_unchanged(sample):
    rows, keys, titles, groups, aggregates = sample
    assert len(keys) == len(groups) == 5
    assert keys[0] == 10
    assert groups[1]['saldo_a_pagar'] == Decimal('160')
    assert [t['codigo_parcela'] for t in groups[1]['titulos']] == [2, 3]
    assert groups[0]['sem_cadastro'] is False
    assert groups[1]['sem_cadastro'] is True
    queried = cp._consultar_oracle(FakeSession([aggregates]))
    assert [g['codigo_fornecedor'] for g in queried] == keys
    assert sum(g['valor_total_vencido'] for g in queried) == Decimal('480')


def test_bound_detail_filters_include_only_selected_null_names(sample):
    rows, keys, *_ = sample
    oracle = FakeSession([[rows[0], rows[1]]])
    cp._consultar_titulos_oracle(oracle, keys[:2])
    sql, params = oracle.executions[0]
    assert params['codigos_fornecedor'] == [10]
    assert params['referencias_sem_cadastro'] == ['nome:CSRF']
    assert (
        'v.codigo_do_fornecedor IS NOT NULL'
        not in sql.split('FROM dbamv.HPC_V_CONTAS_A_PAGAR v')[1].split(
            'GROUP BY'
        )[0]
    )
    assert 'POSTCOMPILE_referencias_sem_cadastro' in sql


def test_unregistered_values_are_pending_instead_of_noncritical(sample):
    groups = sample[3]
    cp._enriquecer_fornecedores(
        groups, {10: {'critico': True, 'nivel_criticidade': 4}}
    )
    assert groups[0]['nivel_criticidade'] == 4
    assert all(g['nivel_criticidade'] is None for g in groups[1:])
    assert all(g['criticidade_pendente'] for g in groups[1:])
    matrix = build_matrix(groups, Decimal('0'))
    assert matrix['quantidade'] == 1
    assert matrix['pendentes'] == {'quantidade': 5, 'valor': Decimal('400')}
    assert matrix['valor'] + matrix['pendentes']['valor'] == Decimal('480')


@pytest.mark.parametrize(
    ('level', 'criticality', 'expected'),
    [
        ('todos', 'todos', 5),
        ('pendente', 'todos', 4),
        ('1', 'todos', 1),
        ('todos', 'nao_criticos', 1),
    ],
)
def test_list_filters_export_totals_and_details(
    sample, monkeypatch, level, criticality, expected
):
    _, _, titles, _, aggregates = sample
    monkeypatch.setattr(cp, '_pagamentos_manuais', lambda s: {})
    monkeypatch.setattr(cp, '_tratamentos', lambda s: {})
    monkeypatch.setattr(cp, '_historico', lambda s: [])
    monkeypatch.setattr(
        cp, '_valor_vencido_inicio_acompanhamento', lambda s: None
    )
    monkeypatch.setattr(
        cp,
        '_consultar_titulos_oracle',
        lambda s, codes, **kw: [
            deepcopy(t) for t in titles if t['codigo_fornecedor'] in codes
        ],
    )
    result = cp.listar_contas_pagar(
        None,
        None,
        FakeSession([aggregates]),
        exportar=True,
        page=1,
        page_size=20,
        nivel_criticidade=level,
        criticidade=criticality,
        faixa_atraso='1',
    )
    assert result['total'] == expected
    assert result['resumo']['valor_vencido_atual'] == '480.00'
    for supplier in result['fornecedores']:
        assert supplier['titulos']
        assert all(
            t['codigo_fornecedor'] == supplier['codigo_fornecedor']
            for t in supplier['titulos']
        )


def test_synthetic_identity_cannot_be_written_as_mv_supplier(sample):
    app = FastAPI()
    app.include_router(cp.router)
    app.dependency_overrides[cp.valida_token_usuario_atual] = lambda: (
        SimpleNamespace(id=7)
    )
    session = FakeSession([])
    app.dependency_overrides[cp.get_session_postgres] = lambda: session
    key = sample[1][1]
    client = TestClient(app)
    response = client.patch(
        f'/app_glosas/financeiro/contas-a-pagar/fornecedores/{key}/criticidade',
        json={'nivel_criticidade': 4, 'justificativa': 'Teste'},
    )
    assert response.status_code == 422
    assert session.executions == []
