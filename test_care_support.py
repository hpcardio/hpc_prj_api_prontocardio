from datetime import date
import json

import pytest

from evolucao_sadt_backend import care_support
from evolucao_sadt_backend import forms_app


RAW = {
    'cd_atendimento': 343092,
    'medicacoes': {'estatina': 'sim'},
    'itens_ultima_prescricao': ['LOSARTANA 50 MG'],
    'ldl': {'valor': '82', 'data': date(2026, 9, 1), 'fonte': 'MV/RES_EXA'},
    'hba1c': {'valor': '6.8', 'data': None, 'fonte': 'MV/RES_EXA'},
    'exames_protocolares': {
        'Creatinina': {'valor': '1.1', 'data': date(2026, 9, 2), 'fonte': 'MV/RES_EXA'},
    },
}


class _Mappings:
    def __init__(self, row):
        self.row = row

    def first(self):
        return self.row

    def all(self):
        if self.row is None:
            return []
        return self.row if isinstance(self.row, list) else [self.row]


class _Result:
    def __init__(self, row):
        self.row = row

    def mappings(self):
        return _Mappings(self.row)


class _Session:
    def __init__(self, row=None):
        self.row = row

    def execute(self, *_args, **_kwargs):
        return _Result(self.row)


class _ReceituarioSession:
    def __init__(self):
        self.statement = ''

    def execute(self, statement, _params):
        self.statement = str(statement)

        class _Rows:
            @staticmethod
            def mappings():
                class _MappedRows:
                    @staticmethod
                    def all():
                        return [
                            {'ds_valor': 'LOSARTANA 50 MG 1 CP/DIA', 'lo_valor': None},
                            {'ds_valor': None, 'lo_valor': 'ATORVASTATINA 40 MG À NOITE'},
                        ]

                return _MappedRows()

        return _Rows()


def test_preserves_lc_dac_response_contract_and_adds_cal_origin(monkeypatch):
    monkeypatch.setattr(care_support, 'load_lc_dac_support', lambda *_: RAW)
    result = care_support.load_care_support(object(), 343092, 'LC-DAC')
    assert {key: result[key] for key in RAW} == RAW
    assert result['cal_origem'] is None


def test_dispatches_prevention_values_with_dates_and_provenance(monkeypatch):
    monkeypatch.setattr(care_support, 'load_lc_dac_support', lambda *_: RAW)
    monkeypatch.setattr(
        care_support,
        '_load_latest_receituario_text',
        lambda *_: 'LOSARTANA 50 MG 1 CP/DIA',
        raising=False,
    )
    monkeypatch.setattr(
        care_support,
        'latest_previous_form_value',
        lambda *_: '2026-09-01',
        raising=False,
    )
    result = care_support.load_care_support(_Session(), 343092, 'LC-PREVENCAO')
    assert result['linha_cuidado'] == 'LC-PREVENCAO'
    assert result['campos']['prescricao_atual'] == {
        'valor': 'LOSARTANA 50 MG 1 CP/DIA',
        'origem': 'MV - último receituário fechado',
    }
    assert result['campos']['data_ancora'] == {
        'valor': '2026-09-01',
        'origem': 'Última evolução estruturada',
    }
    assert result['exames']['ldl']['data'] == '2026-09-01'
    assert result['exames']['ldl']['origem'] == 'MV/RES_EXA'
    assert result['exames']['hba1c']['data'] is None


def test_prevention_does_not_treat_requested_exams_as_current_medications(monkeypatch):
    raw_with_requested_exams = {
        **RAW,
        'itens_ultima_prescricao': [
            'ECG CONVENCIONAL DE ATE 12 DERIVACOES',
            'HEMOGRAMA COMPLETO',
            'CREATININA',
        ],
    }
    monkeypatch.setattr(
        care_support,
        'load_lc_dac_support',
        lambda *_: raw_with_requested_exams,
    )
    monkeypatch.setattr(
        care_support,
        '_load_latest_receituario_text',
        lambda *_: 'LOSARTANA 50 MG\nATORVASTATINA 40 MG',
        raising=False,
    )
    monkeypatch.setattr(care_support, 'latest_previous_form_value', lambda *_: None, raising=False)

    result = care_support.load_care_support(
        _Session(), 343092, 'LC-PREVENCAO'
    )

    assert result['campos']['prescricao_atual']['valor'] == (
        'LOSARTANA 50 MG\nATORVASTATINA 40 MG'
    )


