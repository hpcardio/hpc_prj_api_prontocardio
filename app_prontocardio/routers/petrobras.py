"""Petrobras endpoints; other payer flows are deliberately untouched."""

import base64
import binascii
import hashlib
from datetime import date
from pathlib import PurePath
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app_prontocardio.database import get_session_postgres
from app_prontocardio.models import Usuario
from app_prontocardio.petrobras_models import (
    PetrobrasAnexo,
    PetrobrasEvento,
    PetrobrasExecucao,
    PetrobrasGlosa,
    PetrobrasProtocolo,
)
from app_prontocardio.security import valida_token_usuario_atual
from app_prontocardio.services import petrobras_oracle
from app_prontocardio.services.petrobras_appeals import (
    poll_appeal,
    send_appeal,
)
from app_prontocardio.services.petrobras_config import PetrobrasConfig
from app_prontocardio.services.petrobras_match import reconcile
from app_prontocardio.services.petrobras_sync import (
    event,
    synchronize,
    synchronize_competencia,
)
from app_prontocardio.services.petrobras_tiss import TissError

router = APIRouter(prefix='/app_glosas/petrobras', tags=['Petrobras'])
MAX_ATTACHMENTS = 20
DB = Annotated[Session, Depends(get_session_postgres)]


def get_config():
    return PetrobrasConfig()


Config = Annotated[PetrobrasConfig, Depends(get_config)]


def access(user: Usuario = Depends(valida_token_usuario_atual)):
    if user.perfil != 'ti' and 'follow_up_glosas' not in (
        user.telas_permitidas or []
    ):
        raise HTTPException(403, 'Sem acesso ao Follow-Up de Glosas.')
    return user


def writer(user: Usuario = Depends(access)):
    if user.perfil == 'leitura':
        raise HTTPException(403, 'Perfil de leitura não permite alterações.')
    return user


User = Annotated[Usuario, Depends(access)]
Writer = Annotated[Usuario, Depends(writer)]


def item(session, config, item_id, lock=False):
    stmt = (
        select(PetrobrasGlosa)
        .join(PetrobrasProtocolo)
        .where(
            PetrobrasGlosa.id == item_id,
            PetrobrasProtocolo.prestador == config.prestador_cnpj,
        )
    )
    if lock:
        stmt = stmt.with_for_update(of=PetrobrasGlosa)
    row = session.scalar(stmt)
    if row is None:
        raise HTTPException(404, 'Glosa Petrobras não encontrada.')
    return row


def public(row):
    return {
        key: getattr(row, key)
        for key in (
            'id',
            'demonstrativo',
            'guia_prestador',
            'guia_operadora',
            'tuss',
            'fonte',
            'valor_glosa',
            'presente_na_fonte',
            'cruzamento',
            'oracle',
            'divergencias',
            'candidatos',
            'erro_oracle',
            'atualizado_em',
            'justificativa',
            'versao',
            'prazo',
            'origem_prazo',
            'recurso_estado',
            'recurso_guia',
            'recurso_protocolo',
            'recurso_resultado',
            'ultima_consulta_recurso',
        )
    }


@router.get('/status')
def status(config: Config, user: User):
    return {
        'habilitado': config.enabled,
        'transmissao_habilitada': config.transmission_enabled,
        'mapeamento_guia': config.oracle_guia,
        'convenio_id': config.convenio_id,
        'registro_ans': config.registro_ans,
        'intervalo_minutos': config.interval_minutes,
        'limite_anexo_bytes': config.max_attachment_bytes,
    }


@router.get('/glosas')
def list_items(  # noqa: PLR0913 - independent HTTP query parameters
    db: DB,
    config: Config,
    user: User,
    guia: str = Query('', max_length=100),
    tuss: str = Query('', max_length=20),
    cruzamento: Literal[
        '', 'conciliado', 'nao_localizado', 'ambiguo', 'dados_insuficientes'
    ] = '',
    recurso: str = Query('', max_length=30),
    prazo_ate: date | None = None,
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
):
    stmt = (
        select(PetrobrasGlosa)
        .join(PetrobrasProtocolo)
        .where(PetrobrasProtocolo.prestador == config.prestador_cnpj)
    )
    if guia:
        stmt = stmt.where(
            or_(
                PetrobrasGlosa.guia_prestador == guia.strip(),
                PetrobrasGlosa.guia_operadora == guia.strip(),
            )
        )
    if tuss:
        stmt = stmt.where(PetrobrasGlosa.tuss == tuss.strip())
    if cruzamento:
        stmt = stmt.where(PetrobrasGlosa.cruzamento == cruzamento)
    if recurso:
        stmt = stmt.where(PetrobrasGlosa.recurso_estado == recurso)
    if prazo_ate:
        stmt = stmt.where(PetrobrasGlosa.prazo <= prazo_ate)
    total = db.scalar(select(func.count()).select_from(stmt.subquery()))
    rows = db.scalars(
        stmt
        .order_by(
            PetrobrasGlosa.prazo.asc().nullslast(), PetrobrasGlosa.id.desc()
        )
        .offset(offset)
        .limit(limit)
    ).all()
    return {'total': total, 'items': [public(r) for r in rows]}


