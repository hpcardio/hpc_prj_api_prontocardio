from typing import Annotated, Any

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
    status,
)
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app_prontocardio.database import get_session_postgres
from app_prontocardio.models import WhatsappEnvioIdempotente
from app_prontocardio.routers.agendamentos import ValidaUsuarioAtual
from app_prontocardio.settings import Settings
from app_prontocardio.whatsapp_service import (
    TELEFONE_E164_MAX_LENGTH,
    TELEFONE_E164_MIN_LENGTH,
)
from app_prontocardio.whatsapp_service import (
    enviar_comprovante_whatsapp as enviar_comprovante_whatsapp_service,
)
from app_prontocardio.whatsapp_service import (
    enviar_template_whatsapp as enviar_template_whatsapp_service,
)
from app_prontocardio.whatsapp_service import (
    enviar_texto_whatsapp as enviar_texto_whatsapp_service,
)

router = APIRouter(prefix='/whatsapp', tags=['whatsapp'])

settings = Settings()
MENSAGEM_RESPOSTA_AUTOMATICA_PADRAO = (
    'Olá! Este número é usado apenas para envio automático de confirmações, '
    'cancelamentos e orientações de agendamento.\n'
    'Para falar com o Hospital ProntoCardio, entre em contato pelo '
    'número/canal oficial: 3466-3000'
)


class WhatsAppTextoInput(BaseModel):
    telefone: str = Field(
        min_length=TELEFONE_E164_MIN_LENGTH,
        max_length=TELEFONE_E164_MAX_LENGTH,
        description='Telefone no padrao E.164 sem +. Ex: 5521977854114',
    )
    mensagem: str = Field(min_length=1, max_length=4096)


class WhatsAppTemplateInput(BaseModel):
    telefone: str = Field(
        min_length=TELEFONE_E164_MIN_LENGTH,
        max_length=TELEFONE_E164_MAX_LENGTH,
        description='Telefone no padrao E.164 sem +. Ex: 5521977854114',
    )
    nome_template: str = Field(min_length=1, max_length=512)
    idioma: str = Field(default='pt_BR', min_length=2, max_length=16)
    parametros: list[str] = Field(default_factory=list, max_length=20)


class WhatsAppComprovanteInput(BaseModel):
    telefone: str = Field(
        min_length=TELEFONE_E164_MIN_LENGTH,
        max_length=TELEFONE_E164_MAX_LENGTH,
    )
    nome_template: str = Field(min_length=1, max_length=512)
    idioma: str = Field(default='pt_BR', min_length=2, max_length=16)
    chave_idempotencia: str = Field(min_length=8, max_length=160)


def comprovante_form(
    telefone: Annotated[str, Form()],
    nome_template: Annotated[str, Form()],
    idioma: Annotated[str, Form()] = 'pt_BR',
    chave_idempotencia: Annotated[str, Form()] = '',
) -> WhatsAppComprovanteInput:
    return WhatsAppComprovanteInput(
        telefone=telefone,
        nome_template=nome_template,
        idioma=idioma,
        chave_idempotencia=chave_idempotencia,
    )


@router.post('/enviar-comprovante')
async def enviar_comprovante_whatsapp(
    usuario_atual: ValidaUsuarioAtual,
    payload: Annotated[WhatsAppComprovanteInput, Depends(comprovante_form)],
    arquivo: UploadFile = File(),
    session: Session = Depends(get_session_postgres),
) -> dict[str, Any]:
    del usuario_atual
    if not settings.WHATSAPP_COMPROVANTE_ENABLED:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail='Envio de comprovante não configurado.',
        )
    if payload.nome_template != settings.WHATSAPP_COMPROVANTE_TEMPLATE:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail='Template de comprovante não autorizado.',
        )
    previous = session.scalar(
        select(WhatsappEnvioIdempotente).where(
            WhatsappEnvioIdempotente.chave == payload.chave_idempotencia
        )
    )
    if previous:
        if previous.status == 'ENVIADO':
            return {
                'status': 'ja_enviado',
                'id_externo': previous.id_externo,
                'telefone_final': previous.telefone_final,
            }
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail='Envio anterior pendente de verificação.',
        )
    record = WhatsappEnvioIdempotente(chave=payload.chave_idempotencia)
    session.add(record)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        concurrent = session.scalar(
            select(WhatsappEnvioIdempotente).where(
                WhatsappEnvioIdempotente.chave == payload.chave_idempotencia
            )
        )
        if concurrent and concurrent.status == 'ENVIADO':
            return {
                'status': 'ja_enviado',
                'id_externo': concurrent.id_externo,
                'telefone_final': concurrent.telefone_final,
            }
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail='Envio concorrente pendente de verificação.',
        )
    content = await arquivo.read(settings.WHATSAPP_COMPROVANTE_MAX_BYTES + 1)
    try:
        result = enviar_comprovante_whatsapp_service(
            telefone=payload.telefone,
            nome_template=payload.nome_template,
            idioma=payload.idioma,
            png=content,
        )
        record.status = 'ENVIADO'
        record.id_externo = result['id_externo']
        record.telefone_final = result['telefone_final']
        session.commit()
        return result
    except HTTPException as exc:
        record.status = (
            'INCERTO'
            if exc.status_code >= status.HTTP_500_INTERNAL_SERVER_ERROR
            else 'FALHOU'
        )
        record.erro_sanitizado = str(exc.detail)[:240]
        session.commit()
        raise