def test_current_medications_come_from_latest_closed_mv_receituario():
    session = _ReceituarioSession()

    result = care_support._load_latest_receituario_text(session, 343092)

    assert result == 'LOSARTANA 50 MG 1 CP/DIA\nATORVASTATINA 40 MG À NOITE'
    assert 'PW_DOCUMENTO_CLINICO' in session.statement
    assert 'DS_RECEITUARIO_LIVRE' in session.statement
    assert 'CD_OBJETO = 529' in session.statement


def test_missing_or_malformed_data_remains_absent(monkeypatch):
    malformed = {**RAW, 'ldl': {'valor': 'não numérico', 'data': date(2026, 9, 1)}}
    monkeypatch.setattr(care_support, 'load_lc_dac_support', lambda *_: malformed)
    monkeypatch.setattr(care_support, '_load_latest_receituario_text', lambda *_: None)
    monkeypatch.setattr(care_support, 'latest_previous_form_value', lambda *_: None)
    result = care_support.load_care_support(_Session(), 343092, 'LC-IC')
    assert 'ldl' not in result['exames']
    assert 'feve' not in result['campos']


@pytest.mark.parametrize(
    ('report_text', 'expected'),
    (
        ('Fração de ejeção do ventrículo esquerdo (Simpson): 38%', '38'),
        ('FEVE = 55 %', '55'),
        ('Fração de ejeção: 41,5%', '41.5'),
    ),
)
def test_extracts_only_explicit_feve_from_echo_report(report_text, expected):
    assert care_support.extract_feve_from_report(report_text) == expected


@pytest.mark.parametrize(
    'report_text',
    (
        'Átrio esquerdo 42%. Função sistólica preservada.',
        'FEVE não mensurável no estudo atual.',
        'Fração de encurtamento: 35%.',
    ),
)
def test_does_not_infer_feve_from_unrelated_or_missing_percentages(report_text):
    assert care_support.extract_feve_from_report(report_text) is None


def test_lc_ic_loads_latest_explicit_feve_from_mv_echo_report(monkeypatch):
    monkeypatch.setattr(care_support, 'load_lc_dac_support', lambda *_: RAW)
    monkeypatch.setattr(care_support, '_attendance_identification', lambda *_: {})
    monkeypatch.setattr(care_support, '_load_latest_receituario_text', lambda *_: None)
    monkeypatch.setattr(care_support, 'latest_previous_form_value', lambda *_: None)
    monkeypatch.setattr(
        care_support,
        '_extract_pdf_text',
        lambda value: value.decode('utf-8'),
        raising=False,
    )
    session = _Session([
        {
            'laudo_pdf': b'FEVE = 38%',
            'dt_laudo': date(2026, 10, 1),
            'nome_exame': 'ECODOPPLERCARDIOGRAMA TRANSTORACICO',
        }
    ])

    result = care_support.load_care_support(session, 343092, 'LC-IC')

    assert result['campos']['feve'] == {
        'valor': '38',
        'data': '2026-10-01',
        'origem': 'MV - laudo de ecocardiograma transtorácico',
    }
    assert result['campos']['feve_data'] == {
        'valor': '2026-10-01',
        'origem': 'MV - laudo de ecocardiograma transtorácico',
    }


def test_lc_ic_keeps_manual_feve_available_when_report_query_fails(monkeypatch):
    monkeypatch.setattr(care_support, 'load_lc_dac_support', lambda *_: RAW)
    monkeypatch.setattr(care_support, '_attendance_identification', lambda *_: {})
    monkeypatch.setattr(care_support, '_load_latest_receituario_text', lambda *_: None)
    monkeypatch.setattr(care_support, 'latest_previous_form_value', lambda *_: None)
    monkeypatch.setattr(
        care_support,
        '_load_latest_echo_feve',
        lambda *_: (_ for _ in ()).throw(RuntimeError('laudos indisponíveis')),
    )

    result = care_support.load_care_support(_Session(), 343092, 'LC-IC')

    assert 'feve' not in result['campos']
    assert result['alertas'] == [
        'FEVE automática indisponível; informe manualmente.'
    ]
    assert result['fontes_indisponiveis'] == ['laudo_ecocardiograma']


