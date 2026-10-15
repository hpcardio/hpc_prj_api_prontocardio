from copy import deepcopy
from decimal import Decimal
from app_prontocardio.services.issec_card_merge import merge_issec_cards


def card(treated=False, medication=False):
    items = [
        dict(
            cd_reg=n,
            cd_lancamento=11,
            nr_guia=str(n),
            cd_pro_fat='41001230',
            dt_lancamento='2026-08-15',
            valor_glosa=Decimal('580.95'),
            vl_total_conta=Decimal('580.95'),
            valor_total_tratado=Decimal('580.95') if treated else Decimal(0),
            registro_glosa_id=n if treated else n + 10,
        )
        for n in (1, 2)
    ]
    if medication:
        items.append(
            dict(
                cd_reg=3,
                cd_lancamento=4,
                nr_guia='3',
                cd_pro_fat='90425405',
                dt_lancamento='2026-08-15',
                valor_glosa=Decimal('0.88'),
                vl_total_conta=Decimal('0.88'),
                valor_total_tratado=Decimal(0),
            )
        )
    return dict(
        convenio='ISSEC',
        cd_remessa=19877,
        processo={'numero_processo': '2600033060'},
        possui_recurso=treated,
        pacientes=[dict(codigo_paciente=1, nm_paciente='Teste', itens=items)],
    )


def test_retains_treatments_and_adds_missing_medication_in_both_orders():
    for cards in [
        [card(True), card(medication=True)],
        [card(medication=True), card(True)],
    ]:
        before = deepcopy(cards)
        result = merge_issec_cards(cards)
        assert cards == before
        assert len(result) == 1
        c = result[0]
        assert len(c['pacientes'][0]['itens']) == 3
        assert c['valor_glosado'] == Decimal('1162.78')
        assert c['valor_total_tratado'] == Decimal('1161.90')
        assert c['valor_glosa_pendente'] == Decimal('0.88')
        assert c['pacientes'][0]['itens'][0]['registro_glosa_id'] == 1


def test_other_process_remittance_insurer_and_unknown_identity_are_unchanged():
    for field, value in [
        ('cd_remessa', 2),
        ('convenio', 'IPM'),
        ('processo', {'numero_processo': 'other'}),
    ]:
        a, b = card(True), card(medication=True)
        b[field] = value
        assert merge_issec_cards([a, b]) == [a, b]
    a, b = card(True), card(medication=True)
    b['pacientes'][0]['itens'][0]['cd_reg'] = None
    assert merge_issec_cards([a, b]) == [a, b]


def test_does_not_guess_between_two_treatments_or_repeated_lines():
    a, b = card(True), card(True, True)
    b['pacientes'][0]['itens'][0]['registro_glosa_id'] = 99
    assert merge_issec_cards([a, b]) == [a, b]
    b = card(medication=True)
    b['pacientes'][0]['itens'].append(deepcopy(b['pacientes'][0]['itens'][0]))
    assert merge_issec_cards([a, b]) == [a, b]
