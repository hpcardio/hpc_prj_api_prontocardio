from evolucao_sadt_backend.forms_app import FormInput, render_form


def test_prevention_v4_uses_clinical_headings_without_document_numbering():
    text = render_form(FormInput(
        cd_atendimento=343092,
        linha_cuidado='LC-PREVENCAO',
        versao='4.0',
        dados={
            'tipo_consulta': 'CAL',
            'intercorrencia': 'nao',
            'pa_situacao': 'fora_da_meta',
            'pa_sistolica': '142',
            'pa_diastolica': '86',
            'pa_condutas': ['ajustar'],
            'ldl_situacao': 'na_meta',
            'ldl_resultado': '48',
            'ldl_data': '2026-09-12',
            'ldl_nova_dosagem': 'prevista_pelo_ciclo',
            'hba1c_situacao': 'fora_da_meta',
            'hba1c_condutas': ['ajustar', 'nutricao'],
            'atividade_fisica_conduta': 'Estimular caminhada 30 minutos, 5 vezes por semana.',
            'prescricao_atual': 'Losartana 50 mg 1 cp/dia',
            'adesao': 'adequada',
            'efeitos_adversos': 'nao',
            'avaliacao_cardiovascular_complementar': ['ecg'],
            'exames_solicitados': ['40302580'],
            'prioridades': ['controle_pressao'],
            'proxima_etapa': 'cal2',
            'conduta_adicional': 'Reavaliar em 30 dias.',
        },
    ))
    assert text.index('EVOLUÇÃO CLÍNICA') < text.index('METAS CARDIOMETABÓLICAS')
    assert 'LDL-C: 48 mg/dL em 2026-09-12.' in text
    assert 'Nova dosagem: PREVISTA PELO CICLO.' in text
    assert 'Situação da HbA1c: FORA DA META.' in text
    assert 'Ajustar tratamento do diabete melito' in text
    assert 'Estimular caminhada 30 minutos' in text
    assert 'Losartana 50 mg 1 cp/dia' in text
    assert 'Exames selecionados para solicitação: 40302580.' in text
    assert 'Próxima etapa: CAL 2.' in text
    assert '11. PLANO' not in text
    assert '6.1.' not in text


def test_prevention_cir_is_short_and_does_not_repeat_cal():
    text = render_form(FormInput(
        cd_atendimento=343092,
        linha_cuidado='LC-PREVENCAO',
        versao='4.0',
        dados={'tipo_consulta': 'CIR', 'mudanca_clinica_cir': 'nao', 'decisoes_cir': 'Manter plano vigente.'},
    ))
    assert 'INTEGRAÇÃO DE RESULTADOS — CIR' in text
    assert 'DECISÃO CLÍNICA' in text
    assert 'METAS CARDIOMETABÓLICAS' not in text
    assert '14.' not in text


def test_prevention_cal_organizes_complete_data_targets_and_consolidated_plan():
    text = render_form(FormInput(
        cd_atendimento=343092,
        linha_cuidado='LC-PREVENCAO',
        versao='4.0',
        dados={
            'tipo_consulta': 'CAL',
            'hipertensao': 'sim',
            'diabete_melito': 'sim',
            'outros_fatores_relevantes': 'História familiar de doença cardiovascular prematura',
            'intercorrencia': 'sim',
            'intercorrencias': ['outra'],
            'intercorrencia_outra': 'Edema de membros inferiores',
            'pa_sistolica': '142',
            'pa_diastolica': '86',
            'peso_atual': '82',
            'altura': '1,70',
            'imc': '28,4',
            'peso_anterior': '85',
            'meta_peso': '78',
            'pa_situacao': 'fora_da_meta',
            'pa_condutas': ['ajustar'],
            'ldl_resultado': '48',
            'ldl_meta': '50',
            'ldl_situacao': 'na_meta',
            'hba1c_resultado': '8,1',
            'hba1c_meta': '7',
            'hba1c_situacao': 'fora_da_meta',
            'hba1c_condutas': ['ajustar', 'nutricao'],
            'creatinina': '1,2',
            'tfg': '58',
            'rac_resultado': '42',
            'renal_situacao': 'alterado',
            'prescricao_atual': 'Losartana 50 mg 1 cp/dia',
            'conduta_adicional': 'Reavaliar em 30 dias',
        },
    ))
    assert 'Hipertensão arterial' in text
    assert 'Diabete melito' in text
    assert 'História familiar de doença cardiovascular prematura' in text
    assert 'Edema de membros inferiores' in text
    assert 'Altura: 1,70 m.' in text
    assert 'IMC: 28,4 kg/m².' in text
    assert 'Peso anterior: 85 kg.' in text
    assert 'Meta de peso: 78 kg.' in text
    assert 'Meta de LDL-C: 50 mg/dL.' in text
    assert 'Meta de HbA1c: 7%.' in text
    assert 'Creatinina: 1,2 mg/dL.' in text
    assert 'TFGe: 58 mL/min/1,73m².' in text
    assert 'RAC: 42 mg/g.' in text
    assert 'Situação da pressão arterial: FORA DA META.' in text
    assert 'Situação do LDL-C: NA META.' in text
    assert 'Situação da HbA1c: FORA DA META.' in text
    assert 'PLANO TERAPÊUTICO CONSOLIDADO' in text
    assert 'Ajustar tratamento anti-hipertensivo.' in text
    assert 'Ajustar tratamento do diabete melito; Reforçar orientação alimentar.' in text
    assert not any(line.lstrip().startswith(tuple(f'{i}.' for i in range(1, 20))) for line in text.splitlines())


def test_prevention_cal_derives_ldl_target_and_status_from_risk_category():
    text = render_form(FormInput(
        cd_atendimento=343092,
        linha_cuidado='LC-PREVENCAO',
        versao='4.0',
        dados={
            'tipo_consulta': 'CAL',
            'categoria_risco': 'risco_muito_alto',
            'ldl_resultado': '50',
            'ldl_meta': '130',
        },
    ))
    assert 'Categoria de risco cardiovascular: Risco Muito Alto.' in text
    assert 'Meta de LDL-C: < 50 mg/dL.' in text
    assert 'Situação do LDL-C: FORA DA META.' in text
    assert 'Meta de LDL-C: 130 mg/dL.' not in text


def test_prevention_cal_keeps_legacy_risk_label_drafts_compatible():
    text = render_form(FormInput(
        cd_atendimento=343092,
        linha_cuidado='LC-PREVENCAO',
        versao='4.0',
        dados={
            'tipo_consulta': 'CAL',
            'categoria_risco': 'Risco Extremo',
            'ldl_resultado': '39',
        },
    ))
    assert 'Categoria de risco cardiovascular: Risco Extremo.' in text
    assert 'Meta de LDL-C: < 40 mg/dL.' in text
    assert 'Situação do LDL-C: NA META.' in text