def test_identifies_patient_and_consultation_date_from_open_mv_attendance(monkeypatch):
    monkeypatch.setattr(care_support, 'load_lc_dac_support', lambda *_: RAW)
    monkeypatch.setattr(care_support, '_load_latest_receituario_text', lambda *_: None)
    monkeypatch.setattr(care_support, 'latest_previous_form_value', lambda *_: None)
    session = _Session({
        'nm_paciente': 'MARIA TESTE',
        'dt_atendimento': date(2026, 10, 10),
    })

    result = care_support.load_care_support(session, 343092, 'LC-IC')

    assert result['campos']['paciente'] == {
        'valor': 'MARIA TESTE',
        'origem': 'MV - atendimento aberto',
    }
    assert result['campos']['data_consulta'] == {
        'valor': '2026-10-10',
        'origem': 'MV - atendimento aberto',
    }


def test_lc_ic_loads_medications_anchor_and_previous_phenotype(monkeypatch):
    monkeypatch.setattr(care_support, 'load_lc_dac_support', lambda *_: RAW)
    monkeypatch.setattr(care_support, '_attendance_identification', lambda *_: {})
    monkeypatch.setattr(
        care_support,
        '_load_latest_receituario_text',
        lambda *_: 'SACUBITRIL/VALSARTANA 49/51 MG\nCARVEDILOL 25 MG',
    )
    previous = {
        'data_ancora': '2026-08-01',
        'fenotipo_atual': 'ICFEr',
    }
    monkeypatch.setattr(
        care_support,
        'latest_previous_form_value',
        lambda _attendance, _line, field: previous.get(field),
    )

    result = care_support.load_care_support(_Session(), 343092, 'LC-IC')

    assert result['campos']['medicacoes_atuais'] == {
        'valor': 'SACUBITRIL/VALSARTANA 49/51 MG\nCARVEDILOL 25 MG',
        'origem': 'MV - último receituário fechado',
    }
    assert result['campos']['data_ancora'] == {
        'valor': '2026-08-01',
        'origem': 'Última evolução estruturada',
    }
    assert result['campos']['fenotipo_anterior'] == {
        'valor': 'ICFEr',
        'origem': 'Última evolução estruturada',
    }
    assert 'prescricao_atual' not in result['campos']
    assert result['medicamentos'] == [
        {
            'id': 'mv-receituario-1',
            'texto': 'SACUBITRIL/VALSARTANA 49/51 MG',
            'origem': 'MV',
            'chave_origem': 'receituario:1',
        },
        {
            'id': 'mv-receituario-2',
            'texto': 'CARVEDILOL 25 MG',
            'origem': 'MV',
            'chave_origem': 'receituario:2',
        },
    ]


def test_support_exposes_additive_v9_results_and_empty_origin_contract(monkeypatch):
    monkeypatch.setattr(care_support, 'load_lc_dac_support', lambda *_: RAW)
    monkeypatch.setattr(care_support, '_attendance_identification', lambda *_: {})
    monkeypatch.setattr(care_support, '_load_latest_receituario_text', lambda *_: None)
    monkeypatch.setattr(care_support, 'latest_previous_form_value', lambda *_: None)

    result = care_support.load_care_support(_Session(), 343092, 'LC-PREVENCAO')

    assert result['resultados_exames']['ldl'] == {
        'exame': 'ldl',
        'valor': '82',
        'data': '2026-09-01',
        'origem': 'MV/RES_EXA',
        'situacao': 'resultado_disponivel',
    }
    assert result['eventos_assistenciais'] == {
        'internacoes_ic_6m': None,
        'emergencias_12m': None,
    }
    assert result['cal_origem'] is None
    assert result['fontes_indisponiveis'] == []


