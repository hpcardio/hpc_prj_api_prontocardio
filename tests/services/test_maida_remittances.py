from decimal import Decimal
from unittest.mock import patch

from app_prontocardio.services import maida_followup as m


def demo(guide, ident, **changes):
    return dict(
        dict(
            id_registro=ident,
            numero_guia_senha=guide,
            codigo_servico='0012',
            origem_maida='lote_glosa_recursado',
            valor_glosa=Decimal('0.88'),
        ),
        **changes,
    )


def item(guide, remittance, launch=1):
    return dict(
        nr_guia=guide,
        cd_remessa=remittance,
        cd_reg=remittance,
        cd_lancamento=launch,
        cd_pro_fat='12',
        nm_convenio='ISSEC',
    )


def test_indexed_matching_preserves_results_and_ambiguities():
    rows = [
        demo('A', '1'),
        demo('B', '2'),
        demo('A', '3'),
        demo('C', '4'),
        demo('D', '5', codigo_servico='99'),
    ]
    items = {
        1: [item('A', 1)],
        2: [item('B', 2), item('B', 2, 2)],
        3: [item('D', 3)],
        4: [],
        5: [item('A', 5)],
    }
    expected = {
        k: list(m.match_maida_items(rows, v, {'99': '12'}))
        for k, v in items.items()
    }
    assert m.match_maida_remittances(rows, items, {'99': '12'}) == expected


def test_each_remittance_receives_only_its_guide_not_all_documents():
    rows = [demo(str(n), str(n)) for n in range(172)]
    items = {n: [item(str(n), n)] for n in range(172)}
    with patch.object(
        m, 'match_maida_items', wraps=m.match_maida_items
    ) as match:
        result = m.match_maida_remittances(rows, items)
    assert all(len(pairs) == 1 for pairs in result.values())
    assert sum(len(call.args[0]) for call in match.call_args_list) == len(rows)


def test_duplicate_imports_stay_deduplicated_and_distinct_lines_preserved():
    a = demo(
        'A', '1', documento_maida_id='doc', aba_maida='sheet', linha_maida=7
    )
    b = dict(a, id_registro='2', origem_maida='lote_glosa_nao_recursado')
    c = dict(a, id_registro='3', linha_maida=8)
    rows = [a, b, c]
    items = {1: [item('A', 1)]}
    expected = list(m.match_maida_items(rows, items[1]))
    assert m.match_maida_remittances(rows, items)[1] == expected
    assert [pair[0]['id_registro'] for pair in expected] == ['1', '3']
