from datetime import date
from decimal import Decimal
from unittest.mock import Mock

import pytest

from app_prontocardio.routers import repasse_medico as module


def billing(code='40901645'):
    return {
        'chave_faturamento': 'AMB:1:procedure',
        'cd_atendimento': 1,
        'nm_paciente': 'Paciente ficticio',
        'dt_lancamento': date(2026, 10, 1),
        'cd_pro_fat': code,
        'ds_procedimento': 'Exame',
        'quantidade': 1,
        'valor_bruto': Decimal('100'),
    }


def report(code='40901360', doctor=7):
    return {
        'cd_atendimento': 1,
        'cd_pro_fat': code,
        'cd_prestador': doctor,
        'nm_prestador': 'Medico ficticio',
        'cd_laudo': doctor,
    }


@pytest.mark.parametrize(
    ('code', 'reports', 'origin', 'doctor'),
    [
        ('40901645', [report()], 'laudo_exato', 7),
        ('30911141', [report('30911079')], 'laudo_hemodinamico_unico', 7),
        (
            '30911141',
            [report('30911141', 8), report('30911079')],
            'laudo_exato',
            8,
        ),
        ('40901645', [report(doctor=7), report(doctor=8)], 'ambiguo', None),
        ('41001230', [report()], 'sem_laudo_compativel', None),
        ('41001230', [], 'sem_laudo_compativel', None),
    ],
)
def test_report_attribution_preserves_ambiguity(code, reports, origin, doctor):
    result = module.resolver_particulares_por_laudo([billing(code)], reports)[
        0
    ]
    assert result['codigo_prestador'] == doctor
    assert result['origem_atribuicao'] == origin
    assert result['precisa_conferencia'] == (doctor is None)
    assert result['valor_bruto'] == Decimal('100')


def test_reports_never_cross_attendances():
    other = {**report(), 'cd_atendimento': 2}
    result = module.resolver_particulares_por_laudo([billing()], [other])[0]
    assert result['codigo_prestador'] is None


def test_preview_forwards_optional_flag_without_changing_default(monkeypatch):
    preview = Mock(return_value={})
    monkeypatch.setattr(module, 'montar_preview', preview)
    module.consultar_preview(None, 'session', date(2026, 10, 1), 1, None, True)
    preview.assert_called_once_with(
        'session', date(2026, 10, 1), 1, None, True
    )
    assert module.montar_preview is preview


@pytest.mark.parametrize(
    ('code', 'reports', 'expected'),
    [
        (
            '40901645',
            [{**report(), 'dt_laudo': date(2026, 10, 2)}],
            date(2026, 10, 2),
        ),
        (
            '40901645',
            [
                {**report(), 'dt_laudo': date(2026, 10, 4)},
                {**report(), 'dt_laudo': None},
                {**report(), 'dt_laudo': date(2026, 10, 2)},
                {**report('99999999'), 'dt_laudo': date(2026, 10, 9)},
                {
                    **report(),
                    'cd_atendimento': 2,
                    'dt_laudo': date(2026, 10, 10),
                },
            ],
            date(2026, 10, 4),
        ),
        (
            '30911141',
            [{**report('30911079'), 'dt_laudo': date(2026, 10, 3)}],
            date(2026, 10, 3),
        ),
        ('40901645', [report()], None),
        ('40901645', [{**report(), 'dt_laudo': None}], None),
        (
            '40901645',
            [
                {**report(doctor=7), 'dt_laudo': date(2026, 10, 2)},
                {**report(doctor=8), 'dt_laudo': date(2026, 10, 3)},
            ],
            None,
        ),
        (
            '41001230',
            [{**report(), 'dt_laudo': date(2026, 10, 2)}],
            None,
        ),
    ],
)
def test_report_date_preserves_existing_production_behavior(
    code, reports, expected
):
    result = module.resolver_particulares_por_laudo([billing(code)], reports)[
        0
    ]
    assert result['data_laudo'] == expected
    assert result['data_atendimento'] == date(2026, 10, 1)
