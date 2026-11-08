import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError

from evolucao_sadt_backend.forms_app import (
    FormInput,
    app as forms_app,
    render_form_result,
    require_form_write_owner,
)
from evolucao_sadt_backend.mv_context_token import MvContextClaims


def integrated_payload(**data_overrides):
    data = {
        'schema': 'integrated-care-v1',
        'tipo_consulta': 'CAL',
        'linhas_ativas': ['LC-DAC', 'LC-IC'],
        'comum': {
            'historia_clinica': 'História compartilhada.',
            'pa_sistolica': '128',
            'pa_diastolica': '76',
            'prescricao_atual': 'Losartana 50 mg.',
        },
        'modulos': {
            'LC-DAC': {'diagnostico': 'DAC estável', 'conduta': 'Manter tratamento.'},
            'LC-IC': {'fenotipo_atual': 'ICFEr', 'conduta': 'Manter tratamento.'},
            'LC-PREVENCAO': {'conduta': 'CONTEÚDO INATIVO'},
            'MODULO-INVENTADO': {'conduta': 'NÃO EXECUTAR'},
        },
        'exames_solicitados': ['40301630', '40301630'],
        'exames_origens': {},
        'navegacao': {'etapa_ativa': 'plan:consolidado', 'etapas_visitadas': []},
    }
    data.update(data_overrides)
    return FormInput(
        cd_atendimento=339070,
        linha_cuidado='LC-MULTI',
        versao='5.0',
        dados=data,
    )


def test_integrated_contract_accepts_only_v5_schema():
    assert integrated_payload().linha_cuidado == 'LC-MULTI'
    with pytest.raises(ValidationError):
        FormInput(cd_atendimento=1, linha_cuidado='LC-MULTI', versao='4.0', dados={})
    with pytest.raises(ValidationError):
        FormInput(cd_atendimento=1, linha_cuidado='LC-MULTI', versao='5.0', dados={'schema': 'other'})


def test_consolidated_text_deduplicates_common_plan_exams_and_ignores_inactive():
    result = render_form_result(integrated_payload(), enforce_mv_limit=True)
    text = result.texto_mv
    assert text.count('História compartilhada.') == 1
    assert text.count('Losartana 50 mg.') == 1
    assert text.count('Manter tratamento.') == 1
    assert text.count('40301630') == 1
    assert text.count('LC-DAC') == 1
    assert text.count('LC-IC') == 1
    assert 'CONTEÚDO INATIVO' not in text
    assert 'NÃO EXECUTAR' not in text


def test_integrated_text_renders_shared_structured_data_once_and_builds_plan_from_exceptions():
    payload = integrated_payload()
    payload.dados['comum'] = {
        'sintomas_selecionados': ['Dor torácica', 'Dispneia'],
        'eventos_selecionados': ['Nenhum'],
        'comorbidades_selecionadas': ['HAS', 'Diabetes'],
        'medicacoes_em_uso': 'Losartana 50 mg 2x/dia',
        'pa_sistolica': '150',
        'pa_diastolica': '90',
        'ldl': '82',
        'categoria_risco': 'Alto',
        'conduta_target_pa': 'Intensificar / ajustar tratamento',
        'decisao_clinica': ['Necessita otimização terapêutica'],
        'proximo_acompanhamento': 'Retorno antecipado',
        'conduta_adicional': 'Reavaliar em 14 dias.',
    }
    text = render_form_result(payload, enforce_mv_limit=True).texto_mv
    assert text.count('Dor torácica') == 1
    assert text.count('HAS; Diabetes') == 1
    assert text.count('Losartana 50 mg 2x/dia') == 1
    assert 'PA: 150/90 mmHg' in text
    assert 'LDL-c: 82 mg/dL; categoria de risco: Alto.' in text
    assert text.count('Intensificar / ajustar tratamento') == 1
    assert text.count('Necessita otimização terapêutica') == 1
    assert text.count('Retorno antecipado') == 1


