"""Petrobras endpoints; other payer flows are deliberately untouched."""

import base64
import binascii
import hashlib
from collections import Counter
from datetime import date
from decimal import Decimal
from pathlib import PurePath
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.orm import Session, aliased

from app_prontocardio.database import get_session_postgres
from app_prontocardio.models import Tiss, Usuario
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
    ACATO_EVENTS,
    acatos,
    appeal_codes,
    appeal_readiness,
    efetivar_portal,
    efetivar_preview,
    poll_appeal,
    send_appeal,
    send_protocol_appeal,
)
from app_prontocardio.services.petrobras_config import PetrobrasConfig
from app_prontocardio.services.petrobras_manual_link import guide_summary
from app_prontocardio.services.petrobras_match import (
    GUIDE_FIELD,
    GUIDE_ONLY_SCOPE,
    provider_guide,
    reconcile,
)
from app_prontocardio.services.petrobras_portal_pipeline import detail_guides
from app_prontocardio.services.petrobras_portal_status import (
    sync_portal_situation,
)
from app_prontocardio.services.petrobras_sync import (
    MANUAL_KEY_EVENT,
    event,
    guide_senhas,
    manual_keys,
    matching_source,
    synchronize,
    synchronize_competencia,
)
from app_prontocardio.services.petrobras_tiss import TissError, appeal_block

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


def public(row, table_codes=frozenset()):
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
    } | {'bloqueio_envio': appeal_block(row.fonte or {}, table_codes)}


