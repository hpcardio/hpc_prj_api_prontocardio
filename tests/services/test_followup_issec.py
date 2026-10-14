from datetime import date
from decimal import Decimal
from unittest.mock import Mock

from app_prontocardio.services.followup_issec import match_rows, portal_cards


def portal(number='2600003764'):
    return dict(
        processo=number,
        competencia='01/2026',
        mes_producao=date(2025, 12, 1),
        valor_cobrado=7930,
        valor_total_itens_glosados=190,
    )


def total(remessa=16437):
    return dict(cd_remessa=remessa, competencia=date(2026, 1, 1), total=7930)


def sessions(rows=None, totals=None):
    pg, mv = Mock(), Mock()
    pg.execute.return_value.mappings.return_value = rows or [portal()]
    mv.execute.return_value.mappings.return_value = totals or [total()]
    return pg, mv


def test_matches_entry_month_and_remittance_total():
    assert list(match_rows([portal()], [total()])) == [(portal(), 16437)]
    wrong = total()
    wrong['competencia'] = date(2025, 11, 1)
    assert list(match_rows([portal()], [wrong])) == []


def test_ambiguous_process_or_remittance_never_auto_links():
    assert list(match_rows([portal(), portal('other')], [total()])) == []
    assert list(match_rows([portal()], [total(), total(16438)])) == []


def test_card_uses_portal_glosa_without_fabricating_items():
    pg, mv = sessions()
    cards = portal_cards(pg, mv, set(), cd_remessa=16437)
    assert len(cards) == 1
    assert cards[0]['numero_protocolo'] == '2600003764'
    assert cards[0]['valor_glosado'] == Decimal('190.00')
    assert cards[0]['valor_remessa'] == Decimal('7930.00')
    assert cards[0]['pacientes'] == []
    assert cards[0]['valor_total_tratado'] == 0
    pg.commit.assert_not_called()
    mv.commit.assert_not_called()


def test_existing_process_is_not_counted_twice():
    pg, mv = sessions()
    assert portal_cards(pg, mv, {'2600003764'}) == []


def test_filters_preserve_ambiguity_and_scope():
    for filters in [
        dict(processo='wrong'),
        dict(protocolo='wrong'),
        dict(cd_remessa=999),
        dict(q='wrong'),
        dict(processos_permitidos=set()),
        dict(remessas_permitidas=set()),
    ]:
        pg, mv = sessions()
        assert portal_cards(pg, mv, set(), **filters) == []
    pg, mv = sessions([portal(), portal('other')])
    assert portal_cards(pg, mv, set(), processo='2600003764') == []


def test_other_insurers_and_unsupported_filters_do_not_query():
    for filters in [dict(convenio='IPM'), dict(unsupported=True)]:
        pg, mv = sessions()
        assert portal_cards(pg, mv, set(), **filters) == []
        pg.execute.assert_not_called()
        mv.execute.assert_not_called()


def test_process_and_protocol_find_same_card():
    for field in ['processo', 'protocolo', 'q']:
        pg, mv = sessions()
        assert len(portal_cards(pg, mv, set(), **{field: '2600003764'})) == 1


def test_response_schema_and_existing_treatment():
    from app_prontocardio.schema import CardFollowUpGlosaPublic

    pg, mv = sessions()
    card = portal_cards(pg, mv, set())[0]
    assert CardFollowUpGlosaPublic.model_validate(card).cd_remessa == 16437
    row = portal()
    row['tratado'] = True
    pg, mv = sessions([row])
    assert portal_cards(pg, mv, set()) == []


def test_production_month_also_matches_and_same_month_does_not_duplicate():
    row = portal()
    production = dict(total(), competencia=date(2025, 12, 15))
    assert list(match_rows([row], [production])) == [(row, 16437)]
    row['competencia'] = '12/2025'
    assert list(match_rows([row], [production])) == [(row, 16437)]


def test_two_months_cannot_choose_between_different_remittances():
    production = dict(total(16438), competencia=date(2025, 12, 1))
    assert list(match_rows([portal()], [total(), production])) == []


def test_cross_month_process_collision_is_ambiguous():
    other = dict(
        portal('other'), competencia='02/2026', mes_producao=date(2026, 1, 1)
    )
    assert list(match_rows([portal(), other], [total()])) == []


def test_missing_entry_uses_production_and_query_includes_both_months():
    row = dict(portal(), competencia=None, mes_producao=date(2026, 1, 10))
    pg, mv = sessions([row])
    assert len(portal_cards(pg, mv, set())) == 1
    pg, mv = sessions()
    portal_cards(pg, mv, set())
    assert mv.execute.call_args.args[1]['inicio'] == date(2025, 12, 1)
    assert mv.execute.call_args.args[1]['fim'] == date(2026, 2, 1)


def test_production_match_still_requires_same_total():
    production = dict(total(), competencia=date(2025, 12, 1), total=7931)
    assert list(match_rows([portal()], [production])) == []


def test_existing_card_accepts_production_month(monkeypatch):
    from app_prontocardio.routers import financeiro

    pg, mv = Mock(), Mock()
    row = dict(portal(), competencia='02/2026', mes_producao=date(2026, 1, 1))
    pg.execute.return_value.mappings.return_value = [row]
    mv.execute.return_value.mappings.return_value = [
        dict(
            cd_remessa=16437,
            cd_reg=1,
            dt_competencia=date(2026, 1, 1),
            vl_total_registro=7930,
        )
    ]
    monkeypatch.setattr(
        financeiro, '_tabela_schema_existe', lambda *args: True
    )
    card = dict(
        convenio='ISSEC', cd_remessa=16437, processo=dict(numero_processo='')
    )
    financeiro._aplicar_processos_portal_issec(pg, mv, [card])
    assert card['processo']['numero_processo'] == '2600003764'
    assert "TO_CHAR(mes_producao, 'MM/YYYY')" in str(
        pg.execute.call_args.args[0]
    )
