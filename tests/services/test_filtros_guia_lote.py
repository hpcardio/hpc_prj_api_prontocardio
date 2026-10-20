# ruff: noqa: PLR2004
from decimal import Decimal as D
from unittest.mock import Mock

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app_prontocardio.models import ModelContaAtendimento
from app_prontocardio.routers import financeiro
from app_prontocardio.routers.app_glosas import (
    _aplicar_filtros_conta_atendimento,
)
from app_prontocardio.services import filtros_guia_lote as f


def test_exact_scope_intersects_guide_and_lot():
    engine = create_engine('sqlite://')
    with engine.connect() as c:
        c.exec_driver_sql("ATTACH DATABASE ':memory:' AS api_prontocardio")
        c.exec_driver_sql(
            'CREATE TABLE api_prontocardio.registros_glosa(id '
            'INT,cd_remessa INT,conta INT,cd_lancamento INT,cd_atendimento '
            'INT,guia TEXT,sn_ativo TEXT,convenio TEXT,numero_lote TEXT)'
        )
        c.exec_driver_sql(
            'CREATE TABLE '
            'api_prontocardio.registros_glosa_demonstrativo_ipm('
            'registro_glosa_id '
            'INT,id_registro TEXT)'
        )
        c.exec_driver_sql(
            'CREATE TABLE '
            'api_prontocardio.demonstrativo_conta_ipm(id_registro '
            'TEXT,numero_guia_senha TEXT,numero_lote TEXT,origem_maida '
            'TEXT)'
        )
        c.exec_driver_sql(
            'INSERT INTO api_prontocardio.registros_glosa VALUES '
            "(1,10,20,30,40,'00123','true','ISSEC',NULL),(2,11,21,31,41,'1234','true','ISSEC','50'),(3,12,22,32,42,'00123','true','OUTRO','50'),(4,13,23,33,43,'00123','false','ISSEC','50')"
        )
        c.exec_driver_sql(
            'INSERT INTO api_prontocardio.registros_glosa_demonstrativo_ipm '
            "VALUES (1,'a')"
        )
        c.exec_driver_sql(
            'INSERT INTO api_prontocardio.demonstrativo_conta_ipm VALUES '
            "('a','00123','50','lotes')"
        )
        with Session(c) as s:
            assert [
                r['id'] for r in f.registros_no_escopo(s, '00123', '50')
            ] == [1]
            assert len(f.registros_no_escopo(s, lote='50')) == 2
            assert f.registros_no_escopo(s, '123', '50') == []
            assert f.registros_no_escopo(s, lote="50' OR 1=1 --") == []


def test_filters_before_totals_and_pagination_preserving_treatment(
    monkeypatch,
):
    scope = [
        dict(id=n, cd_remessa=n, cd_atendimento=1, conta=2, cd_lancamento=3)
        for n in (1, 2)
    ]
    monkeypatch.setattr(f, 'registros_no_escopo', lambda *a: scope)
    record = Mock(id=1, valor_recursado=D('2'))

    def query(**params):
        n = params['cd_remessa']
        item = dict(
            cd_remessa=n,
            cd_atendimento=1,
            cd_reg=2,
            cd_lancamento=3,
            vl_total_conta=10,
            valor_glosa=5,
            valor_total_tratado=2,
            registro_recusa=record,
        )
        extra = {
            **item,
            'cd_lancamento': 999,
            'registro_recusa': None,
            'valor_glosa': 100,
        }
        card = dict(
            cd_remessa=n,
            processo={'numero_processo': str(n)},
            pacientes=[dict(codigo_paciente=1, itens=[item, extra])],
        )
        return dict(cards=[card], total=1)

    result = f.consultar_filtrado(
        None,
        query,
        dict(limit=1, offset=1, agrupar_por_processo=True),
        'g',
        'l',
    )
    assert result['total'] == 2
    assert result['quantidade_glosas'] == 2
    assert result['valor_total_glosado'] == 10
    assert result['valor_total_recursado'] == 2
    assert len(result['cards']) == 1
    assert result['cards'][0]['cd_remessa'] == 2
    assert len(result['cards'][0]['pacientes'][0]['itens']) == 1
    assert (
        result['cards'][0]['pacientes'][0]['itens'][0]['registro_recusa']
        is record
    )


def test_empty_scope_never_runs_oracle(monkeypatch):
    monkeypatch.setattr(f, 'registros_no_escopo', lambda *a: [])
    query = Mock()
    result = f.consultar_filtrado(
        None,
        query,
        dict(limit=10, offset=0, agrupar_por_processo=True),
        '',
        '999',
    )
    assert result['total'] == 0
    assert result['cards'] == []
    query.assert_not_called()


def test_triagem_lot_is_applied_before_pagination():

    query = _aplicar_filtros_conta_atendimento(
        select(ModelContaAtendimento), {'escopo_lote': [(10, 20, 30, '0040')]}
    )
    assert '0040' in query.compile().params.values()
    empty = _aplicar_filtros_conta_atendimento(
        select(ModelContaAtendimento), {'escopo_lote': []}
    )
    assert 'false' in str(empty)


def test_does_not_include_other_lot_on_same_oracle_item():
    scope = [
        dict(
            id=1, cd_remessa=10, cd_atendimento=20, conta=30, cd_lancamento=40
        )
    ]
    item = dict(
        cd_remessa=10,
        cd_atendimento=20,
        cd_reg=30,
        cd_lancamento=40,
        registro_glosa={'id': 2},
    )
    assert f.filtrar_cards([{'pacientes': [{'itens': [item]}]}], scope) == []


def test_endpoint_dispatches_filters_without_recursive_parameters(monkeypatch):

    def filtered(session, query, params, guia, lote):
        assert guia == '00123'
        assert lote == '30700'
        assert 'nr_guia' not in params
        assert 'numero_lote' not in params
        assert 'consultar_filtrado' not in params
        assert params['limit'] == 10
        assert params['offset'] == 0
        return {'filtered': True}

    monkeypatch.setattr(f, 'consultar_filtrado', filtered)
    result = financeiro.consultar_follow_up_glosas(
        None,
        Mock(),
        session_oracle=Mock(),
        nr_guia=' 00123 ',
        numero_lote=' 30700 ',
        limit=10,
        offset=0,
    )
    assert result == {'filtered': True}
