from copy import deepcopy
from unittest.mock import Mock

import pytest

from app_prontocardio.routers import financeiro


def card(process='', insurer='ISSEC', remittance=17451):
    return dict(
        cd_remessa=remittance,
        convenio=insurer,
        processo=dict(numero_processo=process),
        pacientes=[dict(itens=[dict(valor_glosa=580.95)]) for _ in range(3)],
    )


def setup(monkeypatch, initial=None, candidates=None):
    loader = Mock(side_effect=[initial or [], candidates or []])
    monkeypatch.setattr(financeiro, '_cards_cogestao_follow_up', loader)
    monkeypatch.setattr(
        financeiro, '_cards_registros_glosa_follow_up', Mock(return_value=[])
    )

    def resolve(pg, mv, cards):
        for item in cards:
            if (
                item['convenio'] == 'ISSEC'
                and not item['processo']['numero_processo']
            ):
                item['processo']['numero_processo'] = '2600011399'

    monkeypatch.setattr(financeiro, '_aplicar_processos_portal_issec', resolve)
    return loader


@pytest.mark.parametrize('details', [True, False])
def test_process_filter_retains_three_maida_items(monkeypatch, details):
    loader = setup(monkeypatch, candidates=[card()])
    result = financeiro._cards_cogestao_com_processo_issec(
        None,
        None,
        set(),
        processo_original='2600011399',
        cd_remessa=17451,
        incluir_detalhes=details,
        paciente='Filtro',
    )
    assert len(result) == 1
    assert result[0]['processo']['numero_processo'] == '2600011399'
    assert sum(len(p['itens']) for p in result[0]['pacientes']) == 3
    assert loader.call_args.kwargs == dict(
        processo_original=None,
        cd_remessa=17451,
        incluir_detalhes=details,
        paciente='Filtro',
        convenio='ISSEC',
    )


def test_wrong_process_and_other_insurers_are_not_returned(monkeypatch):
    setup(monkeypatch, candidates=[card(), card('wrong', 'IPM')])
    assert (
        financeiro._cards_cogestao_com_processo_issec(
            None, None, set(), processo_original='wrong'
        )
        == []
    )


def test_ambiguous_unresolved_process_is_excluded(monkeypatch):
    setup(monkeypatch, candidates=[card()])
    monkeypatch.setattr(
        financeiro, '_aplicar_processos_portal_issec', lambda *args: None
    )
    assert (
        financeiro._cards_cogestao_com_processo_issec(
            None, None, set(), processo_original='2600011399'
        )
        == []
    )


def test_existing_card_is_not_duplicated(monkeypatch):
    existing = card('2600011399')
    setup(monkeypatch, initial=[existing], candidates=[deepcopy(existing)])
    assert financeiro._cards_cogestao_com_processo_issec(
        None, None, set(), processo_original='2600011399'
    ) == [existing]


@pytest.mark.parametrize(
    'filters',
    [
        dict(processo_original=None),
        dict(processo_original='123', convenio='IPM'),
    ],
)
def test_unrelated_queries_preserve_original_path(monkeypatch, filters):
    loader = setup(monkeypatch)
    financeiro._cards_cogestao_com_processo_issec(None, None, set(), **filters)
    loader.assert_called_once()


def test_registered_glosas_resolve_process_before_filter(monkeypatch):
    setup(monkeypatch)
    registros = Mock(return_value=[card()])
    monkeypatch.setattr(
        financeiro, '_cards_registros_glosa_follow_up', registros
    )
    result = financeiro._cards_cogestao_com_processo_issec(
        None,
        None,
        set(),
        processo_original='2600011399',
        cd_remessa=17451,
        paciente='Filtro',
        incluir_detalhes=True,
    )
    assert len(result) == 1
    assert result[0]['processo']['numero_processo'] == '2600011399'
    assert sum(len(p['itens']) for p in result[0]['pacientes']) == 3
    assert registros.call_args.kwargs['processo_original'] is None
    assert registros.call_args.kwargs['cd_remessa'] == 17451
    assert registros.call_args.kwargs['paciente'] == 'Filtro'
    assert registros.call_args.kwargs['convenio'] == 'ISSEC'


def test_registered_glosas_do_not_duplicate_resolved_cogestao(monkeypatch):
    setup(monkeypatch, candidates=[card()])
    monkeypatch.setattr(
        financeiro,
        '_cards_registros_glosa_follow_up',
        Mock(return_value=[card()]),
    )
    result = financeiro._cards_cogestao_com_processo_issec(
        None,
        None,
        set(),
        processo_original='2600011399',
        cd_remessa=17451,
    )
    assert len(result) == 1
