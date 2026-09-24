import json
import math
import os
import re
import socket
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


class MvContextVerificationError(PermissionError):
    '''Raised when the MV identity cannot be verified server-side.'''


@dataclass(frozen=True)
class VerifiedMvContext:
    cd_atendimento: int
    cd_usuario: str
    cd_prestador: int
    crm: str


MvSessionVerifier = Callable[[int, str], VerifiedMvContext | None]
ProntoRedeExchangeClient = Callable[
    [str, Mapping[str, str], Mapping[str, int | str], float],
    object,
]

FINALIDADE_PRONTOREDE = 'ATUALIZACAO_PRONTOCARDIO_REDE'
DEFAULT_PRONTOREDE_CONTEXT_TIMEOUT_SECONDS = 5.0
MAX_PRONTOREDE_CONTEXT_TIMEOUT_SECONDS = 30.0
MAX_PRONTOREDE_CONTEXT_RESPONSE_BYTES = 64 * 1024
MAX_PRONTOREDE_CONTEXT_TTL = timedelta(minutes=5)
MAX_FORMULARIO_TOKEN_LENGTH = 2048
HTTPS_DEFAULT_PORT = 443
PRONTOREDE_FORM_PATH = '/mv/atualizacao-prontocardio-rede'
FORMULARIO_TOKEN_PATTERN = re.compile(r'[A-Za-z0-9._~-]+')
SAFE_PRONTOREDE_UPSTREAM_ERRORS = frozenset({
    'Credencial de integração inválida.',
    'Requisição inválida.',
    'Usuário MV sem autorização assistencial.',
    'Contexto clínico divergente.',
    'Falha ao auditar o contexto MV.',
    'Falha ao revalidar o contexto MV.',
})


class ProntoRedeContextUnavailableError(RuntimeError):
    '''Raised when the server-to-server context exchange is unavailable.'''


class ProntoRedeContextInvalidResponseError(RuntimeError):
    '''Raised when ProntoRede returns an invalid short-context response.'''


@dataclass(frozen=True)
class ProntoRedeContextExchange:
    formulario_token: str
    expira_em: str
    url: str


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


def resolve_verified_mv_context(
    cd_atendimento: int,
    session_evidence: str,
    *,
    session_verifier: MvSessionVerifier | None = None,
) -> VerifiedMvContext:
    '''Resolve MV identity only from a configured server-side verifier.

    Browser values are context hints, never authentication. Until Task 2
    configures a verifier for a server-verifiable MV session artifact, this
    resolver rejects every request.
    '''
    if not isinstance(cd_atendimento, int) or isinstance(cd_atendimento, bool):
        raise MvContextVerificationError('Atendimento MV inválido.')
    if cd_atendimento <= 0:
        raise MvContextVerificationError('Atendimento MV inválido.')
    if not isinstance(session_evidence, str) or not session_evidence.strip():
        raise MvContextVerificationError('Evidência de sessão MV inválida.')
    if session_verifier is None:
        raise MvContextVerificationError(
            'Identidade MV não verificável sem verificador de sessão.'
        )

    context = session_verifier(cd_atendimento, session_evidence)
    if context is None or not isinstance(context, VerifiedMvContext):
        raise MvContextVerificationError('Sessão MV não verificável.')
    if context.cd_atendimento != cd_atendimento:
        raise MvContextVerificationError('Atendimento diverge da sessão MV.')
    if (
        not isinstance(context.cd_usuario, str)
        or not context.cd_usuario.strip()
        or not isinstance(context.cd_prestador, int)
        or isinstance(context.cd_prestador, bool)
        or context.cd_prestador <= 0
        or not isinstance(context.crm, str)
        or not context.crm.strip()
    ):
        raise MvContextVerificationError(
            'Sessão MV sem prestador ou CRM verificável.'
        )
    return context


def exchange_verified_mv_context(
    context: VerifiedMvContext,
    *,
    exchange_client: ProntoRedeExchangeClient | None = None,
) -> ProntoRedeContextExchange:
    '''Exchange only server-verified MV claims for a short ProntoRede token.'''
    url = os.getenv('PRONTOREDE_CONTEXT_URL', '').strip()
    issuer_token = os.getenv('PRONTOREDE_CONTEXT_ISSUER_TOKEN', '').strip()
    if not url or not issuer_token:
        raise ProntoRedeContextUnavailableError(
            'Troca de contexto ProntoRede não configurada.'
        )

    expected_origin = _expected_prontorede_origin(url)
    timeout = _context_timeout_from_environment()
    headers = {
        'Authorization': f'Bearer {issuer_token}',
        'Content-Type': 'application/json',
    }
    payload = {
        'atendimentoMv': str(context.cd_atendimento),
        'usuarioMv': context.cd_usuario,
        'prestadorMv': str(context.cd_prestador),
        'crm': context.crm,
        'finalidade': FINALIDADE_PRONTOREDE,
    }
    client = exchange_client or _post_json
    try:
        response = client(url, headers, payload, timeout)
    except (OSError, TimeoutError, URLError, socket.timeout) as exc:
        raise ProntoRedeContextUnavailableError(
            'Troca de contexto ProntoRede indisponível.'
        ) from exc
    return _parse_exchange_response(response, expected_origin)


