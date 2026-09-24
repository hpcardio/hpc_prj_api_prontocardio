import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response

from app_prontocardio.models import Usuario
from app_prontocardio.routers.agendamentos import (
    garantir_paciente_autorizado,
    valida_acesso_agendamento,
)
from app_prontocardio.routers.paciente_auth import PrincipalPaciente

router = APIRouter(prefix='/resultados', tags=['resultados'])

NVC_BASE_URL = os.getenv(
    'NVC_BASE_URL',
    'https://nvc.hospitalprontocardio.com.br',
).rstrip('/')
_NVC_TOKEN_TTL_SEGUNDOS = 6 * 60 * 60
_nvc_token: str | None = None
_nvc_token_expira_em = 0.0
_nvc_token_lock = threading.Lock()
ValidaUsuarioAtual = Annotated[
    Usuario | PrincipalPaciente,
    Depends(valida_acesso_agendamento),
]


def _autenticar_nvc() -> str:
    email = os.getenv('NVC_INTEGRATION_EMAIL', '').strip()
    senha = os.getenv('NVC_INTEGRATION_PASSWORD', '').strip()
    if not email or not senha:
        raise HTTPException(
            status_code=503,
            detail='A integração do serviço de resultados não está configurada.',
        )

    payload = json.dumps(
        {'email': email, 'password': senha, 'rememberMe': True}
    ).encode('utf-8')
    request = urllib.request.Request(
        f'{NVC_BASE_URL}/api/auth/sign-in/email',
        data=payload,
        method='POST',
        headers={
            'Accept': 'application/json',
            'Content-Type': 'application/json',
            'Origin': NVC_BASE_URL,
            'User-Agent': 'API-ProntoCardio/1.0',
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            token = str(response.headers.get('set-auth-token') or '').strip()
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as exc:
        raise HTTPException(
            status_code=503,
            detail='Não foi possível autenticar a integração de resultados.',
        ) from exc
    if not token:
        raise HTTPException(
            status_code=503,
            detail='O serviço de resultados não forneceu um token de integração.',
        )
    return token


def _obter_token_nvc(*, renovar: bool = False) -> str:
    global _nvc_token, _nvc_token_expira_em
    agora = time.monotonic()
    if not renovar and _nvc_token and agora < _nvc_token_expira_em:
        return _nvc_token
    with _nvc_token_lock:
        agora = time.monotonic()
        if not renovar and _nvc_token and agora < _nvc_token_expira_em:
            return _nvc_token
        _nvc_token = _autenticar_nvc()
        _nvc_token_expira_em = agora + _NVC_TOKEN_TTL_SEGUNDOS
        return _nvc_token


def _invalidar_token_nvc() -> None:
    global _nvc_token, _nvc_token_expira_em
    with _nvc_token_lock:
        _nvc_token = None
        _nvc_token_expira_em = 0.0


def _request_nvc(path: str, timeout: int = 45) -> tuple[bytes, str]:
    for tentativa in range(2):
        token = _obter_token_nvc(renovar=tentativa > 0)
        request = urllib.request.Request(
            f'{NVC_BASE_URL}{path}',
            headers={
                'Accept': 'application/json, application/pdf',
                'Authorization': f'Bearer {token}',
                'User-Agent': 'API-ProntoCardio/1.0',
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read(), response.headers.get_content_type()
        except urllib.error.HTTPError as exc:
            if exc.code == 401 and tentativa == 0:
                _invalidar_token_nvc()
                continue
            detail = 'O serviço de resultados não conseguiu concluir a consulta.'
            try:
                upstream = json.loads(exc.read().decode('utf-8'))
                detail = upstream.get('error') or upstream.get('detail') or detail
            except (UnicodeDecodeError, json.JSONDecodeError):
                pass
            raise HTTPException(status_code=502, detail=detail) from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise HTTPException(
                status_code=503,
                detail=(
                    'O serviço de resultados está temporariamente indisponível.'
                ),
            ) from exc
    raise HTTPException(
        status_code=503,
        detail='Não foi possível renovar a autenticação do serviço de resultados.',
    )


def _json_nvc(path: str, timeout: int = 45) -> dict:
    content, _ = _request_nvc(path, timeout=timeout)
    try:
        data = json.loads(content.decode('utf-8'))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(
            status_code=502,
            detail='O serviço de resultados retornou uma resposta inválida.',
        ) from exc
    if not isinstance(data, dict):
        raise HTTPException(
            status_code=502,
            detail='O serviço de resultados retornou um formato inesperado.',
        )
    return data


def _garantir_exame_autorizado(
    usuario_atual: Usuario | PrincipalPaciente,
    id_exame_pedido: int,
) -> None:
    if not isinstance(usuario_atual, PrincipalPaciente):
        return
    query = urllib.parse.urlencode({'paciente_id': usuario_atual.cd_paciente})
    data = _json_nvc(f'/api/mvsoul/laudo?{query}')
    resultados = data.get('results') if isinstance(data, dict) else None
    autorizado = any(
        int(item.get('id_exame_pedido') or 0) == id_exame_pedido
        for item in (resultados or [])
        if isinstance(item, dict)
    )
    if not autorizado:
        raise HTTPException(
            status_code=404,
            detail='Exame não encontrado para o paciente autenticado.',
        )


@router.get('/pacientes/{cd_paciente}/laudos')
def consultar_laudos_paciente(
    cd_paciente: int,
    usuario_atual: ValidaUsuarioAtual,
):
    garantir_paciente_autorizado(usuario_atual, cd_paciente)
    if cd_paciente <= 0:
        raise HTTPException(
            status_code=422, detail='Código do paciente inválido.'
        )
    query = urllib.parse.urlencode({'paciente_id': cd_paciente})
    return _json_nvc(f'/api/mvsoul/laudo?{query}')


@router.get('/laudos/{id_exame_pedido}/pdf')
def abrir_laudo_pdf(
    id_exame_pedido: int,
    usuario_atual: ValidaUsuarioAtual,
):
    _garantir_exame_autorizado(usuario_atual, id_exame_pedido)
    content, content_type = _request_nvc(
        f'/api/mvsoul/laudo/pdf/{id_exame_pedido}',
    )
    if content_type != 'application/pdf' or not content.startswith(b'%PDF'):
        raise HTTPException(
            status_code=502,
            detail='O laudo não está disponível em PDF.',
        )
    return Response(
        content=content,
        media_type='application/pdf',
        headers={
            'Content-Disposition': (
                f'inline; filename="laudo-{id_exame_pedido}.pdf"'
            )
        },
    )


@router.get('/laudos/{id_exame_pedido}/texto')
def consultar_laudo_texto(
    id_exame_pedido: int,
    usuario_atual: ValidaUsuarioAtual,
):
    _garantir_exame_autorizado(usuario_atual, id_exame_pedido)
    return _json_nvc(f'/api/mvsoul/laudo/{id_exame_pedido}/text')


@router.get('/imagens/{id_exame_pedido}/visualizador')
def abrir_visualizador_imagens(
    id_exame_pedido: int,
    usuario_atual: ValidaUsuarioAtual,
):
    _garantir_exame_autorizado(usuario_atual, id_exame_pedido)
    data = _json_nvc(
        f'/api/mvsoul/pacs/{id_exame_pedido}/visualizador',
        timeout=60,
    )
    link = str(data.get('link') or '')
    parsed = urllib.parse.urlparse(link)
    if (
        parsed.scheme != 'https'
        or not parsed.hostname
        or not parsed.hostname.endswith('cloudmv.com.br')
    ):
        raise HTTPException(
            status_code=502,
            detail='O visualizador de imagens retornou um endereço inválido.',
        )
    return {'link': link}
