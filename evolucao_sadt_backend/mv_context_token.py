import base64
import hashlib
import hmac
import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from fastapi import Header, HTTPException

from evolucao_sadt_backend.prontorede_context import VerifiedMvContext

TOKEN_PURPOSE = 'EVOLUCAO_SADT_CONTEXT_V1'
DEFAULT_TTL_SECONDS = 5 * 60
MIN_SECRET_BYTES = 32


class MvContextTokenError(PermissionError):
    pass


@dataclass(frozen=True)
class MvContextClaims:
    cd_atendimento: int
    cd_usuario: str
    cd_prestador: int
    crm: str
    expires_at: int

    def require_attendance(self, cd_atendimento: int) -> None:
        if self.cd_atendimento != cd_atendimento:
            raise MvContextTokenError('Contexto pertence a outro atendimento.')


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b'=').decode('ascii')


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + '=' * (-len(value) % 4))


def _secret(value: str) -> bytes:
    if (
        not isinstance(value, str)
        or len(value.encode('utf-8')) < MIN_SECRET_BYTES
    ):
        raise MvContextTokenError('Assinatura do contexto MV indisponível.')
    return value.encode('utf-8')


def issue_mv_context_token(
    context: VerifiedMvContext,
    secret: str,
    *,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    now: datetime | None = None,
) -> str:
    instant = now or datetime.now(timezone.utc)
    expires_at = instant + timedelta(seconds=ttl_seconds)
    payload = {
        'atendimento': context.cd_atendimento,
        'crm': context.crm,
        'exp': int(expires_at.timestamp()),
        'prestador': context.cd_prestador,
        'purpose': TOKEN_PURPOSE,
        'usuario': context.cd_usuario,
    }
    encoded = _b64encode(
        json.dumps(payload, separators=(',', ':'), sort_keys=True).encode()
    )
    signature = _b64encode(
        hmac.new(_secret(secret), encoded.encode(), hashlib.sha256).digest()
    )
    return f'{encoded}.{signature}'


def verify_mv_context_token(
    token: str,
    secret: str,
    *,
    now: datetime | None = None,
) -> MvContextClaims:
    try:
        encoded, signature = token.split('.', 1)
        expected = _b64encode(
            hmac.new(
                _secret(secret), encoded.encode(), hashlib.sha256
            ).digest()
        )
        if not hmac.compare_digest(signature, expected):
            raise MvContextTokenError('Contexto MV inválido.')
        payload = json.loads(_b64decode(encoded))
        claims = MvContextClaims(
            cd_atendimento=int(payload['atendimento']),
            cd_usuario=str(payload['usuario']).strip().upper(),
            cd_prestador=int(payload['prestador']),
            crm=str(payload['crm']).strip(),
            expires_at=int(payload['exp']),
        )
    except MvContextTokenError:
        raise
    except (
        KeyError,
        TypeError,
        ValueError,
        UnicodeError,
        json.JSONDecodeError,
    ) as exc:
        raise MvContextTokenError('Contexto MV inválido.') from exc
    if payload.get('purpose') != TOKEN_PURPOSE:
        raise MvContextTokenError('Contexto MV inválido.')
    timestamp = int((now or datetime.now(timezone.utc)).timestamp())
    if claims.expires_at <= timestamp:
        raise MvContextTokenError('Contexto MV expirado.')
    if (
        claims.cd_atendimento <= 0
        or claims.cd_prestador <= 0
        or not claims.cd_usuario
        or not claims.crm
    ):
        raise MvContextTokenError('Contexto MV inválido.')
    return claims


def require_mv_context(
    token: str = Header(alias='X-MV-Context-Token'),
) -> MvContextClaims:
    try:
        return verify_mv_context_token(
            token,
            os.getenv('EVOLUCAO_SADT_CONTEXT_SECRET', ''),
        )
    except MvContextTokenError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
