from evolucao_sadt_backend.forms_app import FormInput, render_form
from evolucao_sadt_backend.lc_dac_support import (
    detect_lc_dac_medications,
    extract_numeric_result,
)


def test_cal1_renders_sara_clinical_flow():
    text = render_form(
        FormInput(
            cd_atendimento=345211,
            linha_cuidado='LC-DAC',
            versao='2.0',
            dados={
                'diagnostico': 'pos_angioplastia_sca',
                'tempo': 'cal1',
                'data_angioplastia': '2026-07-20',
                'icp_chip': 'nao',
                'sintomas': ['angina_estavel', 'dispneia_esforcos'],
                'ccs': 'ii',
                'tratamento_antianginoso': 'otimizado',
                'eventos': ['nenhum'],
                'has': 'sim',
                'pa_sistolica': '142',
                'pa_diastolica': '82',
                'pa_condutas': ['dose_ajustada'],
                'fc': '72',
                'peso': '81',
                'altura': '1.75',
                'imc': '26.4',
                'ldl_resultado': '68',
                'ldl_data': '2026-08-20',
                'ldl_condutas': ['ezetimiba'],
                'estatina': 'sim',
                'estatina_alta_intensidade': 'sim',
                'aas': 'sim',
                'segundo_antiagregante': 'ticagrelor',
                'anticoagulante': 'nao',
                'plano_terapeutico': 'ajustado',
                'diabetes': 'nao',
                'tabagismo_status': 'nunca',
                'atividade_fisica_status': 'insuficiente',
                'atividade_orientacao': 'sim',
                'alimentacao_status': 'adequada',
                'adesao': 'adequada',
                'efeitos_adversos': 'nao',
                'acesso_vascular': 'sem_alteracoes',
                'investigacao_adicional': 'nao',
                'decisoes_finais': ['otimizacao_terapeutica'],
                'proximo_acompanhamento': 'cronograma',
            },
        )
    )

    assert 'FORMULÁRIO CLÍNICO: LC-DAC v2.0' in text
    assert 'SITUAÇÃO CLÍNICA ATUAL' in text
    assert 'Classe CCS: II' in text
    assert 'Pressão arterial: 142/82 mmHg. Situação: Fora da meta.' in text
    assert 'AVALIAÇÃO ESPECÍFICA DO CAL1' in text
    assert 'DECISÃO CLÍNICA FINAL' in text
    assert len(text) <= 4000


def test_cir_without_change_is_compact_and_keeps_therapeutic_review():
    text = render_form(
        FormInput(
            cd_atendimento=345211,
            linha_cuidado='LC-DAC',
            versao='2.0',
            dados={
                'diagnostico': 'dac_cronica_estavel',
                'tempo': 'cir1',
                'mudanca_clinica_cir': 'nao',
                'ldl_resultado': '48',
                'estatina': 'sim',
                'estatina_alta_intensidade': 'sim',
                'aas': 'sim',
                'anticoagulante': 'nao',
                'plano_terapeutico': 'adequado',
                'diabetes': 'nao',
                'tabagismo_status': 'nunca',
                'atividade_fisica_status': 'adequada',
                'alimentacao_status': 'adequada',
                'adesao': 'adequada',
                'efeitos_adversos': 'nao',
                'decisoes_finais': ['estavel'],
                'proximo_acompanhamento': 'cronograma',
            },
        )
    )

    assert 'CONSULTA DE INTEGRAÇÃO DE RESULTADOS' in text
    assert 'Mudança clínica relevante desde a CAL: Não.' in text
    assert 'SINTOMAS DESDE A ÚLTIMA CONSULTA' not in text
    assert 'CONTROLE DA DISLIPIDEMIA' in text


def test_mv_support_parses_strict_numbers_and_medications():
    assert extract_numeric_result('48,7') == '48.7'
    assert extract_numeric_result('48,7 mg/dL') is None
    detected = detect_lc_dac_medications(
        ['Atorvastatina 40 mg', 'Somalgin Cardio 100 mg', 'Ticagrelor 90 mg']
    )
    assert detected == {
        'estatina': 'sim',
        'estatina_farmaco_dose': 'Atorvastatina 40 mg',
        'estatina_alta_intensidade': 'sim',
        'aas': 'sim',
        'segundo_antiagregante': 'ticagrelor',
    }


