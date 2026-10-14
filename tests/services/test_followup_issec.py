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
    wrong['competencia'] = date(2025, 12, 1)
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
