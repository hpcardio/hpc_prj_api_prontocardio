from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from fastapi import HTTPException

from app_prontocardio.routers import painel_leitos_soulmv as panel


def test_care_scales_projects_scores_and_limits_period(monkeypatch):
    calls = []
    timestamp = datetime(2026, 10, 30, 8, 0)

    def fetch(sql, binds):
        calls.append((sql, binds))
        return [
            {
                'CD_AVALIACAO': 100,
                'CD_FORMULA': 200,
                'DS_FORMULA': ' BRADEN ',
                'DH_AVALIACAO': timestamp,
                'VL_RESULTADO': Decimal('18'),
                'DS_INTERPRETACAO_RESULTADO': ' Baixo risco ',
                'NM_USUARIO': ' Profissional ',
            }
        ]

    monkeypatch.setattr(panel, 'fetch_all', fetch)
    before = datetime.now() - timedelta(days=7)
    response = panel.escalas_assistenciais(123)
    after = datetime.now() - timedelta(days=7)
    assert response['period'] == '7d'
    assert response['evaluations'] == [
        {
            'id': 100,
            'formulaId': 200,
            'formula': 'BRADEN',
            'date': timestamp.isoformat(),
            'value': 18,
            'interpretation': 'Baixo risco',
            'user': 'Profissional',
        }
    ]
    assert calls[0][0] == panel.SQL_ESCALAS_ASSISTENCIAIS
    assert calls[0][1]['admission_id'] == 123  # noqa: PLR2004
    assert before <= calls[0][1]['start_date'] <= after


def test_care_scales_empty_and_missing_values(monkeypatch):
    monkeypatch.setattr(panel, 'fetch_all', lambda *_: [])
    assert panel.escalas_assistenciais(123)['evaluations'] == []
    monkeypatch.setattr(panel, 'fetch_all', lambda *_: [{}])
    row = panel.escalas_assistenciais(123)['evaluations'][0]
    assert row['interpretation'] is None
    assert row['formula'] == 'Escala'
    assert row['user'] == 'Profissional não informado'


def test_care_scales_database_errors_do_not_expose_details(monkeypatch):
    def fail(*_):
        raise RuntimeError('sensitive database detail')

    monkeypatch.setattr(panel, 'fetch_all', fail)
    with pytest.raises(HTTPException) as error:
        panel.escalas_assistenciais(123)
    assert error.value.status_code == 503  # noqa: PLR2004
    assert 'sensitive' not in error.value.detail
