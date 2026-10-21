from decimal import Decimal
from unittest.mock import Mock

from app_prontocardio.routers import financeiro as f
from app_prontocardio.services.maida_followup import (
    match_maida_items,
    unique_document_lines,
)


def line(**changes):
    return dict(
        dict(
            registro_glosa_id=37209,
            documento_maida_id='document-31554',
            aba_maida='DemonstrativoAnaliseDeContas',
            linha_maida=76,
            numero_lote='31554',
            numero_guia_senha='580314',
            codigo_servico='40101010',
            codigo_glosa='1811',
            data_realizacao='2026-08-26',
            referencia='2026-09-01',
            quantidade_executada=Decimal('1'),
            valor_processado=Decimal('30'),
            valor_glosa=Decimal('30'),
            valor_liberado=Decimal('0'),
            origem_maida='lote_glosa_recursado',
            id_registro='first',
        ),
        **changes,
    )


def test_same_document_from_both_filters_counts_once():
    rows = []
    for index, value in enumerate(('30', '.88', '.88', '.88')):
        original = line(linha_maida=index + 1, valor_glosa=Decimal(value))
        rows.extend([
            original,
            dict(
                original,
                origem_maida='lote_glosa_nao_recursado',
                id_registro=f'copy-{index}',
            ),
        ])
    result = list(unique_document_lines(rows))
    assert len(result) == len(rows) // 2
    assert sum(row['valor_glosa'] for row in result) == Decimal('32.64')
    assert len(rows) == len(result) * 2


def test_distinct_lines_and_conflicting_values_are_preserved():
    original = line()
    for changes in (
        {'linha_maida': 77},
        {'documento_maida_id': 'another-document'},
        {'aba_maida': 'another-sheet'},
        {'valor_glosa': Decimal('29')},
        {'numero_guia_senha': 'different-guide'},
        {'codigo_glosa': '2601'},
        {'quantidade_executada': Decimal('2')},
        {'origem_maida': None},
        {'documento_maida_id': None},
        {'linha_maida': None},
        {'aba_maida': None},
    ):
        other = line(**changes)
        assert list(unique_document_lines([original, other])) == [
            original,
            other,
        ]


def test_each_treatment_keeps_its_document_values():
    a, b = line(), line(registro_glosa_id=37326)
    assert list(
        unique_document_lines([a, b, a], scope=('registro_glosa_id',))
    ) == [a, b]


def test_followup_aggregation_uses_physical_line_once(monkeypatch):
    monkeypatch.setattr(f, '_tabela_ipm_existe', lambda *args: True)
    a = line(criterio_correspondencia='guia_procedimento')
    b = dict(a, origem_maida='lote_glosa_nao_recursado', id_registro='second')
    c = dict(a, linha_maida=77, id_registro='third')
    session = Mock()
    session.execute.return_value.mappings.return_value = [a, b, c]
    result = f._dados_demonstrativo_registros_follow_up(session, {37209})[
        37209
    ]
    assert result['valor_glosa'] == Decimal('60')
    assert result['quantidade_maida'] == Decimal('2')
    assert result['numero_lote'] == '31554'
    sql = str(session.execute.call_args.args[0])
    for field in ('documento_maida_id', 'aba_maida', 'linha_maida'):
        assert f'demo.{field}' in sql


def test_portal_item_matching_deduplicates_the_same_document():
    a = line()
    b = dict(a, origem_maida='lote_glosa_nao_recursado', id_registro='second')
    item = dict(
        nm_convenio='ISSEC',
        nr_guia='580314',
        cd_pro_fat='40101010',
        cd_remessa=20035,
        cd_reg=424496,
        cd_lancamento=9,
    )
    assert list(match_maida_items([a, b], [item])) == [(a, item)]