def test_integrated_text_renders_structured_medications_results_and_cal_origin():
    payload = integrated_payload()
    payload.dados['comum'].pop('prescricao_atual', None)
    payload.dados['medicamentos'] = [
        {
            'id': 'mv-atorva', 'codigo': 'atorva', 'farmaco': 'Atorvastatina',
            'posologia': '20 mg 1x/dia', 'classe': 'Estatina', 'origem': 'MV',
            'presente_no_inicio': True, 'acao_consulta': 'titular',
            'ativo_apos_consulta': True,
        },
        {
            'id': 'consulta-dapa', 'codigo': 'dapa', 'farmaco': 'Dapagliflozina',
            'posologia': '10 mg 1x/dia', 'classe': 'iSGLT2', 'origem': 'consulta',
            'presente_no_inicio': False, 'acao_consulta': 'iniciar',
            'ativo_apos_consulta': True,
        },
    ]
    payload.dados['resultados_exames'] = {
        'creatinina': {
            'exame': 'Creatinina', 'valor': '1,4', 'unidade': 'mg/dL',
            'data': '2026-11-01', 'origem': 'MV/RES_EXA',
            'valor_anterior': '1,0', 'data_anterior': '2026-10-05',
            'situacao': 'disponível',
        },
    }
    payload.dados['cal_origem'] = {
        'id': 'CAL-123', 'data': '2026-10-05',
        'condutas': ['Reavaliar função renal'],
        'exames': [{'codigo': '40301630', 'situacao': 'resultado_disponivel'}],
    }
    text = render_form_result(payload, enforce_mv_limit=True).texto_mv
    assert 'Atorvastatina 20 mg 1x/dia' in text
    assert 'Titular Atorvastatina 20 mg 1x/dia.' in text
    assert 'Iniciar Dapagliflozina 10 mg 1x/dia.' in text
    assert 'Creatinina: 1,4 mg/dL (01/11/2026, MV/RES_EXA); anterior: 1,0 mg/dL (05/10/2026).' in text
    assert 'CAL de origem: 05/10/2026 (CAL-123).' in text
    assert 'Condutas da CAL: Reavaliar função renal.' in text


def test_integrated_text_does_not_repeat_legacy_medication_when_structured_list_exists():
    payload = integrated_payload()
    payload.dados['comum']['prescricao_atual'] = 'Atorvastatina 20 mg 1x/dia'
    payload.dados['medicamentos'] = [
        {
            'id': 'mv-atorva', 'farmaco': 'Atorvastatina',
            'posologia': '20 mg 1x/dia', 'classe': 'Estatina', 'origem': 'MV',
            'presente_no_inicio': True, 'ativo_apos_consulta': True,
        },
    ]
    text = render_form_result(payload, enforce_mv_limit=True).texto_mv
    assert text.count('MEDICAÇÕES ATUAIS') == 1
    assert text.count('Atorvastatina 20 mg 1x/dia') == 1


def test_integrated_text_includes_alarm_specific_justification():
    payload = integrated_payload()
    payload.dados['comum']['risco_dor_toracica_evolucao'] = 'Novo ou em piora (semanas a meses)'
    payload.dados['justificativas_alarmes'] = {
        'angina-change': {
            'motivo': 'Risco avaliado e conduta ambulatorial segura',
            'complemento': 'ECG sem alteração dinâmica; retorno em 48 horas.',
        }
    }
    text = render_form_result(payload, enforce_mv_limit=True).texto_mv
    assert 'Dor torácica nova ou em piora' in text
    assert 'Risco avaliado e conduta ambulatorial segura' in text
    assert 'ECG sem alteração dinâmica; retorno em 48 horas.' in text


def test_integrated_text_omits_justification_when_alarm_is_no_longer_active():
    payload = integrated_payload()
    payload.dados['comum']['nyha'] = 'III'
    payload.dados['justificativas_alarmes'] = {
        'nyha-iv': {'motivo': 'Achado já conhecido e estável'}
    }
    text = render_form_result(payload, enforce_mv_limit=True).texto_mv
    assert 'JUSTIFICATIVAS DOS ALARMES CLÍNICOS' not in text
    assert 'Achado já conhecido e estável' not in text


def test_oversized_draft_is_rendered_but_send_is_rejected():
    payload = integrated_payload(comum={'historia_clinica': 'X' * 4100})
    draft = render_form_result(payload, enforce_mv_limit=False)
    assert draft.excede_limite is True
    assert draft.caracteres > 4000
    with pytest.raises(HTTPException, match='ultrapassa'):
        render_form_result(payload, enforce_mv_limit=True)


def test_oversized_v5_draft_is_persisted_with_limit_metadata():
    claims = MvContextClaims(
        cd_atendimento=339070,
        cd_usuario='MEDICO.TESTE',
        cd_prestador=781,
        crm='CRM-CE 00000',
        expires_at=4102444800,
    )
    forms_app.dependency_overrides[require_form_write_owner] = lambda: claims
    mounted = FastAPI()
    mounted.mount('/formularios', forms_app)
    client = TestClient(mounted)
    payload = integrated_payload(comum={'historia_clinica': 'X' * 4100})
    response = client.put('/formularios/999502', json=payload.model_dump())
    assert response.status_code == 200, response.text
    assert response.json()['excede_limite'] is True
    assert response.json()['caracteres'] > 4000

