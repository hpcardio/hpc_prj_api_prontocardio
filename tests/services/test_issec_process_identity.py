from datetime import date
from decimal import Decimal
from unittest.mock import Mock

from app_prontocardio.routers import financeiro as f
from app_prontocardio.services.issec_process_identity import (
    clear_lot_processes,
)


def test_clears_only_proven_lot_in_same_remittance(monkeypatch):
    monkeypatch.setattr(f, '_tabela_ipm_existe', lambda *args: True)
    monkeypatch.setattr(f, '_tabela_schema_existe', lambda *args: True)
    session = Mock()
    session.execute.return_value.mappings.return_value = [
        {'cd_remessa': 20035, 'lote': '31554'},
    ]
    cards = [
        {
            'cd_remessa': remessa,
            'convenio': convenio,
            'processo': {'numero_processo': processo},
            'valor_glosado': '32.64',
        }
        for remessa, convenio, processo in (
            (20035, 'ISSEC', '31554'),
            (20036, 'ISSEC', '31554'),
            (20035, 'ISSEC', '2600035789'),
            (20035, 'OUTRO', '31554'),
            (20035, 'ISSEC', ''),
        )
    ]
    clear_lot_processes(session, cards)
    assert [c['processo']['numero_processo'] for c in cards] == [
        '',
        '31554',
        '2600035789',
        '31554',
        '',
    ]
    assert all(c['valor_glosado'] == '32.64' for c in cards)
    query = str(session.execute.call_args.args[0])
    assert 'NOT EXISTS' in query  # Real portal identifiers are never erased.
    assert 'origem_maida IS NOT NULL' in query


def test_no_query_when_no_process_or_no_issec():
    session = Mock()
    clear_lot_processes(
        session,
        [
            {'cd_remessa': 1, 'convenio': 'ISSEC', 'processo': {}},
        ],
    )
    session.execute.assert_not_called()


def test_lot_is_resolved_by_total_and_either_portal_month(monkeypatch):

    monkeypatch.setattr(f, '_tabela_ipm_existe', lambda *args: True)
    monkeypatch.setattr(f, '_tabela_schema_existe', lambda *args: True)
    session, oracle = Mock(), Mock()
    lot_result, portal_result = Mock(), Mock()
    lot_result.mappings.return_value = [{'cd_remessa': 20035, 'lote': '31554'}]
    portal_result.mappings.return_value = [
        {
            'processo': '2600035789',
            'competencia': '09/2026',
            'mes_producao': date(2026, 8, 1),
            'valor_cobrado': Decimal('3076.96'),
        },
    ]
    session.execute.side_effect = [lot_result, portal_result]
    oracle.execute.return_value.mappings.return_value = [
        {
            'cd_remessa': 20035,
            'cd_reg': 1,
            'dt_competencia': date(2026, 8, 1),
            'vl_total_registro': Decimal('3076.96'),
        },
    ]
    card = {
        'cd_remessa': 20035,
        'convenio': 'ISSEC',
        'processo': {'numero_processo': '31554'},
        'valor_glosado': '32.64',
    }
    f._aplicar_processos_portal_issec(session, oracle, [card])
    assert card['processo']['numero_processo'] == '2600035789'
    assert card['valor_glosado'] == '32.64'


def test_unmatched_lot_stays_empty_instead_of_becoming_a_process(monkeypatch):
    monkeypatch.setattr(f, '_tabela_ipm_existe', lambda *args: True)
    monkeypatch.setattr(f, '_tabela_schema_existe', lambda *args: True)
    session, oracle = Mock(), Mock()
    result = Mock()
    result.mappings.return_value = [{'cd_remessa': 20512, 'lote': '33216'}]
    session.execute.return_value = result
    oracle.execute.return_value.mappings.return_value = []
    card = {
        'cd_remessa': 20512,
        'convenio': 'ISSEC',
        'processo': {'numero_processo': '33216'},
    }
    f._aplicar_processos_portal_issec(session, oracle, [card])
    assert card['processo']['numero_processo'] == ''
