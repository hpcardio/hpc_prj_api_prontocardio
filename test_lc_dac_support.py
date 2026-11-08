from datetime import date, timedelta

from evolucao_sadt_backend.lc_dac_support import (
    _latest_lab,
    _latest_protocol_labs,
    detect_lc_dac_medications,
)


def test_detects_statin_name_dose_and_high_intensity_from_active_prescription():
    detected = detect_lc_dac_medications(
        ['ROSUVASTATINA 20 MG COMPRIMIDO', 'AAS 100 MG']
    )

    assert detected['estatina'] == 'sim'
    assert detected['estatina_farmaco_dose'] == 'ROSUVASTATINA 20 MG COMPRIMIDO'
    assert detected['estatina_alta_intensidade'] == 'sim'
    assert detected['aas'] == 'sim'


def test_detects_anticoagulant_and_current_combination_from_active_prescription():
    detected = detect_lc_dac_medications(
        ['APIXABANA 5 MG', 'CLOPIDOGREL 75 MG']
    )

    assert detected['anticoagulante'] == 'sim'
    assert detected['anticoagulante_estrategia'] == 'clopidogrel'
    assert detected['segundo_antiagregante'] == 'clopidogrel'


def test_does_not_claim_absent_medications_when_prescription_has_no_match():
    detected = detect_lc_dac_medications(['LOSARTANA 50 MG'])

    assert 'estatina' not in detected
    assert 'aas' not in detected
    assert 'anticoagulante' not in detected


def test_ignores_hba1c_older_than_the_document_defined_three_month_window():
    today = date(2026, 9, 11)
    rows = [
        {
            'nm_exa_lab': 'Hemoglobina glicada',
            'nm_campo': 'HbA1c',
            'ds_resultado': '7.2',
            'dt_laudo': today - timedelta(days=91),
        }
    ]

    assert _latest_lab(rows, 'hba1c', max_age_days=90, today=today) is None


def test_collects_latest_numeric_protocol_labs_from_mv_rows():
    rows = [
        {
            'nm_exa_lab': 'HEMOGRAMA COMPLETO',
            'nm_campo': 'HEMOGLOBINA',
            'ds_resultado': '13,7',
            'dt_laudo': date(2026, 9, 11),
        },
        {
            'nm_exa_lab': 'CREATININA',
            'nm_campo': 'RESULTADO',
            'ds_resultado': '0.9',
            'dt_laudo': date(2026, 9, 10),
        },
        {
            'nm_exa_lab': 'CREATININA',
            'nm_campo': 'RESULTADO',
            'ds_resultado': '1.2',
            'dt_laudo': date(2026, 8, 1),
        },
    ]

    results = _latest_protocol_labs(rows)

    assert results['Hemoglobina']['valor'] == '13.7'
    assert results['Creatinina']['valor'] == '0.9'
    assert results['Creatinina']['fonte'] == 'MV/RES_EXA'
