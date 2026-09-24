from datetime import date
from decimal import Decimal

from app_prontocardio.biq_hemodinamica import consultar_receita_hemodinamica


class _FakeMappings:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def mappings(self):
        return _FakeMappings(self._rows)


class _FakeSession:
    def __init__(self, rows):
        self._rows = rows
        self.params = None

    def execute(self, _query, params):
        self.params = params
        return _FakeResult(self._rows)


def test_receita_hemodinamica_uses_real_mv_values_and_inclusive_period():
    session = _FakeSession(
        [
            {
                "procedimento": "PCT ANGIOPLASTIA",
                "receita": Decimal("125000.50"),
                "quantidade": Decimal("12"),
                "atendimentos": 8,
                "receita_total": Decimal("200000.75"),
            },
            {
                "procedimento": "PCT CATETERISMO",
                "receita": Decimal("75000.25"),
                "quantidade": Decimal("30"),
                "atendimentos": 24,
                "receita_total": Decimal("200000.75"),
            },
        ]
    )

    payload = consultar_receita_hemodinamica(
        session=session,
        data_inicio=date(2026, 9, 1),
        data_fim=date(2026, 9, 24),
        cd_convenio="1,2",
        procedimento="CATETERISMO CARDIACO",
    )

    assert session.params == {
        "data_inicio": date(2026, 9, 1),
        "data_fim_exclusiva": date(2026, 9, 25),
        "cd_convenio": "1,2",
        "procedimento": "CATETERISMO CARDIACO",
        "cd_setor_hemodinamica": 26,
        "limite": 12,
    }
    assert payload == {
        "periodo": {"data_inicio": "2026-09-01", "data_fim": "2026-09-24"},
        "cd_setor": 26,
        "receita_total": 200000.75,
        "receita_por_procedimento": [
            {
                "procedimento": "PCT ANGIOPLASTIA",
                "receita": 125000.5,
                "quantidade": 12.0,
                "atendimentos": 8,
            },
            {
                "procedimento": "PCT CATETERISMO",
                "receita": 75000.25,
                "quantidade": 30.0,
                "atendimentos": 24,
            },
        ],
    }