def _expected_prontorede_origin(url: str) -> str:
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise ProntoRedeContextUnavailableError(
            'Endpoint de contexto ProntoRede inválido.'
        ) from exc
    if (
        parsed.scheme.lower() != 'https'
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        raise ProntoRedeContextUnavailableError(
            'Endpoint de contexto ProntoRede inválido.'
        )
    origin = f'https://{parsed.hostname.lower()}'
    if port and port != HTTPS_DEFAULT_PORT:
        origin = f'{origin}:{port}'
    return origin


def _context_timeout_from_environment() -> float:
    raw_timeout = os.getenv('PRONTOREDE_CONTEXT_TIMEOUT_SECONDS', '').strip()
    if not raw_timeout:
        return DEFAULT_PRONTOREDE_CONTEXT_TIMEOUT_SECONDS
    try:
        timeout = float(raw_timeout)
    except ValueError as exc:
        raise ProntoRedeContextUnavailableError(
            'Timeout de contexto ProntoRede inválido.'
        ) from exc
    if (
        not math.isfinite(timeout)
        or timeout <= 0
        or timeout > MAX_PRONTOREDE_CONTEXT_TIMEOUT_SECONDS
    ):
        raise ProntoRedeContextUnavailableError(
            'Timeout de contexto ProntoRede inválido.'
        )
    return timeout


def _post_json(
    url: str,
    headers: Mapping[str, str],
    payload: Mapping[str, int | str],
    timeout: float,
) -> object:
    request = Request(
        url,
        data=json.dumps(payload).encode('utf-8'),
        headers=dict(headers),
        method='POST',
    )
    try:
        opener = build_opener(_NoRedirectHandler())
        with opener.open(request, timeout=timeout) as response:
            if not (
                HTTPStatus.OK
                <= response.status
                < HTTPStatus.MULTIPLE_CHOICES
            ):
                raise ProntoRedeContextInvalidResponseError(
                    'Resposta de contexto ProntoRede inválida.'
                )
            content_type = response.headers.get_content_type()
            if content_type != 'application/json':
                raise ProntoRedeContextInvalidResponseError(
                    'Resposta de contexto ProntoRede inválida.'
                )
            body = response.read(MAX_PRONTOREDE_CONTEXT_RESPONSE_BYTES + 1)
            if len(body) > MAX_PRONTOREDE_CONTEXT_RESPONSE_BYTES:
                raise ProntoRedeContextInvalidResponseError(
                    'Resposta de contexto ProntoRede inválida.'
                )
            return json.loads(body.decode('utf-8'))
    except HTTPError as exc:
        reason = ''
        try:
            body = exc.read(MAX_PRONTOREDE_CONTEXT_RESPONSE_BYTES + 1)
            if len(body) <= MAX_PRONTOREDE_CONTEXT_RESPONSE_BYTES:
                error_body = json.loads(body.decode('utf-8'))
                candidate = (
                    error_body.get('error')
                    if isinstance(error_body, Mapping)
                    else None
                )
                if candidate in SAFE_PRONTOREDE_UPSTREAM_ERRORS:
                    reason = f' reason={candidate}'
        except (UnicodeDecodeError, json.JSONDecodeError, OSError):
            pass
        raise ProntoRedeContextInvalidResponseError(
            f'ProntoRede upstream status={exc.code}{reason}'
        ) from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProntoRedeContextInvalidResponseError(
            'Resposta de contexto ProntoRede inválida.'
        ) from exc


def _parse_exchange_response(
    response: object,
    expected_origin: str,
) -> ProntoRedeContextExchange:
    if not isinstance(response, Mapping):
        raise ProntoRedeContextInvalidResponseError(
            'Resposta de contexto ProntoRede inválida.'
        )
    values = {
        name: response.get(name)
        for name in ('formulario_token', 'expira_em', 'url')
    }
    if not any(values.values()):
        values = {
            'formulario_token': response.get('formularioToken'),
            'expira_em': response.get('expiraEm'),
            'url': f'{expected_origin}{PRONTOREDE_FORM_PATH}',
        }
    if any(
        not isinstance(value, str) or not value.strip()
        for value in values.values()
    ):
        raise ProntoRedeContextInvalidResponseError(
            'Resposta de contexto ProntoRede inválida.'
        )
    _validate_formulario_token(values['formulario_token'])
    _validate_expiration(values['expira_em'])
    _validate_response_url(values['url'], expected_origin)
    return ProntoRedeContextExchange(**values)


def _validate_formulario_token(token: str) -> None:
    if (
        len(token) > MAX_FORMULARIO_TOKEN_LENGTH
        or not FORMULARIO_TOKEN_PATTERN.fullmatch(token)
    ):
        raise ProntoRedeContextInvalidResponseError(
            'Resposta de contexto ProntoRede inválida.'
        )


def _validate_expiration(expira_em: str) -> None:
    try:
        normalized = (
            f'{expira_em[:-1]}+00:00'
            if expira_em.endswith('Z')
            else expira_em
        )
        expires_at = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ProntoRedeContextInvalidResponseError(
            'Resposta de contexto ProntoRede inválida.'
        ) from exc
    if expires_at.tzinfo is None or expires_at.utcoffset() is None:
        raise ProntoRedeContextInvalidResponseError(
            'Resposta de contexto ProntoRede inválida.'
        )
    ttl = expires_at.astimezone(timezone.utc) - datetime.now(timezone.utc)
    if ttl <= timedelta() or ttl > MAX_PRONTOREDE_CONTEXT_TTL:
        raise ProntoRedeContextInvalidResponseError(
            'Resposta de contexto ProntoRede inválida.'
        )


def _validate_response_url(url: str, expected_origin: str) -> None:
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise ProntoRedeContextInvalidResponseError(
            'Resposta de contexto ProntoRede inválida.'
        ) from exc
    if (
        parsed.scheme.lower() != 'https'
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        raise ProntoRedeContextInvalidResponseError(
            'Resposta de contexto ProntoRede inválida.'
        )
    origin = f'https://{parsed.hostname.lower()}'
    if port and port != HTTPS_DEFAULT_PORT:
        origin = f'{origin}:{port}'
    if origin != expected_origin:
        raise ProntoRedeContextInvalidResponseError(
            'Resposta de contexto ProntoRede inválida.'
        )