@router.get('/status')
def status(config: Config, user: User):
    return {
        'habilitado': config.enabled,
        'transmissao_habilitada': config.transmission_enabled,
        'transmissao_usuario': config.can_transmit(
            getattr(user, 'email', None)
        ),
        'mapeamento_guia': GUIDE_FIELD,
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
    codes = appeal_codes(db, config, [r.fonte for r in rows])
    return {'total': total, 'items': [public(r, codes) for r in rows]}


MAX_DEMONSTRATIVO_ITEMS = 2000


def acato_ativo():
    """Item whose latest acceptance event is an internal acato."""
    newer = aliased(PetrobrasEvento)
    return exists().where(
        PetrobrasEvento.glosa_id == PetrobrasGlosa.id,
        PetrobrasEvento.tipo == 'acato',
        ~exists().where(
            newer.glosa_id == PetrobrasEvento.glosa_id,
            newer.tipo.in_(ACATO_EVENTS),
            newer.id > PetrobrasEvento.id,
        ),
    )


def treated(valor_glosa, recurso_estado, acato):
    """Sent appeal treats the whole glosa; otherwise the acato value."""
    glosa = Decimal(str(valor_glosa or 0))
    if recurso_estado != 'rascunho':
        return glosa
    return min(glosa, Decimal(str((acato or {}).get('valor') or 0)))


def untouched():
    """No acato, appeal, justification, deadline or attachment."""
    return and_(
        ~acato_ativo(),
        PetrobrasGlosa.recurso_estado == 'rascunho',
        func.coalesce(PetrobrasGlosa.justificativa, '') == '',
        PetrobrasGlosa.prazo.is_(None),
        ~exists().where(PetrobrasAnexo.glosa_id == PetrobrasGlosa.id),
    )


def detailed_by_analysis():
    """Untouched payment guide whose items an analysis already detailed."""
    detailed = aliased(PetrobrasGlosa)
    return and_(
        PetrobrasGlosa.fonte['escopo'].as_string() == GUIDE_ONLY_SCOPE,
        untouched(),
        exists().where(
            detailed.protocolo_id == PetrobrasGlosa.protocolo_id,
            detailed.presente_na_fonte.is_(True),
            detailed.fonte['escopo'].as_string() != GUIDE_ONLY_SCOPE,
            func.ltrim(detailed.guia_prestador, '0')
            == func.ltrim(PetrobrasGlosa.guia_prestador, '0'),
        ),
    )


def superseded_by_reissue():
    """Untouched item also returned by a more recent demonstrativo.

    The payer reissues analyses of a protocol; the same item (provider guide
    + scope + sequence) is then listed once, from the latest emission. Items
    with justification, deadline, attachments or an appeal always remain.
    """
    newer = aliased(PetrobrasGlosa)
    emitted = func.coalesce(
        PetrobrasGlosa.fonte['data_emissao'].as_string(), ''
    )
    newer_emitted = func.coalesce(newer.fonte['data_emissao'].as_string(), '')
    reissued = exists().where(
        newer.protocolo_id == PetrobrasGlosa.protocolo_id,
        newer.id != PetrobrasGlosa.id,
        newer.presente_na_fonte.is_(True),
        newer.guia_prestador.is_not_distinct_from(
            PetrobrasGlosa.guia_prestador
        ),
        newer.fonte['escopo'].as_string()
        == PetrobrasGlosa.fonte['escopo'].as_string(),
        func.coalesce(newer.fonte['sequencial'].as_string(), '')
        == func.coalesce(PetrobrasGlosa.fonte['sequencial'].as_string(), ''),
        or_(
            newer_emitted > emitted,
            and_(newer_emitted == emitted, newer.id > PetrobrasGlosa.id),
        ),
    )
    return and_(untouched(), reissued)


@router.get('/demonstrativos')
def list_demonstrativos(  # noqa: PLR0913 - independent HTTP query parameters
    db: DB,
    config: Config,
    user: User,
    protocolo: str = Query('', max_length=20),
    guia: str = Query('', max_length=100),
    lote: str = Query('', max_length=100),
    paciente: str = Query('', max_length=150),
    cd_atendimento: int | None = Query(None, ge=1),
    cd_remessa: int | None = Query(None, ge=1),
    offset: int = Query(0, ge=0),
    limit: int = Query(10, ge=0, le=50),
):
    """Follow-Up cards: one page of demonstrativos with all their items.

    ``limit=0`` returns only the totals used to paginate the merged list.
    """
    filters = [
        PetrobrasProtocolo.prestador == config.prestador_cnpj,
        PetrobrasGlosa.presente_na_fonte.is_(True),
        ~superseded_by_reissue(),
        ~detailed_by_analysis(),
    ]
    if protocolo.strip():
        filters.append(PetrobrasProtocolo.numero == protocolo.strip())
    if guia.strip():
        # Senha belongs to the guide: a reissue without it is still found by
        # the senha sent in another demonstrativo of the same guide.
        same_guide = aliased(PetrobrasGlosa)
        filters.append(
            or_(
                func.ltrim(PetrobrasGlosa.guia_prestador, '0')
                == provider_guide(guia),
                PetrobrasGlosa.fonte['senha'].as_string() == guia.strip(),
                PetrobrasGlosa.guia_operadora == guia.strip(),
                exists().where(
                    same_guide.protocolo_id == PetrobrasGlosa.protocolo_id,
                    same_guide.guia_prestador == PetrobrasGlosa.guia_prestador,
                    same_guide.fonte['senha'].as_string() == guia.strip(),
                ),
            )
        )
    if lote.strip():
        filters.append(
            PetrobrasGlosa.fonte['lote'].as_string() == lote.strip()
        )
    if paciente.strip():
        filters.append(
            PetrobrasGlosa
            .oracle['nm_paciente']
            .as_string()
            .ilike(f'%{paciente.strip()}%')
        )
    if cd_atendimento:
        filters.append(
            PetrobrasGlosa.oracle['cd_atendimento'].as_integer()
            == cd_atendimento
        )
    if cd_remessa:
        filters.append(
            PetrobrasGlosa.oracle['cd_remessa'].as_integer() == cd_remessa
        )
    scoped = select(PetrobrasGlosa).join(PetrobrasProtocolo).where(*filters)
    base = scoped.subquery()
    summary = db.execute(
        select(
            func.count(func.distinct(base.c.demonstrativo)),
            func.count(base.c.id),
            func.coalesce(func.sum(base.c.valor_glosa), 0),
        )
    ).one()
    states = db.execute(
        select(base.c.id, base.c.valor_glosa, base.c.recurso_estado)
    ).all()
    accepted = acatos(db, (r.id for r in states))
    # Accepted on the portal is acato, not appeal, though no longer a draft.
    appealed = sum(
        (
            Decimal(str(v or 0))
            for _, v, e in states
            if e not in {'rascunho', 'aceito_portal'}
        ),
        Decimal(0),
    )
    total_treated = sum(
        (treated(v, e, accepted.get(i)) for i, v, e in states), Decimal(0)
    )
    page = list(
        db.scalars(
            select(PetrobrasGlosa.demonstrativo)
            .join(PetrobrasProtocolo)
            .where(*filters)
            .group_by(PetrobrasGlosa.demonstrativo)
            .order_by(
                func.max(
                    PetrobrasGlosa.fonte['data_emissao'].as_string()
                ).desc(),
                PetrobrasGlosa.demonstrativo.desc(),
            )
            .offset(offset)
            .limit(limit)
        )
    )
    rows, senhas, terms = [], {}, {}
    if page:
        rows = db.scalars(
            scoped
            .where(PetrobrasGlosa.demonstrativo.in_(page))
            .order_by(
                PetrobrasGlosa.demonstrativo,
                PetrobrasProtocolo.numero,
                PetrobrasGlosa.guia_prestador,
                PetrobrasGlosa.id,
            )
            .limit(MAX_DEMONSTRATIVO_ITEMS)
        ).all()
        senhas = guide_senhas(db, {r.protocolo_id for r in rows})
        terms = glosa_terms(db, rows)
    codes = appeal_codes(db, config, [r.fonte for r in rows])
    return {
        'total': summary[0],
        'quantidade_glosas': summary[1],
        'valor_total_glosado': summary[2],
        'valor_total_tratado': total_treated,
        'valor_total_recursado': appealed,
        'valor_total_acatado': total_treated - appealed,
        'offset': offset,
        'limit': limit,
        'demonstrativos': page,
        'integracao': {
            'transmissao_habilitada': config.transmission_enabled,
            'limite_anexo_bytes': config.max_attachment_bytes,
        },
        'items': [
            with_guide_senha(r, senhas, accepted, terms, codes) for r in rows
        ],
    }


def glosa_terms(db, rows):
    """TISS descriptions (table 38) of the glosa codes on the page."""
    codes = {
        str(m.get('codigo'))
        for r in rows
        for m in (r.fonte or {}).get('motivos') or []
        if m.get('codigo')
    }
    if not codes:
        return {}
    return dict(
        db.execute(
            select(Tiss.codigo_termo, Tiss.termo).where(
                Tiss.codigo_termo.in_(codes)
            )
        ).all()
    )


def with_guide_senha(  # noqa: PLR0913 - page-level lookups
    row, senhas, accepted=None, terms=None, table_codes=frozenset()
):
    """Public row plus the senha of its guide, when the guide has only one.

    Shows the payer's senha; a key typed in the manual association is used
    only to find the MV account.
    """
    source = matching_source(row.fonte or {}, senhas, manual=False)
    acato = (accepted or {}).get(row.id)
    return {
        **public(row, table_codes),
        'motivos': [
            {**m, 'descricao': (terms or {}).get(str(m.get('codigo')))}
            for m in (row.fonte or {}).get('motivos') or []
        ],
        'senha_guia': source.get('senha'),
        'acato': acato,
        'valor_tratado': treated(row.valor_glosa, row.recurso_estado, acato),
    }


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


@router.post('/portal/detalhar')
def portal_detail(db: DB, config: Config, user: Writer):
    """Item detail of guide-only glosas from the portal analysis XML."""
    try:
        return detail_guides(db, config, petrobras_oracle.candidates, user.id)
    except (TissError, ValueError) as exc:
        raise HTTPException(502, str(exc)) from None


class SituacaoPortalInput(BaseModel):
    apos: str | None = Field(None, max_length=30)


@router.post('/portal/situacao')
def portal_situation(
    db: DB,
    config: Config,
    user: Writer,
    body: SituacaoPortalInput | None = None,
):
    """Record what was done directly on the portal (read-only there).

    Short calls: repeat with `apos` = the returned `proximo` until it is null.
    """
    try:
        return sync_portal_situation(
            db, config, user.id, after=body.apos if body else None
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
    source = matching_source(row.fonte, guide_senhas(db, [row.protocolo_id]))
    try:
        candidates = petrobras_oracle.candidates([source], config)
    except Exception:
        raise HTTPException(
            503, 'Oracle indisponível; cruzamento anterior preservado.'
        ) from None
    apply_match(db, config, row, source, candidates, user.id)
    db.commit()
    return public(row)


def apply_match(  # noqa: PLR0913, PLR0917 - explicit audit context
    db, config, row, source, candidates, user_id, always_log=True
):
    result = reconcile(source, candidates, config.convenio_id)
    changed = (row.cruzamento, row.oracle, row.divergencias) != (
        result['status'],
        result['oracle'],
        result['divergencias'],
    )
    row.cruzamento, row.oracle = result['status'], result['oracle']
    row.divergencias, row.candidatos, row.erro_oracle = (
        result['divergencias'],
        result['count'],
        None,
    )
    if changed or always_log:
        event(db, row, 'cruzamento_manual', result, user_id)
    return changed


RECROSS_BATCH = 500


@router.post('/glosas/cruzar')
def match_all(db: DB, config: Config, user: Writer):
    """Re-check stored sources against Oracle; never calls the payer."""
    ids = list(
        db.scalars(
            select(PetrobrasGlosa.id)
            .join(PetrobrasProtocolo)
            .where(PetrobrasProtocolo.prestador == config.prestador_cnpj)
            .order_by(PetrobrasGlosa.id)
        )
    )
    changed = 0
    for start in range(0, len(ids), RECROSS_BATCH):
        rows = db.scalars(
            select(PetrobrasGlosa)
            .where(PetrobrasGlosa.id.in_(ids[start : start + RECROSS_BATCH]))
            .with_for_update()
        ).all()
        senhas = guide_senhas(db, {r.protocolo_id for r in rows})
        sources = {r.id: matching_source(r.fonte, senhas) for r in rows}
        try:
            candidates = petrobras_oracle.candidates(
                list(sources.values()), config
            )
        except Exception:
            db.rollback()
            raise HTTPException(
                503, 'Oracle indisponível; cruzamento anterior preservado.'
            ) from None
        changed += sum(
            apply_match(
                db, config, row, sources[row.id], candidates, user.id, False
            )
            for row in rows
        )
        db.commit()
    totals = dict(
        db.execute(
            select(PetrobrasGlosa.cruzamento, func.count())
            .join(PetrobrasProtocolo)
            .where(PetrobrasProtocolo.prestador == config.prestador_cnpj)
            .group_by(PetrobrasGlosa.cruzamento)
        ).all()
    )
    return {'conferidos': len(ids), 'alterados': changed, 'situacao': totals}


MAX_PENDING_ITEMS = 5000


def pending_filters(config):
    """Items the Follow-Up shows that are still not linked to the MV."""
    return [
        PetrobrasProtocolo.prestador == config.prestador_cnpj,
        PetrobrasGlosa.presente_na_fonte.is_(True),
        PetrobrasGlosa.cruzamento != 'conciliado',
        ~superseded_by_reissue(),
        ~detailed_by_analysis(),
    ]


@router.get('/vinculos-pendentes')
def pending_links(  # noqa: PLR0913 - independent HTTP query parameters
    db: DB,
    config: Config,
    user: User,
    protocolo: str = Query('', max_length=20),
    lote: str = Query('', max_length=100),
    offset: int = Query(0, ge=0),
    limit: int = Query(10, ge=1, le=50),
):
    """Unlinked guides, why each one failed and the MV key it needs."""
    filters = pending_filters(config)
    if protocolo.strip():
        filters.append(PetrobrasProtocolo.numero == protocolo.strip())
    if lote.strip():
        filters.append(
            PetrobrasGlosa.fonte['lote'].as_string() == lote.strip()
        )
    rows = db.scalars(
        select(PetrobrasGlosa)
        .join(PetrobrasProtocolo)
        .where(*filters)
        .order_by(
            PetrobrasProtocolo.numero.desc(),
            PetrobrasGlosa.guia_prestador,
            PetrobrasGlosa.id,
        )
        .limit(MAX_PENDING_ITEMS)
    ).all()
    senhas = guide_senhas(db, {r.protocolo_id for r in rows})
    sources = {r.id: matching_source(r.fonte or {}, senhas) for r in rows}
    guides = {}
    for row in rows:
        guides.setdefault((row.protocolo_id, row.guia_prestador), []).append(
            row
        )
    summaries = [
        guide_summary(
            items,
            sources,
            senhas.manual.get((
                (items[0].fonte or {}).get('protocolo'),
                guide,
            )),
        )
        for (_, guide), items in guides.items()
    ]
    return {
        'total': len(summaries),
        'offset': offset,
        'limit': limit,
        'truncado': len(rows) >= MAX_PENDING_ITEMS,
        'resumo': {
            'guias': len(summaries),
            'itens': len(rows),
            'valor_glosa': sum(
                (s['valor_glosa'] for s in summaries), Decimal(0)
            ),
            'com_campos': sum(1 for s in summaries if s['campos']),
        },
        'guias': summaries[offset : offset + limit],
    }


class ManualKeyInput(BaseModel):
    protocolo_id: int = Field(ge=1)
    guia_prestador: str = Field(min_length=1, max_length=20)
    senha: str | None = Field(None, max_length=20, pattern=r'^\S*$')
    carteira: str | None = Field(None, max_length=30, pattern=r'^\S*$')


@router.put('/vinculos-pendentes')
def save_manual_key(
    body: ManualKeyInput, db: DB, config: Config, user: Writer
):
    """Store the guide's MV key and re-run the automatic item link.

    An omitted field keeps the key already typed; an empty one falls back
    to the payer's data. Nothing is kept when the Oracle lookup fails.
    """
    rows = db.scalars(
        select(PetrobrasGlosa)
        .join(PetrobrasProtocolo)
        .where(
            PetrobrasProtocolo.prestador == config.prestador_cnpj,
            PetrobrasGlosa.protocolo_id == body.protocolo_id,
            PetrobrasGlosa.guia_prestador == body.guia_prestador,
        )
        .order_by(PetrobrasGlosa.id)
        .with_for_update(of=PetrobrasGlosa)
    ).all()
    if not rows:
        raise HTTPException(404, 'Guia Petrobras não encontrada.')
    number = db.get(PetrobrasProtocolo, body.protocolo_id).numero
    typed = manual_keys(db, [body.protocolo_id]).get(
        (number, body.guia_prestador), {}
    )
    key = {
        f: (typed.get(f) or '') if value is None else value.strip()
        for f, value in (('senha', body.senha), ('carteira', body.carteira))
    }
    for row in rows:
        event(db, row, MANUAL_KEY_EVENT, key, user.id)
    db.flush()
    senhas = guide_senhas(db, [body.protocolo_id])
    sources = {r.id: matching_source(r.fonte or {}, senhas) for r in rows}
    try:
        candidates = petrobras_oracle.candidates(
            list(sources.values()), config
        )
    except Exception:
        db.rollback()
        raise HTTPException(
            503, 'Oracle indisponível; nenhuma alteração foi salva.'
        ) from None
    for row in rows:
        apply_match(db, config, row, sources[row.id], candidates, user.id)
    db.commit()
    situations = Counter(r.cruzamento for r in rows if r.presente_na_fonte)
    return {
        'itens': sum(situations.values()),
        'conciliados': situations.get('conciliado', 0),
        'situacao': dict(situations),
    }


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


class AcatoInput(BaseModel):
    data: date
    quantidade: Decimal = Field(gt=0, max_digits=12, decimal_places=2)
    valor: Decimal = Field(gt=0, max_digits=14, decimal_places=2)
    lote: str | None = Field(None, max_length=255)
    processo: str | None = Field(None, max_length=255)
    descricao: str = Field('', max_length=2000)


@router.put('/glosas/{item_id}/acato')
def save_acato(
    item_id: int, body: AcatoInput, db: DB, config: Config, user: Writer
):
    """Recorded here; sent on the payer's screen with the lot's appeal."""
    row = item(db, config, item_id, lock=True)
    if row.recurso_estado != 'rascunho':
        raise HTTPException(409, 'Recurso já transmitido; acato bloqueado.')
    if body.valor > Decimal(str(row.valor_glosa or 0)):
        raise HTTPException(422, 'Valor acatado maior que o valor glosado.')
    quantity = (row.fonte or {}).get('quantidade')
    if quantity and body.quantidade > Decimal(str(quantity)):
        raise HTTPException(422, 'Quantidade acatada maior que a do item.')
    data = body.model_dump(mode='json')
    event(db, row, 'acato', data, user.id)
    db.commit()
    return with_guide_senha(row, {}, {row.id: data})


@router.delete('/glosas/{item_id}/acato')
def remove_acato(item_id: int, db: DB, config: Config, user: Writer):
    row = item(db, config, item_id, lock=True)
    current = acatos(db, [row.id]).get(row.id)
    if current is None:
        raise HTTPException(404, 'Acato não registrado para o item.')
    if row.recurso_estado != 'rascunho':
        raise HTTPException(409, 'Item já enviado ou aceito; acato bloqueado.')
    event(db, row, 'acato_removido', {'antes': current}, user.id)
    db.commit()
    return with_guide_senha(row, {})


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
    if config.transmission_enabled and not config.can_transmit(
        getattr(user, 'email', None)
    ):
        raise HTTPException(
            403, 'Envio de recurso em piloto; usuário ainda não liberado.'
        )
    row = item(db, config, item_id, lock=True)
    try:
        return public(send_appeal(db, config, row, body.versao, user.id))
    except (TissError, ValueError) as exc:
        raise HTTPException(409, str(exc)) from None


MAX_BATCH_SEND = 50
SENT_STATES = {'protocolado', 'enviado_portal'}
PORTAL_SAVED_NOTICE = (
    'Recurso salvo no portal, ainda não enviado. Use Efetivar/Enviar Recursos '
    'Salvos quando quiser enviar o lote; os itens não recursados serão '
    'marcados como aceitos.'
)


@router.get('/glosas/prontos')
def ready_appeals(db: DB, config: Config, user: User):
    """Saved drafts ready to transmit, and drafts still missing something."""
    rows = db.scalars(
        select(PetrobrasGlosa)
        .join(PetrobrasProtocolo)
        .where(
            PetrobrasProtocolo.prestador == config.prestador_cnpj,
            PetrobrasGlosa.recurso_estado == 'rascunho',
            func.coalesce(PetrobrasGlosa.justificativa, '') != '',
        )
        .order_by(PetrobrasGlosa.id)
        .limit(500)
    ).all()
    codes = appeal_codes(db, config, [r.fonte for r in rows])
    ready, pending = [], []
    for row in rows:
        source = row.fonte or {}
        data = {
            'id': row.id,
            'versao': row.versao,
            'protocolo': source.get('protocolo'),
            'lote': source.get('lote'),
            'guia': row.guia_prestador,
            'senha': source.get('senha'),
            'tuss': row.tuss,
            'descricao': source.get('descricao'),
            'valor_glosa': row.valor_glosa,
        }
        issue = appeal_readiness(row, codes)
        (pending if issue else ready).append(
            {**data, 'pendencia': issue} if issue else data
        )
    saved = {}
    for row in db.scalars(
        select(PetrobrasGlosa)
        .join(PetrobrasProtocolo)
        .where(
            PetrobrasProtocolo.prestador == config.prestador_cnpj,
            PetrobrasGlosa.recurso_estado == 'salvo_portal',
        )
        .order_by(PetrobrasGlosa.id)
        .limit(500)
    ):
        source = row.fonte or {}
        group = saved.setdefault(
            (source.get('lote'), source.get('protocolo')),
            {
                'id': row.id,
                'lote': source.get('lote'),
                'protocolo': source.get('protocolo'),
                'itens': 0,
            },
        )
        group['itens'] += 1
    return {
        'prontos': ready,
        'pendencias': pending,
        'salvos': list(saved.values()),
        'usuario_liberado': config.can_transmit(getattr(user, 'email', None)),
        'limite': MAX_BATCH_SEND,
    }


class BatchItem(BaseModel):
    id: int = Field(ge=1)
    versao: int = Field(ge=1)


class BatchSendInput(BaseModel):
    itens: list[BatchItem] = Field(min_length=1, max_length=MAX_BATCH_SEND)
    confirmar_envio: Literal[True]


@router.post('/glosas/enviar-lote')
def send_batch(body: BatchSendInput, db: DB, config: Config, user: Writer):  # noqa: PLR0914 - per item and per protocol outcomes
    """One TISS appeal per lot/protocol with the selected ready items."""
    if not config.can_transmit(getattr(user, 'email', None)):
        raise HTTPException(
            403, 'Envio de recurso em piloto; usuário ainda não liberado.'
        )
    results, groups = {}, {}
    for entry in {i.id: i for i in body.itens}.values():
        try:
            row = item(db, config, entry.id, lock=True)
        except HTTPException as exc:
            results[entry.id] = {'id': entry.id, 'erro': exc.detail}
            continue
        issue = (
            'Recurso alterado; atualize a tela.'
            if row.versao != entry.versao
            else appeal_readiness(row, appeal_codes(db, config, [row.fonte]))
        )
        if issue:
            results[entry.id] = {
                'id': entry.id,
                'situacao': row.recurso_estado,
                'erro': issue,
            }
            continue
        source = row.fonte or {}
        key = (source.get('lote'), source.get('protocolo'))
        groups.setdefault(key, []).append((row, entry.versao))
    appeals = []
    for (lote, protocolo), pairs in groups.items():
        ids = [row.id for row, _ in pairs]
        try:
            rows = send_protocol_appeal(db, config, pairs, user.id)
            outcome = {
                'situacao': rows[0].recurso_estado,
                'protocolo': rows[0].recurso_protocolo,
            }
            if rows[0].recurso_estado == 'salvo_portal':
                outcome['aviso'] = PORTAL_SAVED_NOTICE
            pending = db.scalar(
                select(func.count(PetrobrasAnexo.id)).where(
                    PetrobrasAnexo.glosa_id.in_(ids),
                    PetrobrasAnexo.estado != 'protocolado',
                )
            )
            if pending and rows[0].recurso_estado == 'protocolado':
                outcome['aviso'] = (
                    f'Recurso protocolado; {pending} comprovante(s) não '
                    'enviado(s). Veja o histórico do item.'
                )
        except (TissError, ValueError) as exc:
            db.rollback()
            current = db.get(PetrobrasGlosa, ids[0])
            outcome = {
                'situacao': current.recurso_estado if current else None,
                'erro': str(exc),
            }
        appeals.append({
            'lote': lote,
            'protocolo_petrobras': protocolo,
            'itens': ids,
            **outcome,
        })
        for item_id in ids:
            results[item_id] = {'id': item_id, **outcome}
    return {
        'protocolados': sum(
            r.get('situacao') in SENT_STATES for r in results.values()
        ),
        'recursos': appeals,
        'resultados': list(results.values()),
    }


class EfetivarInput(BaseModel):
    ciente_nao_recursados: Literal[True]


@router.get('/glosas/{item_id}/efetivar-portal')
def portal_submit_preview(item_id: int, db: DB, config: Config, user: Writer):
    """What the user must acknowledge before Efetivar."""
    row = item(db, config, item_id)
    try:
        return efetivar_preview(db, config, row)
    except (TissError, ValueError) as exc:
        raise HTTPException(409, str(exc)) from None


@router.post('/glosas/{item_id}/efetivar-portal')
def portal_submit(
    item_id: int,
    body: EfetivarInput,
    db: DB,
    config: Config,
    user: Writer,
):
    """Send the lot's saved appeals at the user's explicit request."""
    if not config.can_transmit(getattr(user, 'email', None)):
        raise HTTPException(
            403, 'Envio de recurso em piloto; usuário ainda não liberado.'
        )
    row = item(db, config, item_id, lock=True)
    try:
        return public(efetivar_portal(db, config, row, user.id))
    except (TissError, ValueError) as exc:
        raise HTTPException(409, str(exc)) from None


@router.post('/glosas/{item_id}/consultar-recurso')
def poll(item_id: int, db: DB, config: Config, user: Writer):

    row = item(db, config, item_id, lock=True)
    try:
        return public(poll_appeal(db, config, row, user.id))
    except (TissError, ValueError) as exc:
        raise HTTPException(502, str(exc)) from None
