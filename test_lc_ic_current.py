from evolucao_sadt_backend.forms_app import FormInput, render_form


def test_ic_v4_renders_confirmed_decisions_in_document_order():
    text = render_form(FormInput(
        cd_atendimento=343092,
        linha_cuidado='LC-IC',
        versao='4.0',
        dados={
            'tipo_consulta': 'CAL',
            'feve': '35',
            'feve_data': '2026-09-01',
            'fenotipo_atual': 'ICFEr',
            'risco_situacao': 'alto_risco',
            'criterios_alto_risco': ['FEVE ≤35%'],
            'pilar_arm_decisao': 'iniciar',
            'sugestao_cdi': 'considerar',
            'observacao_decisao_adicional': 'Controle laboratorial em 7 dias.',
        },
    ))
    assert text.index('6. FENÓTIPO DA INSUFICIÊNCIA CARDÍACA') < text.index('7. ESTRATIFICAÇÃO DE RISCO')
    assert text.index('7. ESTRATIFICAÇÃO DE RISCO') < text.index('10. TRATAMENTO — ICFEr')
    assert 'FEVE: 35% em 2026-09-01.' in text
    assert 'ARM: Iniciar.' in text
    assert 'Indicar CDI' not in text
    assert '11. TRATAMENTO — ICFEmi' not in text


def test_ic_v4_renders_medication_and_dose_for_each_foundational_therapy():
    text = render_form(FormInput(
        cd_atendimento=343092,
        linha_cuidado='LC-IC',
        versao='4.0',
        dados={
            'tipo_consulta': 'CAL',
            'fenotipo_atual': 'ICFEr',
            'pilar_arni_medicacao_dose': 'Sacubitril/valsartana 49/51 mg, 12/12h',
            'pilar_betabloqueador_medicacao_dose': 'Carvedilol 25 mg, 12/12h',
            'pilar_arm_medicacao_dose': 'Espironolactona 25 mg/dia',
            'pilar_isglt2_medicacao_dose': 'Dapagliflozina 10 mg/dia',
        },
    ))

    assert 'ARNI/IECA/BRA — medicação e dose: Sacubitril/valsartana 49/51 mg, 12/12h.' in text
    assert 'Betabloqueador — medicação e dose: Carvedilol 25 mg, 12/12h.' in text
    assert 'ARM — medicação e dose: Espironolactona 25 mg/dia.' in text
    assert 'iSGLT2 — medicação e dose: Dapagliflozina 10 mg/dia.' in text


def test_ic_v4_renders_the_four_pillars_under_icfemi_treatment():
    text = render_form(FormInput(
        cd_atendimento=343092,
        linha_cuidado='LC-IC',
        versao='4.0',
        dados={
            'tipo_consulta': 'CAL',
            'fenotipo_atual': 'ICFEmi',
            'pilar_arni_decisao': 'manter',
            'pilar_arni_medicacao_dose': 'Enalapril 10 mg, 12/12h',
            'pilar_betabloqueador_decisao': 'titular',
            'pilar_betabloqueador_medicacao_dose': 'Carvedilol 12,5 mg, 12/12h',
            'pilar_arm_decisao': 'iniciar',
            'pilar_arm_medicacao_dose': 'Espironolactona 25 mg/dia',
            'pilar_isglt2_decisao': 'manter',
            'pilar_isglt2_medicacao_dose': 'Dapagliflozina 10 mg/dia',
        },
    ))

    assert '11. TRATAMENTO — ICFEmi' in text
    assert 'ARNI/IECA/BRA: Manter.' in text
    assert 'Betabloqueador: Titular.' in text
    assert 'ARM: Iniciar.' in text
    assert 'iSGLT2: Manter.' in text
    assert 'ARNI/IECA/BRA — medicação e dose: Enalapril 10 mg, 12/12h.' in text
    assert '10. TRATAMENTO — ICFEr' not in text


def test_ic_cir_omits_full_cal_and_keeps_decision():
    text = render_form(FormInput(
        cd_atendimento=343092,
        linha_cuidado='LC-IC',
        versao='4.0',
        dados={'tipo_consulta': 'CIR', 'mudanca_clinica_cir': 'sim', 'mudanca_clinica_descricao': 'Piora da dispneia.', 'decisoes_cir': 'Ajustar diurético.'},
    ))
    assert '18. CIR — CONSULTA DE INTEGRAÇÃO DE RESULTADOS' in text
    assert 'Piora da dispneia.' in text
    assert 'Ajustar diurético.' in text
    assert '10. TRATAMENTO — ICFEr' not in text


def test_ic_v4_keeps_manually_completed_cycle_identification_in_mv_text():
    text = render_form(FormInput(
        cd_atendimento=343092,
        linha_cuidado='LC-IC',
        versao='4.0',
        dados={
            'tipo_consulta': 'CAL',
            'data_ancora': '2026-09-10',
            'tempo': 'cal1',
            'ultima_avaliacao': '2026-09-01',
            'feve': '38',
            'proxima_etapa': 'cir1',
        },
    ))

    assert '3. IDENTIFICAÇÃO DO CICLO' in text
    assert 'Início na Linha de Cuidado: 2026-09-10.' in text
    assert 'Momento atual da Linha: CAL1.' in text
    assert 'Última avaliação da Linha: 2026-09-01.' in text
    assert 'Última FEVE: 38%.' in text
    assert 'Próxima etapa prevista: CIR1.' in text


def test_ic_v4_renders_new_clinical_decisions_and_medications():
    text = render_form(FormInput(
        cd_atendimento=343092,
        linha_cuidado='LC-IC',
        versao='4.0',
        dados={
            'tipo_consulta': 'CAL',
            'altura': '1,70',
            'imc': '27.7',
            'exame_dirigido_outro': 'Sopro sistólico',
            'criterios_alto_risco': ['BNP/NT-proBNP elevado'],
            'condutas_alto_risco': ['antecipar', 'especialista'],
            'etiologia_status': 'confirmada',
            'investigacao_etiologica': ['valvar'],
            'investigacao_valvar_classificacao': 'moderada_importante',
            'investigacao_valvar_conduta': 'heart_team',
            'medicacoes_atuais': 'CARVEDILOL 25 MG',
            'sinais_ic_avancada': ['Hipotensão limitando o tratamento'],
            'condutas_ic_avancada': ['intensificar'],
        },
    ))

    assert '5. EXAME FÍSICO' in text
    assert 'Altura: 1,70' in text
    assert 'IMC: 27.7' in text
    assert 'Sopro sistólico' in text
    assert 'BNP/NT-proBNP elevado' in text
    assert 'Antecipar retorno' in text
    assert 'Discutir com especialista em IC' in text
    assert 'Etiologia da IC: Confirmada.' in text
    assert 'Valvopatia moderada/importante' in text
    assert 'Encaminhar para Heart Team' in text
    assert '9. TRATAMENTO MEDICAMENTOSO' in text
    assert 'CARVEDILOL 25 MG' in text
    assert '15. AVALIAÇÃO PARA IC AVANÇADA' in text
    assert 'Intensificar acompanhamento' in text


def test_ic_v4_renders_improved_feve_with_clinical_label():
    text = render_form(FormInput(
        cd_atendimento=343092,
        linha_cuidado='LC-IC',
        versao='4.0',
        dados={
            'feve': '55',
            'feve_data': '2026-10-01',
            'fenotipo_atual': 'FEVE_MELHORADA',
        },
    ))

    assert 'FEVE: 55% em 2026-10-01.' in text
    assert 'Fenótipo atual: FEVE melhorada.' in text
    assert 'FEVE_MELHORADA' not in text