@router.get('/protocolos')
def list_protocols(db: DB, config: Config, user: User):
    rows = db.scalars(
        select(PetrobrasProtocolo)
        .where(PetrobrasProtocolo.prestador == config.prestador_cnpj)
        .order_by(PetrobrasProtocolo.id.desc())
        .limit(500)
    )
    return [
        {
            'id': r.id,
            'numero': r.numero,
            'cd_remessa': r.cd_remessa,
            'ultima_sincronizacao': r.ultima_sincronizacao,
            'erro': r.erro,
        }
        for r in rows
    ]


class ProtocolInput(BaseModel):
    numero: str = Field(min_length=1, max_length=12, pattern=r'^\S+$')


@router.post('/protocolos')
def add_protocol(body: ProtocolInput, db: DB, config: Config, user: Writer):
    if not config.prestador_cnpj:
        raise HTTPException(409, 'Configure o CNPJ do prestador.')
    row = db.scalar(
        select(PetrobrasProtocolo).where(
            PetrobrasProtocolo.prestador == config.prestador_cnpj,
            PetrobrasProtocolo.numero == body.numero,
        )
    )
    if row is None:
        row = PetrobrasProtocolo(
            prestador=config.prestador_cnpj, numero=body.numero
        )
        db.add(row)
        db.commit()
    return {'id': row.id, 'numero': row.numero}


class CompetenciaInput(BaseModel):
    competencia: str = Field(pattern=r'^\d{4}(0[1-9]|1[0-2])$')


@router.post('/competencias/sincronizar')
def discover(body: CompetenciaInput, db: DB, config: Config, user: Writer):
    try:
        return synchronize_competencia(
            db, config, body.competencia, petrobras_oracle.candidates, user.id
        )
    except (TissError, ValueError) as exc:
        raise HTTPException(502, str(exc)) from None


@router.post('/protocolos/{protocol_id}/sincronizar')
def sync(protocol_id: int, db: DB, config: Config, user: Writer):
    try:
        return synchronize(
            db, config, protocol_id, petrobras_oracle.candidates, user.id
        )
    except (TissError, ValueError) as exc:
        raise HTTPException(502, str(exc)) from None


@router.get('/execucoes')
def runs(db: DB, user: User):
    return [
        {
            'id': r.id,
            'inicio': r.inicio,
            'fim': r.fim,
            'situacao': r.situacao,
            'quantidade': r.quantidade,
            'erro': r.erro,
        }
        for r in db.scalars(
            select(PetrobrasExecucao)
            .order_by(PetrobrasExecucao.id.desc())
            .limit(20)
        )
    ]


@router.post('/glosas/{item_id}/cruzar')
def match_again(item_id: int, db: DB, config: Config, user: Writer):
    row = item(db, config, item_id, lock=True)
    try:
        candidates = petrobras_oracle.candidates([row.fonte], config)
    except Exception:
        raise HTTPException(
            503, 'Oracle indisponível; cruzamento anterior preservado.'
        ) from None
    result = reconcile(
        row.fonte, candidates, config.convenio_id, config.oracle_guia
    )
    row.cruzamento, row.oracle = result['status'], result['oracle']
    row.divergencias, row.candidatos, row.erro_oracle = (
        result['divergencias'],
        result['count'],
        None,
    )
    event(db, row, 'cruzamento_manual', result, user.id)
    db.commit()
    return public(row)


class DraftInput(BaseModel):
    justificativa: str = Field(max_length=500)
    versao: int = Field(ge=1)
    prazo: date | None = None
    origem_prazo: str | None = Field(None, max_length=500)


@router.put('/glosas/{item_id}/rascunho')
def save_draft(
    item_id: int, body: DraftInput, db: DB, config: Config, user: Writer
):
    row = item(db, config, item_id, lock=True)
    if row.versao != body.versao or row.recurso_estado != 'rascunho':
        raise HTTPException(
            409,
            'Registro alterado ou recurso já transmitido. Atualize a tela.',
        )
    if body.prazo and not (body.origem_prazo or '').strip():
        raise HTTPException(422, 'Informe a origem da regra ou data de prazo.')
    event(
        db,
        row,
        'rascunho',
        {
            'antes': row.justificativa,
            'depois': body.justificativa,
            'prazo': str(body.prazo) if body.prazo else None,
            'origem_prazo': body.origem_prazo,
        },
        user.id,
    )
    row.justificativa = body.justificativa.strip()
    row.prazo, row.origem_prazo = body.prazo, body.origem_prazo
    row.versao += 1
    db.commit()
    return public(row)


