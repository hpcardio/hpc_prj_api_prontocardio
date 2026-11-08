import sqlite3

from fastapi import FastAPI
from fastapi.testclient import TestClient

from evolucao_sadt_backend import forms_app
from evolucao_sadt_backend.mv_context_token import MvContextClaims


def payload(base=None, resolution=None, history='Primeira versão'):
    return {
        'cd_atendimento': 339070,
        'linha_cuidado': 'LC-MULTI',
        'versao': '5.0',
        'dados': {
            'schema': 'integrated-care-v1', 'tipo_consulta': 'CAL',
            'linhas_ativas': ['LC-DAC'], 'comum': {'historia_clinica': history},
            'modulos': {'LC-DAC': {'diagnostico': 'DAC'}}, 'exames_solicitados': [],
            'exames_origens': {}, 'navegacao': {'etapa_ativa': 'common:abertura', 'etapas_visitadas': []},
        },
        'base_atualizado_em': base,
        'conflict_resolution': resolution,
    }


def test_stale_save_returns_409_and_authorized_replace_preserves_revision(tmp_path, monkeypatch):
    database = tmp_path / 'forms.sqlite3'
    monkeypatch.setattr(forms_app, 'DATABASE_PATH', database)
    forms_app._initialize_database()
    claims = MvContextClaims(cd_atendimento=339070, cd_usuario='MEDICO.TESTE', cd_prestador=781, crm='CRM-CE 1', expires_at=4102444800)
    forms_app.app.dependency_overrides[forms_app.require_form_write_owner] = lambda: claims
    mounted = FastAPI(); mounted.mount('/formularios', forms_app.app)
    client = TestClient(mounted)
    first = client.put('/formularios/990001', json=payload()).json()
    second = client.put('/formularios/990001', json=payload(first['atualizado_em'], history='Servidor atualizado')).json()
    stale = client.put('/formularios/990001', json=payload(first['atualizado_em'], history='Local antigo'))
    assert stale.status_code == 409
    assert stale.json()['detail']['server_atualizado_em'] == second['atualizado_em']
    replaced = client.put('/formularios/990001', json=payload(first['atualizado_em'], 'replace_with_local', 'Local escolhido'))
    assert replaced.status_code == 200
    with sqlite3.connect(database) as connection:
        assert connection.execute('SELECT COUNT(*) FROM formularios_evolucao_revisoes').fetchone()[0] == 1


def test_database_revision_table_initialization_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setattr(forms_app, 'DATABASE_PATH', tmp_path / 'forms.sqlite3')
    forms_app._initialize_database(); forms_app._initialize_database()
    with forms_app._connect() as connection:
        names = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert 'formularios_evolucao_revisoes' in names


def test_save_starts_write_transaction_before_reading_version(tmp_path, monkeypatch):
    database = tmp_path / 'forms.sqlite3'
    monkeypatch.setattr(forms_app, 'DATABASE_PATH', database)
    forms_app._initialize_database()
    statements = []
    original_connect = forms_app._connect
    def traced_connect():
        connection = original_connect()
        connection.set_trace_callback(statements.append)
        return connection
    monkeypatch.setattr(forms_app, '_connect', traced_connect)
    claims = MvContextClaims(cd_atendimento=339070, cd_usuario='MEDICO.TESTE', cd_prestador=781, crm='CRM-CE 1', expires_at=4102444800)
    forms_app.save_form(990002, forms_app.FormSaveInput(**payload()), claims)
    begin_index = next(index for index, statement in enumerate(statements) if statement == 'BEGIN IMMEDIATE')
    select_index = next(index for index, statement in enumerate(statements) if statement.startswith('SELECT * FROM formularios_evolucao'))
    assert begin_index < select_index
