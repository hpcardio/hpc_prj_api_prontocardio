from evolucao_sadt_backend.forms_app import _render_prevent_current, _render_ic_current


def test_existing_risk_target_overrides_manual_value():
    output = _render_prevent_current(dict(tipo_consulta='CAL', categoria_risco='risco_alto', ldl_resultado='75', ldl_meta='130', ldl_situacao='na_meta'))
    assert '70 mg/dL' in output
    assert '130 mg/dL' not in output
    assert 'Risco Alto' in output


def test_existing_medication_dose_and_intermediate_phenotype_are_preserved():
    for phenotype in ['icfer', 'icfemi']:
        output = _render_ic_current(dict(fenotipo_atual=phenotype, pilar_arni_decisao='manter', pilar_arni_medicacao_dose='EXEMPLO', icfemi_isglt2='manter'))
        assert 'EXEMPLO' in output