@router.get('/glosas/{item_id}/historico')
def history(item_id: int, db: DB, config: Config, user: User):
    item(db, config, item_id)
    return [
        {
            'data': e.criado_em,
            'tipo': e.tipo,
            'dados': e.dados,
            'usuario_id': e.usuario_id,
        }
        for e in db.scalars(
            select(PetrobrasEvento)
            .where(PetrobrasEvento.glosa_id == item_id)
            .order_by(PetrobrasEvento.id.desc())
            .limit(100)
        )
    ]


class AttachmentInput(BaseModel):
    nome: str = Field(min_length=1, max_length=200)
    conteudo_base64: str = Field(max_length=14000000)
    tipo_documento: str = Field(pattern=r'^(0[1-9]|1[0-7])$')
    natureza_guia: Literal['1', '2', '3']


def attachment_public(a):
    return {
        'id': a.id,
        'nome': a.nome,
        'mime': a.mime,
        'estado': a.estado,
        'protocolo': a.protocolo,
        'tipo_documento': a.tipo_documento,
        'natureza_guia': a.natureza_guia,
    }


@router.get('/glosas/{item_id}/anexos')
def attachments(item_id: int, db: DB, config: Config, user: User):
    item(db, config, item_id)
    return [
        attachment_public(a)
        for a in db.scalars(
            select(PetrobrasAnexo)
            .where(PetrobrasAnexo.glosa_id == item_id)
            .order_by(PetrobrasAnexo.id)
        )
    ]


@router.post('/glosas/{item_id}/anexos')
def attach(
    item_id: int, body: AttachmentInput, db: DB, config: Config, user: Writer
):
    row = item(db, config, item_id, lock=True)
    if row.recurso_estado != 'rascunho':
        raise HTTPException(409, 'Recurso em transmissão ou já protocolado.')
    try:
        content = base64.b64decode(body.conteudo_base64, validate=True)
    except (ValueError, binascii.Error):
        raise HTTPException(422, 'Arquivo inválido.') from None
    if not content or len(content) > config.max_attachment_bytes:
        raise HTTPException(
            422, 'Arquivo vazio ou acima do limite configurado.'
        )
    if content.startswith(b'%PDF-'):
        mime, fmt = 'application/pdf', '02'
    elif content.startswith(b'\x89PNG\r\n\x1a\n'):
        mime, fmt = 'image/png', '03'
    elif content.startswith(b'\xff\xd8\xff'):
        mime, fmt = 'image/jpeg', '01'
    else:
        raise HTTPException(422, 'Formato permitido: PDF, PNG ou JPEG.')
    digest = hashlib.sha256(content).hexdigest()
    old = db.scalar(
        select(PetrobrasAnexo).where(
            PetrobrasAnexo.glosa_id == item_id, PetrobrasAnexo.sha256 == digest
        )
    )
    if old:
        return attachment_public(old)
    if (
        db.scalar(
            select(func.count())
            .select_from(PetrobrasAnexo)
            .where(PetrobrasAnexo.glosa_id == item_id)
        )
        >= MAX_ATTACHMENTS
    ):
        raise HTTPException(422, 'Limite de 20 anexos por item.')
    name = PurePath(body.nome.replace('\\', '/')).name
    a = PetrobrasAnexo(
        glosa_id=item_id,
        nome=name,
        mime=mime,
        formato=fmt,
        conteudo=content,
        sha256=digest,
        tipo_documento=body.tipo_documento,
        natureza_guia=body.natureza_guia,
    )
    db.add(a)
    db.flush()
    event(
        db, row, 'anexo_adicionado', {'anexo_id': a.id, 'nome': name}, user.id
    )
    row.versao += 1
    db.commit()
    return attachment_public(a)


@router.get('/glosas/{item_id}/anexos/{anexo_id}')
def download(item_id: int, anexo_id: int, db: DB, config: Config, user: User):
    item(db, config, item_id)
    a = db.scalar(
        select(PetrobrasAnexo).where(
            PetrobrasAnexo.id == anexo_id, PetrobrasAnexo.glosa_id == item_id
        )
    )
    if not a:
        raise HTTPException(404, 'Anexo não encontrado.')
    return Response(
        a.conteudo,
        media_type=a.mime,
        headers={
            'Content-Disposition': (
                f'attachment; filename="comprovante-{a.id}"'
            ),
            'Cache-Control': 'no-store',
            'X-Content-Type-Options': 'nosniff',
        },
    )


class SendInput(BaseModel):
    versao: int = Field(ge=1)
    confirmar_envio: Literal[True]


@router.post('/glosas/{item_id}/enviar')
def send(item_id: int, body: SendInput, db: DB, config: Config, user: Writer):

    row = item(db, config, item_id, lock=True)
    try:
        return public(send_appeal(db, config, row, body.versao, user.id))
    except (TissError, ValueError) as exc:
        raise HTTPException(409, str(exc)) from None


@router.post('/glosas/{item_id}/consultar-recurso')
def poll(item_id: int, db: DB, config: Config, user: Writer):

    row = item(db, config, item_id, lock=True)
    try:
        return public(poll_appeal(db, config, row, user.id))
    except (TissError, ValueError) as exc:
        raise HTTPException(502, str(exc)) from None
