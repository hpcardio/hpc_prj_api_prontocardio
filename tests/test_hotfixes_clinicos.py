import sqlite3

from app_prontocardio.routers import painel_leitos_soulmv as painel
from evolucao_sadt_backend import care_support, forms_app, sadt_app


def test_painel_converte_rtf_binario_e_preserva_exame_pendente():
    assert painel._rtf_to_text(b'{\\rtf1 Exame \\b normal\\b0}') == (
        'Exame normal'
    )

    historico = painel._serialize_laboratory_history(
        [
            {
                'TITULO': 'HEMOGRAMA',
                'SITUACAO': 'Não liberado',
                'DATA_SOLICITACAO': '2026-10-13',
            }
        ]
    )

    assert historico == [
        {
            'title': 'HEMOGRAMA',
            'field': 'Resultado',
            'result': None,
            'status': 'Não liberado',
            'requestedAt': '2026-10-13',
            'reportedAt': None,
        }
    ]


def test_prevencao_usa_identificacao_e_prescricao_medicamentosa(monkeypatch):
    monkeypatch.setattr(care_support, 'load_lc_dac_support', lambda *_: {})
    monkeypatch.setattr(
        care_support,
        '_attendance_identification',
        lambda *_: {'paciente': {'valor': 'PACIENTE', 'origem': 'MV'}},
    )
    monkeypatch.setattr(
        care_support,
        '_load_latest_receituario_text',
        lambda *_: 'ATORVASTATINA 40 MG',
    )
    monkeypatch.setattr(
        care_support,
        'latest_previous_form_value',
        lambda *_: '2026-10-01',
    )

    resultado = care_support.load_care_support(object(), 123, 'LC-PREVENCAO')

    assert resultado['campos']['paciente']['valor'] == 'PACIENTE'
    assert resultado['campos']['prescricao_atual']['valor'] == (
        'ATORVASTATINA 40 MG'
    )
    assert resultado['campos']['data_ancora']['valor'] == '2026-10-01'


def test_ultima_data_ancora_ignora_valor_vazio(monkeypatch, tmp_path):
    database = tmp_path / 'formularios.db'
    connection = sqlite3.connect(database)
    connection.execute(
        'CREATE TABLE formularios_evolucao ('
        'cd_atendimento INTEGER, linha_cuidado TEXT, dados_json TEXT, '
        'atualizado_em TEXT)'
    )
    connection.executemany(
        'INSERT INTO formularios_evolucao VALUES (?, ?, ?, ?)',
        [
            (123, 'LC-PREVENCAO', '{"data_ancora":"2026-09-01"}', '1'),
            (123, 'LC-PREVENCAO', '{"data_ancora":""}', '2'),
        ],
    )
    connection.commit()
    connection.close()

    def connect():
        result = sqlite3.connect(database)
        result.row_factory = sqlite3.Row
        return result

    monkeypatch.setattr(forms_app, '_connect', connect)

    assert forms_app.latest_previous_form_value(
        123, 'LC-PREVENCAO', 'data_ancora'
    ) == '2026-09-01'


def test_prevencao_infere_situacao_das_metas():
    texto = forms_app._render_prevent_current(
        {
            'pa_sistolica': '125',
            'pa_diastolica': '75',
            'ldl_resultado': '80',
            'ldl_meta': '70',
            'hba1c_resultado': '6,5',
            'tfg': '55',
        }
    )

    assert 'Situação da pressão arterial: NA META.' in texto
    assert 'Situação do LDL-C: FORA DA META.' in texto
    assert 'Situação da HbA1c: NA META.' in texto
    assert 'Situação da função renal/albuminúria: ALTERADA.' in texto


def test_guia_sem_layout_proprio_usa_padrao_ans():
    assert sadt_app._guide_format(None) == 'ans'
    assert sadt_app._guide_format('outro_convenio') == 'ans'
    assert sadt_app._guide_format('issec') == 'issec'


def test_evolucao_ic_inclui_identificacao_do_ciclo():
    texto = forms_app._render_ic_current(
        {
            'data_ancora': '2026-10-01',
            'tempo': 'cal1',
            'ultima_avaliacao': '2026-10-10',
            'feve': '45',
            'proxima_etapa': 'cir1',
        }
    )

    assert '3. IDENTIFICAÇÃO DO CICLO' in texto
    assert 'Data-âncora: 2026-10-01.' in texto
    assert 'Momento atual da Linha: CAL1.' in texto
    assert 'Próxima etapa prevista: CIR1.' in texto
