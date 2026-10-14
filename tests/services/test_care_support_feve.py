from datetime import date
from io import BytesIO
from unittest.mock import Mock

import pytest
from reportlab.pdfgen import canvas

from evolucao_sadt_backend import care_support as care
from evolucao_sadt_backend.forms_app import _render_ic_current


@pytest.mark.parametrize(
    ('text', 'expected'),
    [
        ('FEVE: 55%', '55'),
        ('Fração de ejeção do ventrículo esquerdo (Simpson): 42,5%', '42.5'),
        ('FEVE = 0%', '0'),
        ('FEVE: 100%', '100'),
        ('FEVE 101%', None),
        ('FEVE -10%', None),
        ('Saturação 97%', None),
        ('FEVE: 55', None),
        (None, None),
    ],
)
def test_extracts_only_explicit_valid_percentage(text, expected):
    assert care.extract_feve_from_report(text) == expected


def test_pdf_extraction_and_invalid_document():
    buf = BytesIO()
    pdf = canvas.Canvas(buf)
    pdf.drawString(30, 700, 'FEVE: 55%')
    pdf.save()
    assert (
        care.extract_feve_from_report(care._extract_pdf_text(buf.getvalue()))
        == '55'
    )
    assert care._extract_pdf_text(b'not a PDF') is None
    assert care._extract_pdf_text(None) is None


def test_latest_usable_echo_retains_date_and_origin(monkeypatch):
    session = Mock()
    session.execute.return_value.mappings.return_value.all.return_value = [
        dict(laudo_pdf=b'first', dt_laudo=date(2026, 2, 1)),
        dict(laudo_pdf=b'second', dt_laudo=date(2026, 1, 1)),
    ]
    monkeypatch.setattr(
        care,
        '_extract_pdf_text',
        lambda value: 'FEVE 45%' if value == b'second' else None,
    )
    result = care._load_latest_echo_feve(session, 123)
    assert result['valor'] == '45'
    assert result['data'] == '2026-01-01'
    assert 'MV' in result['origem']
    assert session.execute.call_args.args[1] == {'cd_atendimento': 123}


@pytest.mark.parametrize('line', ['LC-IC', 'LC-PREVENCAO', 'LC-DAC'])
def test_failed_echo_is_optional_and_restricted_to_ic(monkeypatch, line):
    monkeypatch.setattr(care, 'load_lc_dac_support', lambda *args: {})
    monkeypatch.setattr(care, '_attendance_identification', lambda *args: {})
    monkeypatch.setattr(
        care, '_load_latest_receituario_text', lambda *args: None
    )
    monkeypatch.setattr(care, 'latest_previous_form_value', lambda *args: None)
    echo = Mock(side_effect=RuntimeError('unavailable'))
    monkeypatch.setattr(care, '_load_latest_echo_feve', echo)
    result = care.load_care_support(Mock(), 123, line)
    if line == 'LC-IC':
        echo.assert_called_once()
        assert 'feve' not in result['campos']
        assert any('manualmente' in message for message in result['alertas'])
    else:
        echo.assert_not_called()


def test_improved_phenotype_rendering_preserves_feve():
    result = _render_ic_current({
        'fenotipo_atual': 'FEVE_MELHORADA',
        'feve': '55',
    })
    assert 'Fenótipo atual: FEVE melhorada.' in result
    assert 'FEVE: 55%' in result