def test_support_loads_latest_cal_origin_for_all_care_lines(monkeypatch):
    origin = {
        'id': '991', 'data': '2026-10-05', 'momento': 'cal1',
        'condutas': ['Reavaliar função renal'],
        'exames': [{'codigo': '40301630', 'situacao': 'aguardando'}],
    }
    monkeypatch.setattr(care_support, 'load_lc_dac_support', lambda *_: {**RAW, 'campos': {}})
    monkeypatch.setattr(care_support, '_attendance_identification', lambda *_: {})
    monkeypatch.setattr(care_support, '_load_latest_receituario_text', lambda *_: None)
    monkeypatch.setattr(care_support, 'latest_previous_form_value', lambda *_: None)
    monkeypatch.setattr(care_support, '_load_latest_cal_origin', lambda *_: origin)

    dac = care_support.load_care_support(_Session(), 343092, 'LC-DAC')
    prevention = care_support.load_care_support(_Session(), 343092, 'LC-PREVENCAO')

    assert dac['cal_origem'] == origin
    assert prevention['cal_origem'] == origin


def test_latest_integrated_cal_origin_reads_patient_cal_and_preserves_moment(monkeypatch, tmp_path):
    monkeypatch.setattr(forms_app, 'DATABASE_PATH', tmp_path / 'forms.sqlite3')
    forms_app._initialize_database()
    data = {
        'schema': 'integrated-care-v1', 'tipo_consulta': 'CAL',
        'linhas_ativas': ['LC-DAC'],
        'comum': {
            'data_consulta': '2026-10-05',
            'conduta_target_pa': 'Ajustar tratamento',
        },
        'modulos': {'LC-DAC': {'tempo': 'cal1'}},
        'medicamentos': [],
        'exames_solicitados': ['40301630'],
    }
    with forms_app._connect() as connection:
        connection.execute(
            '''
            INSERT INTO formularios_evolucao (
                cd_pre_med, cd_atendimento, linha_cuidado, versao,
                dados_json, texto_mv, texto_hash, copiado_de_cd_pre_med,
                criado_em, atualizado_em
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''',
            (991, 343091, 'LC-MULTI', '5.0', json.dumps(data), '', '', None,
             '2026-10-05T10:00:00+00:00', '2026-10-05T10:00:00+00:00'),
        )

    assert forms_app.latest_integrated_cal_origin([343092, 343091], 'LC-DAC') == {
        'id': '991', 'data': '2026-10-05', 'momento': 'cal1',
        'condutas': ['Ajustar tratamento'],
        'exames': [{'codigo': '40301630', 'situacao': 'aguardando'}],
    }


def test_rejects_unknown_line_without_querying_oracle(monkeypatch):
    called = False

    def loader(*_):
        nonlocal called
        called = True
        return RAW

    monkeypatch.setattr(care_support, 'load_lc_dac_support', loader)
    with pytest.raises(ValueError, match='Linha de cuidado inválida'):
        care_support.load_care_support(object(), 343092, 'LC-OUTRA')
    assert called is False


def test_latest_previous_anchor_uses_newest_non_empty_structured_form(monkeypatch, tmp_path):
    monkeypatch.setattr(forms_app, 'DATABASE_PATH', tmp_path / 'forms.sqlite3')
    forms_app._initialize_database()
    with forms_app._connect() as connection:
        for cd_pre_med, anchor, updated_at in (
            (10, '2026-01-15', '2026-01-15T10:00:00+00:00'),
            (11, '', '2026-02-15T10:00:00+00:00'),
        ):
            connection.execute(
                '''
                INSERT INTO formularios_evolucao (
                    cd_pre_med, cd_atendimento, linha_cuidado, versao,
                    dados_json, texto_mv, texto_hash, copiado_de_cd_pre_med,
                    criado_em, atualizado_em
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''',
                (
                    cd_pre_med, 343092, 'LC-PREVENCAO', '4.0',
                    json.dumps({'data_ancora': anchor}), '', '', None,
                    updated_at, updated_at,
                ),
            )

    assert forms_app.latest_previous_form_value(
        343092, 'LC-PREVENCAO', 'data_ancora'
    ) == '2026-01-15'
