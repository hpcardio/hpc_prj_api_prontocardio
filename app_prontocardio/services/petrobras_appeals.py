"""Explicit submission with durable intent and no retries after uncertainty."""

from copy import deepcopy
from datetime import datetime
from decimal import Decimal
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import or_, select

from app_prontocardio.models import Tiss
from app_prontocardio.petrobras_models import (
    PetrobrasAnexo,
    PetrobrasGlosa,
    utcnow,
)
from app_prontocardio.services.petrobras_sync import event
from app_prontocardio.services.petrobras_tiss import (
    NS,
    PetrobrasClient,
    TissError,
    appeal_block,
    operator_codes,
    value,
)


def table38_codes(db, codes, today=None):
    """Codes valid today in the ANS TISS table 38 loaded in `tiss`."""
    if not codes:
        return frozenset()
    today = today or datetime.now(ZoneInfo('America/Sao_Paulo')).date()
    return frozenset(
        db.scalars(
            select(Tiss.codigo_termo).where(
                Tiss.codigo_termo.in_(set(codes)),
                Tiss.fonte.ilike('%tabela 38%'),
                or_(
                    Tiss.dt_inicio_vigencia.is_(None),
                    Tiss.dt_inicio_vigencia <= today,
                ),
                or_(
                    Tiss.dt_fim_vigencia.is_(None),
                    Tiss.dt_fim_vigencia >= today,
                ),
            )
        )
    )


def appeal_codes(db, config, sources):
    """Table 38 reasons accepted for appeal, when the pilot allows them."""
    if not config.appeal_table_codes:
        return frozenset()
    return table38_codes(
        db, {c for source in sources for c in operator_codes(source or {})}
    )


def appeal_readiness(row, table_codes=frozenset(), today=None):
    """Why a saved draft cannot be transmitted yet; None when ready."""
    today = today or datetime.now(ZoneInfo('America/Sao_Paulo')).date()
    if row.recurso_estado != 'rascunho':
        return 'Recurso já transmitido ou com envio incerto.'
    if not row.presente_na_fonte or Decimal(row.valor_glosa or 0) <= 0:
        return 'Glosa ausente do demonstrativo ou sem valor.'
    if not (row.justificativa or '').strip():
        return 'Justificativa não preenchida.'
    if row.prazo is None or row.prazo < today or not row.origem_prazo:
        return 'Confirme o prazo vigente e sua origem.'
    return appeal_block(row.fonte or {}, table_codes)


