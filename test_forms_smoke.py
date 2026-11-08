from fastapi import FastAPI
from fastapi.testclient import TestClient

from evolucao_sadt_backend.forms_app import (
    app as forms_app,
    require_form_read_context,
    require_form_write_owner,
)
from evolucao_sadt_backend.mv_context_token import (
    MvContextClaims,
    require_mv_context,
)


def test_forms_smoke():  # noqa: PLR0914, PLR0915 - end-to-end form flow
    # Overrides are global to forms_app: restore them for the other tests.
    previous = dict(forms_app.dependency_overrides)
    try:
        app = FastAPI()
        test_claims = MvContextClaims(
            cd_atendimento=339070,
            cd_usuario='MEDICO.TESTE',
            cd_prestador=781,
            crm='CRM-CE 00000',
            expires_at=4102444800,
        )
        forms_app.dependency_overrides[require_mv_context] = lambda: test_claims
        forms_app.dependency_overrides[require_form_read_context] = lambda: test_claims
        forms_app.dependency_overrides[require_form_write_owner] = lambda: test_claims
        app.mount('/evolucao/formularios', forms_app)
        client = TestClient(app)
        headers = {'x-evolucao-sadt-key': 'test-key'}

        dac = {
            'cd_atendimento': 339070,
            'linha_cuidado': 'LC-DAC',
            'versao': '1.0',
            'dados': {
                'diagnostico': 'pos_angioplastia_dac',
                'tempo': 'cal1',
                'historia_clinica': 'História para validação local.',
                'aas': 'nao',
                'aas_justificativa': 'anticoagulacao',
                'segundo_antiagregante': 'clopidogrel',
                'meta_dapt': '12 meses',
                'estatina': 'sim',
                'ldl_resultado': '48',
                'pressao_arterial': '120/80 mmHg',
                'pressao_meta': '< 130/80 mmHg',
                'pressao_status': 'no_alvo',
                'hba1c_resultado': '6,4%',
                'hba1c_meta': 'menor_7',
                'hba1c_status': 'no_alvo',
                'tabagismo_status': 'pregresso',
                'carga_tabagica': '20 maços-ano',
                'peso': '82 kg',
                'imc': '28,4 kg/m²',
                'imc_classificacao': 'Sobrepeso',
                'acido_urico': '5,8 mg/dL',
                'acido_urico_data': '2026-08-01',
                'circunferencia_abdominal': '98 cm',
                'esteatose': 'nao',
                'apneia': 'sim',
                'cpap': 'sim',
                'polissonografia': 'Apneia obstrutiva leve',
                'polissonografia_data': '2026-07-20',
                'obesidade_glp1': 'Sem indicação atual',
                'cirurgia_bariatrica': 'Sem indicação atual',
                'atividade_fisica_tipo': 'mista',
                'alimentacao_restricoes': [
                    'sodio',
                    'gordura_saturada',
                    'acucar_adicionado',
                ],
                'conduta': 'Manter acompanhamento.',
            },
        }

        ic = {
            'cd_atendimento': 339070,
            'linha_cuidado': 'LC-IC',
            'versao': '1.0',
            'dados': {
                'diagnostico': 'icfer_melhorada',
                'tempo': 'cal2',
                'historia_clinica': 'História de insuficiência cardíaca.',
                'etiologias': ['isquemica_dac', 'has'],
                'ieca_status': 'sim',
                'ieca_qual': 'Sacubitril/valsartana',
                'ieca_dose': '24/26 mg 12/12h',
                'sglt2_status': 'nao',
                'sglt2_justificativa': 'nefropatia_grave',
                'betabloqueador_status': 'sim',
                'betabloqueador_qual': 'Carvedilol',
                'betabloqueador_dose': '6,25 mg 12/12h',
                'mra_status': 'sim',
                'mra_qual': 'Espironolactona',
                'mra_dose': '25 mg/dia',
                'compensado': 'sim',
                'nyha': 'ii',
                'conduta': 'Manter tratamento e reavaliar.',
            },
        }

        prevent = {
            'cd_atendimento': 339070,
            'linha_cuidado': 'LC-PREVENCAO',
            'versao': '1.0',
            'dados': {
                'diagnosticos': [
                    'hipertensao',
                    'diabetes_tipo_2',
                    'dislipidemia',
                    'obesidade',
                ],
                'tempo': 'cal1',
                'historia_clinica': 'Acompanhamento cardiometabólico.',
                'pa': '128/78 mmHg',
                'fc': '68 bpm',
                'imc': '29,4 kg/m²',
                'hipertensao_tipo': 'essencial',
                'pressao_meta': '< 130/80 mmHg',
                'aterosclerose': 'nao',
                'estatina': 'sim',
                'estatina_qual': 'Rosuvastatina 20 mg/dia',
                'ldl_resultado': '62 mg/dL',
                'diabetes_tipo': 'tipo_2_oral',
                'hba1c_meta': '< 7%',
                'hba1c_resultado': '6,7%',
                'diabetes_terapias': ['metformina', 'sglt2'],
                'esteatose': 'nao',
                'apneia': 'nao',
                'renal_albuminuria': 'Ausente',
                'tfg': '82 mL/min/1,73m²',
                'antecedente_renal': 'nao',
                'prevent_resultado': '8% em 10 anos',
                'risco_global': 'intermediario',
                'pa_alvo': 'sim',
                'lipidico_alvo': 'sim',
                'hba1c_alvo': 'sim',
                'imc_alvo': 'nao',
                'sodio_controlado': 'sim',
                'atividade_fisica': 'nao',
                'tabagismo_ativo_final': 'nao',
                'conduta': 'Manter tratamento e reforçar atividade física.',
            },
        }

        for payload, marker in (
            (dac, 'LC-DAC'),
            (ic, 'LC-IC'),
            (prevent, 'LC-PREVENCAO'),
        ):
            response = client.post(
                '/evolucao/formularios/renderizar',
                headers=headers,
                json=payload,
            )
            assert response.status_code == 200, response.text
            body = response.json()
            assert marker in body['texto_mv']
            assert body['caracteres'] <= 4000
            if marker == 'LC-DAC':
                assert 'Meta: < 130/80 mmHg' in body['texto_mv']
                assert 'Meta: < 7,0%' in body['texto_mv']
                assert 'TABAGISMO' in body['texto_mv']
                assert 'IMC E RISCO METABÓLICO' in body['texto_mv']
                assert 'ALIMENTAÇÃO CARDIOPROTETORA' in body['texto_mv']

        pos_rm = {
            **dac,
            'dados': {**dac['dados'], 'diagnostico': 'pos_rm'},
        }
        pos_rm_response = client.post(
            '/evolucao/formularios/renderizar',
            headers=headers,
            json=pos_rm,
        )
        assert pos_rm_response.status_code == 200, pos_rm_response.text
        assert 'Diagnóstico: Pós-RM.' in pos_rm_response.json()['texto_mv']

        saved = client.put(
            '/evolucao/formularios/999001',
            headers=headers,
            json=dac,
        )
        assert saved.status_code == 200, saved.text
        assert saved.json()['dados']['diagnostico'] == 'pos_angioplastia_dac'

        loaded = client.get('/evolucao/formularios/999001', headers=headers)
        assert loaded.status_code == 200, loaded.text
        assert loaded.json()['texto_mv'] == saved.json()['texto_mv']

        listed = client.get(
            '/evolucao/formularios/lista?cd_atendimento=339070',
            headers=headers,
        )
        assert listed.status_code == 200, listed.text
        assert any(item['cd_pre_med'] == 999001 for item in listed.json()['formularios'])

        unrestricted = client.post(
            '/evolucao/formularios/renderizar',
            headers=headers,
            json={**dac, 'cd_atendimento': 1},
        )
        assert unrestricted.status_code == 200

        from evolucao_sadt_backend.forms_app import SUPPORTED_FORM_VERSIONS
        assert SUPPORTED_FORM_VERSIONS == {'1.0', '2.0', '3.0', '3.1', '4.0', '5.0'}


    finally:
        forms_app.dependency_overrides.clear()
        forms_app.dependency_overrides.update(previous)
