import pytest
from types import SimpleNamespace
from fastapi import HTTPException

from evolucao_sadt_backend.mv_identity import (
    EvolutionOwnershipError,
    assert_evolution_ownership,
    normalize_mv_user,
)
from evolucao_sadt_backend.mv_context_token import MvContextClaims
from evolucao_sadt_backend import forms_app


def test_normalize_mv_user_accepts_logged_physician_identifier():
    assert normalize_mv_user(" medico.teste ") == "MEDICO.TESTE"


@pytest.mark.parametrize("value", ["", "medico teste", "A", "x" * 31])
def test_normalize_mv_user_rejects_invalid_identifiers(value):
    with pytest.raises(ValueError, match="Usuário MV inválido"):
        normalize_mv_user(value)


def test_evolution_actions_reject_a_document_owned_by_another_physician():
    with pytest.raises(EvolutionOwnershipError, match="outro profissional"):
        assert_evolution_ownership(
            evolution_user="MEDICO.A",
            evolution_provider=101,
            logged_user="MEDICO.B",
            logged_provider=202,
        )


def test_evolution_actions_accept_the_logged_physician_as_owner():
    assert_evolution_ownership(
        evolution_user="MEDICO.A",
        evolution_provider=101,
        logged_user="medico.a",
        logged_provider=101,
    )


def test_structured_form_autosave_rejects_another_physician(monkeypatch):
    monkeypatch.setattr(
        forms_app,
        '_load_evolution',
        lambda _session, _code: SimpleNamespace(
            cd_atendimento=365250,
            usuario_mv='MEDICO.OUTRO',
            cd_prestador=999,
        ),
    )
    claims = MvContextClaims(
        cd_atendimento=365250,
        cd_usuario='MEDICO.TESTE',
        cd_prestador=781,
        crm='CRM-CE 00000',
        expires_at=4102444800,
    )
    with pytest.raises(HTTPException) as error:
        forms_app.require_form_write_owner(123, claims, object())
    assert error.value.status_code == 409


def test_structured_form_read_rejects_another_attendance(monkeypatch):
    monkeypatch.setattr(
        forms_app,
        '_load_evolution',
        lambda _session, _code: SimpleNamespace(cd_atendimento=999999),
    )
    claims = MvContextClaims(
        cd_atendimento=365250,
        cd_usuario='MEDICO.TESTE',
        cd_prestador=781,
        crm='CRM-CE 00000',
        expires_at=4102444800,
    )
    with pytest.raises(HTTPException) as error:
        forms_app.require_form_read_context(123, claims, object())
    assert error.value.status_code == 409