def send_appeal(db, config, row, version, user_id, client=None):  # noqa: PLR0913, PLR0915 - durable transmission state transitions
    if not config.transmission_enabled:
        raise TissError(
            (
                'Envio depende da validação da '
                'integração Petrobras neste ambiente.'
            )
        )
    if row.versao != version or row.recurso_estado != 'rascunho':
        raise TissError(
            'Recurso alterado ou transmissão iniciada. Consulte a situação.'
        )
    if (
        not row.presente_na_fonte
        or not row.justificativa.strip()
        or Decimal(row.valor_glosa or 0) <= 0
    ):
        raise TissError('Revise a glosa e preencha a justificativa.')
    today = datetime.now(ZoneInfo('America/Sao_Paulo')).date()
    if row.prazo is None or row.prazo < today or not row.origem_prazo:
        raise TissError(
            'Confirme o prazo vigente e sua origem antes do envio.'
        )
    files = list(
        db.scalars(
            select(PetrobrasAnexo).where(PetrobrasAnexo.glosa_id == row.id)
        )
    )
    if any(a.estado in {'transmitindo', 'envio_incerto'} for a in files):
        raise TissError(
            'Documento com envio incerto; confira o portal antes de reenviar.'
        )
    source, justification = deepcopy(row.fonte), row.justificativa
    appeal_id = row.recurso_guia or uuid4().hex[:20]
    codes = appeal_codes(db, config, [source])
    preflight = PetrobrasClient(config, dry_run=True)
    preflight.appeal(source, justification, appeal_id, codes)
    for a in files:
        preflight.document(source, a)
    row.recurso_guia = appeal_id
    row.recurso_estado = 'transmitindo'
    row.versao += 1
    item_id = row.id
    event(
        db,
        row,
        'transmissao_iniciada',
        {
            'guia_recurso': appeal_id,
            'fonte': source,
            'justificativa': justification,
        },
        user_id,
    )
    db.commit()  # before network: a crash cannot trigger a blind resend
    client = client or PetrobrasClient(config)
    try:
        for a in files:
            if a.estado == 'protocolado':
                continue
            a.estado = 'transmitindo'
            db.commit()
            answer = client.document(source, a)
            receipt = value(
                answer, 'recebimentoDocumento/reciboDocumentos/protocoloDoc'
            )
            doc_id = value(
                answer, 'recebimentoDocumento/reciboDocumentos/numeroDocumento'
            )
            if not receipt or doc_id != str(a.id):
                raise TissError('Documento sem recibo correspondente.')
            a.estado, a.protocolo = 'protocolado', receipt
            db.commit()
        answer = client.appeal(source, justification, appeal_id, codes)
        node = answer.find(
            '{%s}recebimentoRecurso/{%s}protocoloRecebimento' % (NS, NS)
        )
        protocol = (
            value(node, 'nrProtocoloRecursoGlosa')
            if node is not None
            else None
        )
        if (
            not protocol
            or value(node, 'nrProtocoloRecursado') != source['protocolo']
        ):
            raise TissError(
                'Resposta sem protocolo correspondente; confira o portal.'
            )
        row.recurso_estado, row.recurso_protocolo = 'protocolado', protocol
        event(db, row, 'protocolado', {'protocolo': protocol}, user_id)
        db.commit()
        return row
    except Exception as exc:
        # Safe cause for diagnosis: TissError text never carries credentials.
        cause = (
            str(exc)[:300]
            if isinstance(exc, TissError)
            else type(exc).__name__
        )
        db.rollback()
        row = db.scalar(
            select(PetrobrasGlosa)
            .where(PetrobrasGlosa.id == item_id)
            .with_for_update()
        )
        row.recurso_estado = 'envio_incerto'
        for a in db.scalars(
            select(PetrobrasAnexo).where(
                PetrobrasAnexo.glosa_id == item_id,
                PetrobrasAnexo.estado == 'transmitindo',
            )
        ):
            a.estado = 'envio_incerto'
        event(
            db,
            row,
            'envio_incerto',
            {
                'orientacao': (
                    'Conferir protocolo no portal antes de qualquer reenvio.'
                ),
                'causa': cause,
            },
            user_id,
        )
        db.commit()
        raise TissError(
            'Envio não confirmado. Consulte o portal; não haverá reenvio '
            f'automático. Motivo: {cause}'
        ) from None


def poll_appeal(db, config, row, user_id=None, client=None):
    if not row.recurso_protocolo:
        raise TissError(
            'Protocolo de recurso não disponível; confira o portal.'
        )
    answer = (client or PetrobrasClient(config)).status(row.recurso_protocolo)
    status_node = answer.find('.//{%s}reciboGlosaStatus' % NS)
    receipt = answer.find('.//{%s}reciboGlosa' % NS)
    if status_node is not None:
        if (
            value(status_node, 'nrProtocoloRecursoGlosa')
            != row.recurso_protocolo
        ):
            raise TissError('Resposta de outro protocolo recusada.')
        result = {
            'situacao_protocolo': value(status_node, 'situacaoProtocolo'),
            'data_situacao': value(status_node, 'dataSituacao'),
        }
        row.recurso_estado = 'em_analise'
    elif receipt is not None:
        if value(receipt, 'numeroGuiaRecGlosaPrestador') != row.recurso_guia:
            raise TissError('Resposta de outra guia de recurso recusada.')
        # Preserve each partial acceptance instead of labeling all accepted.
        result = {
            'valor_recursado': value(receipt, 'valorTotalRecursado'),
            'valor_acatado': value(receipt, 'valorTotalAcatado'),
            'itens': [],
        }
        for node in receipt.findall('.//{%s}respostaGuiaItens' % NS):
            result['itens'].append(response_data(node))
        row.recurso_estado = 'respondido'
    else:
        raise TissError('Resposta de recurso não reconhecida.')
    if row.recurso_resultado != result:
        event(db, row, 'situacao_recurso', result, user_id)
    row.recurso_resultado, row.ultima_consulta_recurso = result, utcnow()
    db.commit()
    return row


def response_data(node):
    """Preserve order and repeated TISS response elements."""
    if len(node) == 0:
        return node.text
    result = {}
    for child in node:
        result.setdefault(child.tag.split('}')[-1], []).append(
            response_data(child)
        )
    return result
