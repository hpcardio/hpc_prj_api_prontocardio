from copy import deepcopy
from decimal import Decimal
import pytest
from app_prontocardio.services.followup_identity import enrich_cards


def data():
    items = [
        dict(
            nr_guia='435190',
            cd_pro_fat=code,
            cd_tuss=code,
            cd_paciente=0,
            cd_atendimento=0,
            nm_paciente='Guia 435190',
            cd_reg=999,
            cd_lancamento=i,
            valor_glosa=value,
            vl_total_conta=value * 2,
            valor_total_tratado=Decimal('0'),
            registro_glosa={'id': i},
        )
        for i, (code, value) in enumerate([
            ('78296250', Decimal('0.36')),
            ('70905096', Decimal('2.57')),
        ])
    ]
    card = dict(
        cd_remessa=17980,
        convenio='ISSEC',
        valor_glosado=Decimal('2.93'),
        pacientes=[
            dict(codigo_paciente=0, nm_paciente='Guia 435190', itens=items)
        ],
    )
    origins = [
        dict(
            nr_guia='435190',
            cd_pro_fat='78347335',
            cd_tuss='78296250',
            cd_paciente=123,
            cd_atendimento=456,
            nm_paciente='Paciente Teste',
        ),
        dict(
            nr_guia='435190',
            cd_pro_fat='70905096',
            cd_tuss='70905096',
            cd_paciente=123,
            cd_atendimento=456,
            nm_paciente='Paciente Teste',
        ),
    ]
    return card, origins


def test_recovers_patient_and_attendance_preserving_financial_and_treatment_data():
    card, origins = data()
    before = deepcopy(card)
    enrich_cards([card], {17980: origins})
    p = card['pacientes'][0]
    assert p['codigo_paciente'] == 123 and p['nm_paciente'] == 'Paciente Teste'
    assert p['valor_glosado'] == Decimal('2.93')
    for item, old in zip(p['itens'], before['pacientes'][0]['itens']):
        assert item['cd_atendimento'] == 456
        for key in old.keys() - {
            'cd_paciente',
            'nm_paciente',
            'cd_atendimento',
        }:
            assert item[key] == old[key]


@pytest.mark.parametrize(
    'case',
    [
        'wrong_guide',
        'wrong_code',
        'wrong_remittance',
        'ambiguous',
        'conflicting_identity',
        'other_insurer',
    ],
)
def test_does_not_invent_or_replace_conflicting_identity(case):
    card, origins = data()
    if case == 'wrong_guide':
        for origin in origins:
            origin['nr_guia'] = 'other'
    elif case == 'wrong_code':
        for origin in origins:
            origin.update(cd_pro_fat='other', cd_tuss='other')
    elif case == 'wrong_remittance':
        card['cd_remessa'] = 1
    elif case == 'ambiguous':
        origins += [dict(o, cd_atendimento=789) for o in origins]
    elif case == 'conflicting_identity':
        for item in card['pacientes'][0]['itens']:
            item['cd_paciente'] = 999
    else:
        card['convenio'] = 'IPM'
    expected = deepcopy(card['pacientes'][0]['itens'])
    enrich_cards([card], {17980: origins})
    assert card['pacientes'][0]['itens'] == expected
