# ruff: noqa: PLR2004
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app_prontocardio import whatsapp_service
from app_prontocardio.routers import painel_leitos_soulmv as panel
from app_prontocardio.routers import whatsapp
from evolucao_sadt_backend import evolucoes, forms_app
from evolucao_sadt_backend.app import app as context_app
from evolucao_sadt_backend.mv_context_token import (
    MvContextClaims,
    MvContextTokenError,
    issue_mv_context_token,
    verify_mv_context_token,
)
from evolucao_sadt_backend.mv_identity import (
    EvolutionOwnershipError,
    assert_evolution_ownership,
)
from evolucao_sadt_backend.prontorede_context import VerifiedMvContext

SECRET = 'test-context-key-' * 4
NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
CONTEXT = VerifiedMvContext(100, 'MEDICO.TESTE', 200, 'CRM TESTE')
CLAIMS = MvContextClaims(100, 'MEDICO.TESTE', 200, 'CRM TESTE', 9999999999)


def test_context_roundtrip_is_scoped_and_expires():
    token = issue_mv_context_token(CONTEXT, SECRET, now=NOW)
    claims = verify_mv_context_token(token, SECRET, now=NOW)
    claims.require_attendance(100)
    with pytest.raises(MvContextTokenError):
        claims.require_attendance(101)
    with pytest.raises(MvContextTokenError, match='expirado'):
        verify_mv_context_token(token, SECRET, now=NOW + timedelta(minutes=5))
    with pytest.raises(MvContextTokenError):
        verify_mv_context_token(token + 'x', SECRET, now=NOW)
    with pytest.raises(MvContextTokenError):
        verify_mv_context_token(token, 'other-secret-' * 4, now=NOW)


@pytest.mark.parametrize('token', ['', 'x', 'a.b', 'null.x', 'a.b.c'])
def test_context_malformed_is_rejected(token):
    with pytest.raises(MvContextTokenError):
        verify_mv_context_token(token, SECRET)


def test_context_issuance_requires_verified_session(monkeypatch):
    app = context_app
    monkeypatch.setenv('EVOLUCAO_SADT_API_KEY', 'internal-test-key')
    monkeypatch.setenv('EVOLUCAO_SADT_CONTEXT_SECRET', SECRET)
    monkeypatch.setattr(
        app.state, 'prontorede_session_verifier', None, raising=False
    )
    client = TestClient(
        app, headers={'x-evolucao-sadt-key': 'internal-test-key'}
    )
    response = client.post(
        '/api/evolucao/context', json={'cd_atendimento': 100}
    )
    assert response.status_code == 401
    monkeypatch.setattr(
        app.state,
        'prontorede_session_verifier',
        lambda attendance, evidence: (
            CONTEXT if evidence == 'valid-proof' else None
        ),
    )
    response = client.post(
        '/api/evolucao/context',
        json={'cd_atendimento': 100},
        headers={'x-prontorede-mv-session-evidence': 'valid-proof'},
    )
    assert response.status_code == 200, response.text
    assert response.headers['cache-control'] == 'no-store'
    assert (
        verify_mv_context_token(
            response.json()['context_token'], SECRET
        ).cd_prestador
        == 200
    )


@pytest.mark.parametrize(
    ('user', 'provider'), [('OUTRO', 200), ('MEDICO.TESTE', 201)]
)
def test_evolution_requires_both_user_and_provider(user, provider):
    with pytest.raises(EvolutionOwnershipError):
        assert_evolution_ownership(
            evolution_user=user,
            evolution_provider=provider,
            logged_user=CLAIMS.cd_usuario,
            logged_provider=CLAIMS.cd_prestador,
        )


def test_forms_deny_cross_attendance_and_other_author(monkeypatch):
    evolution = SimpleNamespace(
        cd_atendimento=101, usuario_mv='MEDICO.TESTE', cd_prestador=200
    )
    monkeypatch.setattr(forms_app, '_load_evolution', lambda *_: evolution)
    with pytest.raises(HTTPException) as error:
        forms_app.require_form_read_context(1, CLAIMS, object())
    assert error.value.status_code == 409
    evolution.cd_atendimento = 100
    assert forms_app.require_form_read_context(1, CLAIMS, object()) == CLAIMS
    evolution.usuario_mv = 'OUTRO'
    with pytest.raises(HTTPException) as error:
        forms_app.require_form_write_owner(1, CLAIMS, object())
    assert error.value.status_code == 409
    evolution.usuario_mv = CLAIMS.cd_usuario
    assert forms_app.require_form_write_owner(1, CLAIMS, object()) == CLAIMS


def test_form_payload_attendance_checked_before_storage():
    with pytest.raises(HTTPException) as error:
        forms_app.save_form(1, SimpleNamespace(cd_atendimento=101), CLAIMS)
    assert error.value.status_code == 409


def test_form_routes_require_context_token():
    client = TestClient(forms_app.app)
    response = client.get('/lista', params={'cd_atendimento': 100})
    assert response.status_code == 422
    response = client.get(
        '/lista',
        params={'cd_atendimento': 100},
        headers={'X-MV-Context-Token': 'bad'},
    )
    assert response.status_code == 401


def test_create_draft_checks_attendance_before_idempotency(monkeypatch):
    monkeypatch.setattr(evolucoes, '_validar_escrita_habilitada', lambda: None)

    def forbidden(*_args):
        pytest.fail('Database must not be touched for another attendance')

    monkeypatch.setattr(evolucoes, '_reservar_auditoria', forbidden)
    monkeypatch.setattr(evolucoes, 'buscar_contexto', forbidden)
    with pytest.raises(HTTPException) as error:
        evolucoes.criar_evolucao_rascunho(
            SimpleNamespace(cd_atendimento=101),
            'idempotency-test',
            object(),
            object(),
            object(),
            CLAIMS,
        )
    assert error.value.status_code == 403


