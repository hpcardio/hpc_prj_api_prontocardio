from datetime import datetime, timezone

import pytest

from evolucao_sadt_backend.mv_context_token import (
    MvContextTokenError,
    issue_mv_context_token,
    verify_mv_context_token,
)
from evolucao_sadt_backend.prontorede_context import VerifiedMvContext


SECRET = 'test-secret-with-at-least-thirty-two-bytes'
CONTEXT = VerifiedMvContext(365250, 'MEDICO.TESTE', 781, 'CRM-CE 00000')


def test_signed_context_token_round_trip():
    token = issue_mv_context_token(
        CONTEXT,
        SECRET,
        now=datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc),
    )
    claims = verify_mv_context_token(
        token,
        SECRET,
        now=datetime(2026, 10, 22, 12, 1, tzinfo=timezone.utc),
    )
    assert claims.cd_atendimento == 365250
    assert claims.cd_usuario == 'MEDICO.TESTE'
    assert claims.cd_prestador == 781


def test_signed_context_token_rejects_tampering():
    token = issue_mv_context_token(CONTEXT, SECRET)
    with pytest.raises(MvContextTokenError):
        verify_mv_context_token(token + 'x', SECRET)


def test_signed_context_token_rejects_expiration():
    token = issue_mv_context_token(
        CONTEXT,
        SECRET,
        ttl_seconds=60,
        now=datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc),
    )
    with pytest.raises(MvContextTokenError, match='expirado'):
        verify_mv_context_token(
            token,
            SECRET,
            now=datetime(2026, 10, 22, 12, 2, tzinfo=timezone.utc),
        )


def test_signed_context_token_checks_attendance():
    token = issue_mv_context_token(CONTEXT, SECRET)
    claims = verify_mv_context_token(token, SECRET)
    with pytest.raises(MvContextTokenError, match='atendimento'):
        claims.require_attendance(999999)
