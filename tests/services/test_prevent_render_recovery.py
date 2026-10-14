from evolucao_sadt_backend.forms_app import _render_prevent_current


def test_preserves_new_prevention_fields():
    result = _render_prevent_current(
        dict(
            tipo_consulta='CAL',
            hipertensao='sim',
            dislipidemia='nao',
            outros_fatores_relevantes='Fator informado',
            intercorrencia_outra='Intercorrência informada',
            altura='1.70',
            imc='24',
            peso_anterior='70',
            meta_peso='68',
            ldl_meta='50',
            hba1c_meta='7',
            creatinina='1.0',
            tfg='90',
            rac_resultado='10',
        )
    )
    for value in [
        'Hipertensão arterial',
        'Fator informado',
        'Intercorrência informada',
        'Altura: 1.70 m.',
        'IMC: 24 kg/m².',
        'Peso anterior: 70 kg.',
        'Meta de peso: 68 kg.',
        'Meta de LDL-C: 50 mg/dL.',
        'Meta de HbA1c: 7%.',
        'Creatinina: 1.0 mg/dL.',
        'TFGe: 90 mL/min/1,73m².',
        'RAC: 10 mg/g.',
    ]:
        assert value in result
    assert 'Dislipidemia' not in result


def test_empty_fields_do_not_generate_empty_sections():
    result = _render_prevent_current({})
    assert 'PERFIL CARDIOVASCULAR' not in result
    assert 'Meta de LDL-C:' not in result
    assert 'Altura:' not in result


def test_cir_keeps_existing_rendering_path():
    result = _render_prevent_current(
        dict(
            tipo_consulta='CIR',
            hipertensao='sim',
            integracao_resultados='Resultados conferidos',
            decisoes_cir='Conduta informada',
        )
    )
    assert 'Resultados conferidos' in result and 'Conduta informada' in result
    assert 'PERFIL CARDIOVASCULAR' not in result