def test_draft_replay_cannot_return_other_professionals_evolution(monkeypatch):
    monkeypatch.setattr(evolucoes, '_validar_escrita_habilitada', lambda: None)
    context = SimpleNamespace(usuario_mv='MEDICO.TESTE', cd_prestador=200)
    monkeypatch.setattr(evolucoes, '_contexto_do_medico', lambda *_: context)
    monkeypatch.setattr(
        evolucoes,
        '_reservar_auditoria',
        lambda *_: (SimpleNamespace(cd_pre_med=1), True),
    )
    monkeypatch.setattr(
        evolucoes,
        '_obter_evolucao_ou_erro',
        lambda *_: SimpleNamespace(
            cd_atendimento=100, usuario_mv='OUTRO', cd_prestador=201
        ),
    )
    with pytest.raises(HTTPException) as error:
        evolucoes.criar_evolucao_rascunho(
            SimpleNamespace(cd_atendimento=100),
            'idempotency-test',
            object(),
            object(),
            object(),
            CLAIMS,
        )
    assert error.value.status_code == 409


@pytest.mark.parametrize(
    ('function', 'extra'),
    [
        (
            evolucoes.salvar_evolucao,
            {'payload': SimpleNamespace(texto='teste')},
        ),
        (evolucoes.assinar_evolucao_mv, {}),
        (evolucoes.cancelar_evolucao_mv, {}),
    ],
)
def test_mutations_deny_other_author_before_audit(
    monkeypatch, function, extra
):
    monkeypatch.setattr(evolucoes, '_validar_escrita_habilitada', lambda: None)
    monkeypatch.setattr(
        evolucoes,
        '_obter_evolucao_ou_erro',
        lambda *_: SimpleNamespace(
            cd_atendimento=100, usuario_mv='OUTRO', cd_prestador=201
        ),
    )
    monkeypatch.setattr(
        evolucoes,
        '_contexto_do_medico',
        lambda *_: SimpleNamespace(
            usuario_mv='MEDICO.TESTE', cd_prestador=200
        ),
    )

    def forbidden(*_args):
        pytest.fail('Denied writes must not reserve an audit')

    monkeypatch.setattr(evolucoes, '_reservar_auditoria', forbidden)
    with pytest.raises(HTTPException) as error:
        function(
            cd_pre_med=1,
            chave='test-idempotency',
            session_oracle=object(),
            session_postgres=object(),
            usuario=object(),
            identidade=CLAIMS,
            **extra,
        )
    assert error.value.status_code == 409


def test_npr_projection_and_empty_result(monkeypatch):
    calls = []

    def rows(*args):
        calls.append(args)
        return [
            {
                'CD_PRE_MED': 1,
                'CD_ITPRE_MED': 2,
                'DESCRICAO_ITEM': ' Item ',
                'HORARIOS_SEM_FECHAMENTO': 3,
                'MOTIVO_NPR': 'pendencia',
            }
        ]

    monkeypatch.setattr(panel, '_cached_clinical_rows', rows)
    result = panel.pendencias_npr(100)
    assert result['hasPending'] is True
    assert result['pendencies'][0]['openScheduleCount'] == 3
    assert result['pendencies'][0]['description'] == 'Item'
    assert calls[0][1] == 100
    monkeypatch.setattr(panel, '_cached_clinical_rows', lambda *_: [])
    assert panel.pendencias_npr(100)['hasPending'] is False


def test_receipt_supports_both_input_formats_and_rejects_conflicts():
    base = dict(
        telefone='5585999999999',
        nome_template='receipt',
        chave_idempotencia='test-receipt',
    )
    individual = {f'parametro_{i}': f' valor  {i} ' for i in range(1, 8)}
    values = [f'valor {i}' for i in range(1, 8)]
    assert whatsapp.receipt_form(**base, **individual).parametros == values
    assert (
        whatsapp.receipt_form(**base, parametros=json.dumps(values)).parametros
        == values
    )
    for params in [
        {},
        {'parametros': '[]'},
        {'parametros': '{}'},
        {'parametros': json.dumps(['x'] * 6)},
        {'parametros': json.dumps([1] * 7)},
        {'parametros': json.dumps(values), **individual},
    ]:
        with pytest.raises(HTTPException) as error:
            whatsapp.receipt_form(**base, **params)
        assert error.value.status_code == 422


def test_receipt_body_and_generic_jpeg_stay_available(monkeypatch):
    captured = []
    monkeypatch.setattr(
        whatsapp_service, 'upload_whatsapp_media', lambda **_: 'media-1'
    )
    monkeypatch.setattr(
        whatsapp_service,
        'post_graph_messages',
        lambda payload: (
            captured.append(payload) or {'messages': [{'id': 'msg-1'}]}
        ),
    )
    values = [f'value-{i}' for i in range(7)]
    whatsapp_service.enviar_comprovante_whatsapp(
        telefone='5585999999999',
        nome_template='receipt',
        idioma='pt_BR',
        png=whatsapp_service.PNG_SIGNATURE + b'test',
        parametros=values,
    )
    assert [
        item['text']
        for item in captured[0]['template']['components'][1]['parameters']
    ] == values
    whatsapp_service.enviar_midia_template_whatsapp(
        telefone='5585999999999',
        nome_template='generic',
        idioma='pt_BR',
        imagem=bytes.fromhex('ffd8ff') + b'test',
        parametros=['general'],
    )
    assert captured[1]['template']['name'] == 'generic'
    assert any(
        route.path == '/whatsapp/enviar-midia-template'
        for route in whatsapp.router.routes
    )
