from decimal import Decimal

import pytest

from app_prontocardio.services.maida_followup import match_maida_items


def item(**changes):
    return dict(
        cd_remessa=16437,
        cd_reg=1,
        cd_lancamento=1,
        nm_convenio='ISSEC',
        nr_guia='123',
        cd_pro_fat='10',
        cd_tuss='20',
        **changes,
    )


def demo(**changes):
    data = dict(
        id_registro='1',
        origem_maida='maida',
        valor_glosa=Decimal('190'),
        numero_guia_senha='123',
        codigo_servico='20',
    )
    data.update(changes)
    return data


def test_requires_guide_and_procedure_together():
    assert len(list(match_maida_items([demo()], [item()]))) == 1
    assert (
        list(match_maida_items([demo(numero_guia_senha='other')], [item()]))
        == []
    )
    assert (
        list(match_maida_items([demo(codigo_servico='other')], [item()])) == []
    )


def test_supports_mv_and_tuss_codes_without_duplicate():
    for code in ['10', '20']:
        assert (
            len(
                list(
                    match_maida_items(
                        [demo(codigo_servico=code)] * 2, [item()] * 2
                    )
                )
            )
            == 1
        )


def test_ambiguous_launches_not_chosen_arbitrarily():
    other = item()
    other['cd_lancamento'] = 2
    assert list(match_maida_items([demo()], [item(), other])) == []


def test_preserves_separate_glosa_lines_and_amounts():
    result = list(match_maida_items([demo(), demo(id_registro='2')], [item()]))
    assert len(result) == 2
    assert sum(d['valor_glosa'] for d, _ in result) == 380


def test_excludes_other_sources_and_insurers_and_paid_items():
    for d in [demo(origem_maida=None), demo(valor_glosa=0)]:
        assert list(match_maida_items([d], [item()])) == []
    other = item()
    other['nm_convenio'] = 'IPM'
    assert list(match_maida_items([demo()], [other])) == []


@pytest.mark.parametrize('use_brasindice', [False, True])
def test_detail_groups_patients_and_preserves_portal_totals(monkeypatch, use_brasindice):
    from datetime import datetime
    from unittest.mock import Mock
    from app_prontocardio.routers import financeiro
    from app_prontocardio.schema import CardFollowUpGlosaPublic
    from app_prontocardio.services.followup_issec import portal_cards

    pg, mv = Mock(), Mock()
    pg.execute.return_value.mappings.return_value = [
        dict(
            processo='2600003764',
            competencia='01/2026',
            valor_cobrado=7930,
            valor_total_itens_glosados=190,
        )
    ]
    mv.execute.return_value.mappings.return_value = [
        dict(
            cd_remessa=16437,
            competencia=datetime(2026, 1, 1),
            total=7930,
        )
    ]
    card = portal_cards(pg, mv, set())[0]
    origin = item()
    origin.update(
        cd_paciente=10,
        nm_paciente='Teste',
        cd_atendimento=20,
        dt_atendimento=datetime(2026, 1, 10),
        vl_total_conta=200,
        qt_lancamento=1,
    )
    line = demo(codigo_servico='0000078358') if use_brasindice else demo()
    if use_brasindice:
        origin.update(cd_pro_fat='90425405', cd_tuss='0000091421')
    line.update(valor_processado=200, valor_liberado=10)
    pg.execute.return_value.mappings.return_value = [line]
    pg.scalars.return_value = []
    monkeypatch.setattr(financeiro, '_tabela_ipm_existe', lambda *a: True)
    monkeypatch.setattr(
        financeiro,
        '_itens_oracle_remessas_ipm',
        lambda *a, **kw: ({}, {16437: [origin]}),
    )
    monkeypatch.setattr(
        'app_prontocardio.services.maida_followup.load_brasindice',
        lambda session: {'78358': '90425405'} if use_brasindice else {},
    )
    financeiro._detalhar_cards_maida_issec(pg, mv, [card])
    validated = CardFollowUpGlosaPublic.model_validate(card)
    assert validated.valor_itens == 7930
    assert validated.valor_glosado == 190
    assert len(validated.pacientes) == 1
    assert validated.pacientes[0].itens[0].valor_glosa == 190
    assert validated.pacientes[0].itens[0].nr_guia == '123'
    pg.commit.assert_not_called()


def test_brasindice_matches_medication_preserving_guide_and_value():
    origin = item()
    origin.update(cd_pro_fat='90425405', cd_tuss='0000091421')
    line = demo(codigo_servico='0000078358', valor_glosa=Decimal('0.88'))
    mapping = {'78358': '90425405'}
    assert list(match_maida_items([line], [origin], mapping)) == [(line, origin)]
    assert list(match_maida_items([line], [origin])) == []
    assert list(match_maida_items(
        [demo(codigo_servico='0000078358', numero_guia_senha='other')],
        [origin], mapping,
    )) == []
    origin['nm_convenio'] = 'IPM'
    assert list(match_maida_items([line], [origin], mapping)) == []


def test_brasindice_does_not_pick_between_direct_and_catalog_candidates():
    direct = item()
    direct['cd_tuss'] = '0000078358'
    mapped = item()
    mapped.update(cd_lancamento=2, cd_pro_fat='90425405')
    assert list(match_maida_items(
        [demo(codigo_servico='0000078358')], [direct, mapped],
        {'78358': '90425405'},
    )) == []


def test_brasindice_duplicate_catalog_and_direct_match_is_one_item():
    origin = item()
    origin.update(cd_pro_fat='90425405', cd_tuss='0000078358')
    assert len(list(match_maida_items(
        [demo(codigo_servico='0000078358')], [origin, origin],
        {'78358': '90425405'},
    ))) == 1


def test_catalog_absent_preserves_legacy_matching():
    from unittest.mock import Mock
    from app_prontocardio.services.maida_followup import load_brasindice
    session = Mock()
    session.execute.return_value.scalar.return_value = None
    assert load_brasindice(session) == {}
    assert session.execute.call_count == 1


def test_catalog_loaded_with_normalized_codes():
    from unittest.mock import Mock
    from app_prontocardio.services.maida_followup import load_brasindice
    session = Mock()
    session.execute.return_value.scalar.return_value = 'table'
    session.execute.return_value.mappings.return_value = [
        {'cd_tiss': '0000078358', 'cd_pro_fat': '90425405'},
    ]
    assert load_brasindice(session) == {'78358': '90425405'}