@router.get('/webhook')
async def validar_webhook_whatsapp(
    mode: str | None = Query(default=None, alias='hub.mode'),
    verify_token: str | None = Query(default=None, alias='hub.verify_token'),
    challenge: str | None = Query(default=None, alias='hub.challenge'),
):
    """Valida o webhook da Meta/WhatsApp Cloud API.

    A Meta chama este endpoint via GET no momento em que salvamos o callback.
    Se o token enviado for igual ao nosso token configurado, precisamos
    devolver o `hub.challenge` como texto puro.
    """

    if (
        mode == 'subscribe'
        and verify_token == settings.WHATSAPP_WEBHOOK_VERIFY_TOKEN
        and challenge
    ):
        return Response(content=challenge, media_type='text/plain')

    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail='Token de verificacao do WhatsApp invalido.',
    )


@router.post('/webhook')
async def receber_webhook_whatsapp(
    request: Request,
) -> dict[str, Any]:
    """Recebe eventos enviados pela Meta/WhatsApp Cloud API.

    Quando o paciente envia uma mensagem para o numero, respondemos com uma
    orientacao simples informando que o canal e apenas de envio automatico.
    Falhas no envio da resposta nao devem fazer a Meta retentar o webhook.
    """

    payload = await request.json()
    mensagem_auto = (
        getattr(settings, 'WHATSAPP_AUTO_REPLY_TEXT', None)
        or MENSAGEM_RESPOSTA_AUTOMATICA_PADRAO
    )
    respostas_enviadas = 0
    respostas_com_erro = 0

    for entry in payload.get('entry', []) or []:
        for change in entry.get('changes', []) or []:
            value = change.get('value') or {}
            for message in value.get('messages', []) or []:
                telefone = message.get('from')
                if not telefone:
                    continue
                try:
                    enviar_texto_whatsapp_service(
                        telefone=telefone,
                        mensagem=mensagem_auto,
                    )
                    respostas_enviadas += 1
                except Exception:
                    respostas_com_erro += 1

    return {
        'status': 'recebido',
        'object': payload.get('object'),
        'respostas_enviadas': respostas_enviadas,
        'respostas_com_erro': respostas_com_erro,
    }


@router.post('/enviar-texto')
def enviar_texto_whatsapp(
    payload: WhatsAppTextoInput,
    usuario_atual: ValidaUsuarioAtual,
) -> dict[str, Any]:
    """Envia mensagem livre dentro da janela de conversa de 24h.

    Este formato funciona depois que o paciente enviou mensagem para o numero
    do hospital/app. Para mensagens iniciadas pela empresa, use templates.
    """

    del usuario_atual
    return enviar_texto_whatsapp_service(
        telefone=payload.telefone,
        mensagem=payload.mensagem,
    )


@router.post('/enviar-template')
def enviar_template_whatsapp(
    payload: WhatsAppTemplateInput,
    usuario_atual: ValidaUsuarioAtual,
) -> dict[str, Any]:
    """Envia template aprovado no WhatsApp Business Manager.

    Use para confirmacao/lembrete/cancelamento de agendamento iniciado pelo
    hospital, fora da janela de conversa de 24h.
    """

    del usuario_atual
    return enviar_template_whatsapp_service(
        telefone=payload.telefone,
        nome_template=payload.nome_template,
        idioma=payload.idioma,
        parametros=payload.parametros,
    )