def test_v3_renders_the_informatized_form_nomenclature_and_structured_exceptions():
    text = render_form(
        FormInput(
            cd_atendimento=345211,
            linha_cuidado='LC-DAC',
            versao='3.0',
            dados={
                'diagnostico': 'pos_angioplastia_sca',
                'contexto_revascularizacao': 'sca',
                'data_angioplastia': '2026-08-12',
                'tempo': 'cal1',
                'icp_chip': 'sim',
                'chip_criterios': ['tronco', 'stent_60'],
                'sintomas': ['assintomatico'],
                'eventos': ['nenhum'],
                'pa_sistolica': '129',
                'pa_diastolica': '79',
                'ldl_resultado': '68',
                'ldl_condutas': ['nao_intensificado'],
                'ldl_nao_intensificado_justificativa': 'acesso_custo',
                'estatina': 'sim',
                'estatina_alta_intensidade': 'sim',
                'aas': 'sim',
                'segundo_antiagregante': 'ticagrelor',
                'anticoagulante': 'sim',
                'anticoagulante_estrategia': 'ticagrelor',
                'plano_terapeutico': 'adequado',
                'diabetes': 'sim',
                'hba1c_faixa': '7_8_9',
                'tabagismo_status': 'passivo',
                'atividade_fisica_status': 'limitada_cardiovascular',
                'alimentacao_status': 'necessita_intervencao',
                'alimentacao_intervencoes': ['nutricao'],
                'adesao': 'adequada',
                'efeitos_adversos': 'nao',
                'investigacao_adicional': 'nao',
                'decisoes_finais': ['estavel'],
                'proximo_acompanhamento': 'antecipado',
                'retorno_intervalo': '1_3_meses',
                'exame_ecg': 'solicitar',
            },
        )
    )

    assert 'FORMULÁRIO CLÍNICO: LC-DAC v3.0' in text
    assert 'Contexto da revascularização: Síndrome coronariana aguda.' in text
    assert 'Data da ICP: 2026-08-12.' in text
    assert 'CHIP: Sim. Critérios: ICP em tronco da coronária esquerda, Stent >60 mm.' in text
    assert 'Limitação de acesso / custo' in text
    assert 'Anticoagulante + Ticagrelor' in text
    assert 'HbA1c: 7–8,9%.' in text
    assert 'Tabagismo passivo relevante' in text
    assert 'ECG: Solicitar.' in text
    assert 'Retorno antecipado - 1–3 meses.' in text


def test_v3_uses_strict_less_than_130_80_for_the_institutional_bp_target():
    text = render_form(
        FormInput(
            cd_atendimento=345211,
            linha_cuidado='LC-DAC',
            versao='3.0',
            dados={
                'diagnostico': 'dac_cronica_estavel',
                'tempo': 'cal1',
                'pa_sistolica': '130',
                'pa_diastolica': '80',
            },
        )
    )
    assert 'Pressão arterial: 130/80 mmHg. Situação: Fora da meta.' in text


def test_v31_renders_physical_exam_and_detailed_hba1c_conduct():
    text = render_form(
        FormInput(
            cd_atendimento=345211,
            linha_cuidado='LC-DAC',
            versao='3.1',
            dados={
                'diagnostico': 'dac_cronica_estavel',
                'tempo': 'cal1',
                'exame_fisico_status': 'com_alteracoes',
                'exame_fisico_descricao': 'Sopro sistólico em foco aórtico',
                'diabetes': 'sim',
                'hba1c_resultado': '7.4',
                'hba1c_data': '2026-09-15',
                'hba1c_status': 'fora_meta',
                'hba1c_condutas': ['medicamento_ajustado', 'nao_ajustado'],
                'hba1c_nao_ajustado_justificativa': 'meta_individualizada',
            },
        )
    )

    assert 'FORMULÁRIO CLÍNICO: LC-DAC v3.1' in text
    assert 'Exame físico cardiovascular: Com alterações relevantes - Sopro sistólico em foco aórtico.' in text
    assert 'HbA1c: 7.4% em 2026-09-15. Situação: Fora da meta.' in text
    assert 'Tratamento medicamentoso ajustado' in text
    assert 'Não ajustado' in text
    assert 'Meta individualizada' in text


if __name__ == '__main__':
    test_cal1_renders_sara_clinical_flow()
    test_cir_without_change_is_compact_and_keeps_therapeutic_review()
    test_mv_support_parses_strict_numbers_and_medications()
    print('lc-dac-sara=OK')
