import json
import uuid
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request as UrlRequest
from urllib.request import urlopen

from fastapi import HTTPException, status

from app_prontocardio.settings import Settings

settings = Settings()
TELEFONE_E164_MIN_LENGTH = 10
TELEFONE_E164_MAX_LENGTH = 15
PNG_SIGNATURE = b'\x89PNG\r\n\x1a\n'


def whatsapp_config() -> tuple[str, str, str]:
    if not settings.WHATSAPP_PHONE_NUMBER_ID:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail='WHATSAPP_PHONE_NUMBER_ID nao configurado.',
        )
    if not settings.WHATSAPP_ACCESS_TOKEN:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail='WHATSAPP_ACCESS_TOKEN nao configurado.',
        )

    return (
        settings.WHATSAPP_GRAPH_API_VERSION,
        settings.WHATSAPP_PHONE_NUMBER_ID,
        settings.WHATSAPP_ACCESS_TOKEN,
    )


def normalizar_telefone_whatsapp(telefone: str) -> str:
    apenas_digitos = ''.join(ch for ch in telefone if ch.isdigit())
    if (
        len(apenas_digitos) < TELEFONE_E164_MIN_LENGTH
        or len(apenas_digitos) > TELEFONE_E164_MAX_LENGTH
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail='Telefone invalido. Use DDI + DDD + numero, sem +.',
        )
    return apenas_digitos


def post_graph_messages(payload: dict[str, Any]) -> dict[str, Any]:
    versao, phone_number_id, token = whatsapp_config()
    url = f'https://graph.facebook.com/{versao}/{phone_number_id}/messages'
    body = json.dumps(payload).encode('utf-8')
    request = UrlRequest(
        url,
        data=body,
        headers={
            'Authorization': f'Bearer {token}',
            'Content-Type': 'application/json',
        },
        method='POST',
    )
    try:
        with urlopen(request, timeout=20) as response:
            return json.loads(response.read().decode('utf-8'))
    except HTTPError as exc:
        detalhe = exc.read().decode('utf-8', errors='replace')
        try:
            detalhe_json = json.loads(detalhe)
        except json.JSONDecodeError:
            detalhe_json = {'message': detalhe}
        raise HTTPException(
            status_code=exc.code,
            detail=detalhe_json,
        ) from exc
    except URLError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f'Falha ao conectar na Graph API: {exc.reason}',
        ) from exc


def upload_whatsapp_media(*, content: bytes, mime_type: str) -> str:
    versao, phone_number_id, token = whatsapp_config()
    boundary = f'----prontocardio-{uuid.uuid4().hex}'
    body = (
        (
            f'--{boundary}\r\nContent-Disposition: form-data; '
            'name="messaging_product"\r\n\r\nwhatsapp\r\n'
            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
            'filename="comprovante.png"\r\nContent-Type: image/png\r\n\r\n'
        ).encode()
        + content
        + f'\r\n--{boundary}--\r\n'.encode()
    )
    request = UrlRequest(
        f'https://graph.facebook.com/{versao}/{phone_number_id}/media',
        data=body,
        headers={
            'Authorization': f'Bearer {token}',
            'Content-Type': f'multipart/form-data; boundary={boundary}',
        },
        method='POST',
    )
    try:
        with urlopen(request, timeout=20) as response:
            media_id = json.loads(response.read().decode('utf-8')).get('id')
    except (HTTPError, URLError) as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail='Falha ao carregar a imagem do comprovante.',
        ) from exc
    if not media_id:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail='A Meta não retornou o identificador da imagem.',
        )
    return str(media_id)


def enviar_comprovante_whatsapp(
    *, telefone: str, nome_template: str, idioma: str, png: bytes
) -> dict[str, Any]:
    if not png.startswith(PNG_SIGNATURE) or len(png) > 5 * 1024 * 1024:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail='Comprovante PNG inválido.',
        )
    telefone_normalizado = normalizar_telefone_whatsapp(telefone)
    media_id = upload_whatsapp_media(content=png, mime_type='image/png')
    resposta = post_graph_messages({
        'messaging_product': 'whatsapp',
        'to': telefone_normalizado,
        'type': 'template',
        'template': {
            'name': nome_template,
            'language': {'code': idioma},
            'components': [
                {
                    'type': 'header',
                    'parameters': [
                        {'type': 'image', 'image': {'id': media_id}}
                    ],
                }
            ],
        },
    })
    messages = resposta.get('messages') or []
    external_id = messages[0].get('id') if messages else None
    if not external_id:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail='A Meta não confirmou o envio do comprovante.',
        )
    return {
        'status': 'enviado',
        'id_externo': str(external_id),
        'telefone_final': telefone_normalizado[-4:],
    }


def enviar_template_whatsapp(
    *,
    telefone: str,
    nome_template: str,
    idioma: str = 'pt_BR',
    parametros: list[str] | None = None,
) -> dict[str, Any]:
    telefone_normalizado = normalizar_telefone_whatsapp(telefone)
    template: dict[str, Any] = {
        'name': nome_template,
        'language': {'code': idioma},
    }
    if parametros:
        template['components'] = [
            {
                'type': 'body',
                'parameters': [
                    {'type': 'text', 'text': str(parametro)}
                    for parametro in parametros
                ],
            }
        ]

    resposta = post_graph_messages({
        'messaging_product': 'whatsapp',
        'to': telefone_normalizado,
        'type': 'template',
        'template': template,
    })
    return {
        'status': 'enviado',
        'telefone': telefone_normalizado,
        'template': nome_template,
        'retorno_meta': resposta,
    }


def enviar_otp_whatsapp(
    *,
    telefone: str,
    codigo: str,
) -> dict[str, Any]:
    """Envia um código usando o template de autenticação aprovado pela Meta."""

    telefone_normalizado = normalizar_telefone_whatsapp(telefone)
    resposta = post_graph_messages(
        {
            'messaging_product': 'whatsapp',
            'to': telefone_normalizado,
            'type': 'template',
            'template': {
                'name': settings.WHATSAPP_TEMPLATE_PATIENT_OTP,
                'language': {
                    'code': settings.WHATSAPP_TEMPLATE_PATIENT_OTP_LANGUAGE
                },
                'components': [
                    {
                        'type': 'body',
                        'parameters': [{'type': 'text', 'text': codigo}],
                    },
                    {
                        'type': 'button',
                        'sub_type': 'url',
                        'index': '0',
                        'parameters': [{'type': 'text', 'text': codigo}],
                    },
                ],
            },
        }
    )
    return {
        'status': 'enviado',
        'telefone_final': telefone_normalizado[-4:],
        'template': settings.WHATSAPP_TEMPLATE_PATIENT_OTP,
        'retorno_meta': resposta,
    }


def enviar_texto_whatsapp(*, telefone: str, mensagem: str) -> dict[str, Any]:
    telefone_normalizado = normalizar_telefone_whatsapp(telefone)
    resposta = post_graph_messages({
        'messaging_product': 'whatsapp',
        'to': telefone_normalizado,
        'type': 'text',
        'text': {'body': mensagem},
    })
    return {
        'status': 'enviado',
        'telefone': telefone_normalizado,
        'retorno_meta': resposta,
    }
