"""Explicit submission with durable intent and no retries after uncertainty."""

from copy import deepcopy
from datetime import datetime
from decimal import Decimal
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import func, or_, select

from app_prontocardio.models import Tiss
from app_prontocardio.petrobras_models import (
    PetrobrasAnexo,
    PetrobrasEvento,
    PetrobrasGlosa,
    utcnow,
)
from app_prontocardio.services.petrobras_portal_appeal import (
    DOCUMENT_TYPES,
    MIN_JUSTIFICATION,
    PortalAppeal,
    is_accepted,
    is_denied,
    is_saved,
    same_value,
)
from app_prontocardio.services.petrobras_sync import event
from app_prontocardio.services.petrobras_tiss import (
    NS,
    PetrobrasClient,
    TissError,
    TissRejected,
    appeal_block,
    operator_codes,
    value,
)

APPEAL_ID_DIGITS = 20  # numeroGuiaRecGlosaPrestador: st_texto20.
ACATO_EVENTS = ('acato', 'acato_removido')


def acatos(db, ids):
    """Acato recorded here, sent on the payer's screen: latest per item."""
    ids = list(ids)
    if not ids:
        return {}
    last = (
        select(func.max(PetrobrasEvento.id))
        .where(
            PetrobrasEvento.glosa_id.in_(ids),
            PetrobrasEvento.tipo.in_(ACATO_EVENTS),
        )
        .group_by(PetrobrasEvento.glosa_id)
    )
    return {
        e.glosa_id: e.dados
        for e in db.scalars(
            select(PetrobrasEvento).where(PetrobrasEvento.id.in_(last))
        )
        if e.tipo == 'acato'
    }


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


def send_appeal(db, config, row, version, user_id, client=None):  # noqa: PLR0913 - explicit audit context
    """Single item: a protocol appeal with one item."""
    return send_protocol_appeal(db, config, [(row, version)], user_id, client)[
        0
    ]


def check_draft(row, version, today):
    if row.versao != version or row.recurso_estado != 'rascunho':
        raise TissError(
            'Recurso alterado ou transmissão iniciada. Consulte a situação.'
        )
    if (
        not row.presente_na_fonte
        or not (row.justificativa or '').strip()
        or Decimal(row.valor_glosa or 0) <= 0
    ):
        raise TissError('Revise a glosa e preencha a justificativa.')
    if row.prazo is None or row.prazo < today or not row.origem_prazo:
        raise TissError(
            'Confirme o prazo vigente e sua origem antes do envio.'
        )


def send_protocol_appeal(db, config, pairs, user_id, client=None):  # noqa: PLR0912, PLR0914, PLR0915 - durable transmission state transitions
    """One TISS appeal for items of one lot/protocol; never retried.

    pairs: [(row, version)] already locked by the caller. All items share
    the appeal number and its outcome (protocol or envio incerto).
    """
    if not config.transmission_enabled:
        raise TissError(
            (
                'Envio depende da validação da '
                'integração Petrobras neste ambiente.'
            )
        )
    today = datetime.now(ZoneInfo('America/Sao_Paulo')).date()
    for row, version in pairs:
        check_draft(row, version, today)
    rows = [row for row, _ in pairs]
    files = {
        row.id: list(
            db.scalars(
                select(PetrobrasAnexo).where(PetrobrasAnexo.glosa_id == row.id)
            )
        )
        for row in rows
    }
    if any(
        a.estado in {'transmitindo', 'envio_incerto'}
        for f in files.values()
        for a in f
    ):
        raise TissError(
            'Documento com envio incerto; confira o portal antes de reenviar.'
        )
    entries = [(deepcopy(r.fonte), r.justificativa) for r in rows]
    protocolo = entries[0][0].get('protocolo')
    # Numeric guide number: operator systems may not accept letters here.
    appeal_id = next(
        (r.recurso_guia for r in rows if (r.recurso_guia or '').isdigit()),
        str(uuid4().int % 10**APPEAL_ID_DIGITS).zfill(APPEAL_ID_DIGITS),
    )
    codes = appeal_codes(db, config, [s for s, _ in entries])
    preflight = PetrobrasClient(config, dry_run=True)
    preflight.appeal_protocol(entries, appeal_id, codes)
    for row, (source, _) in zip(rows, entries):
        for a in files[row.id]:
            preflight.document(source, a, (appeal_id, '0'))
    ids = [row.id for row in rows]
    for row, (source, justification) in zip(rows, entries):
        row.recurso_guia = appeal_id
        row.recurso_estado = 'transmitindo'
        row.versao += 1
        event(
            db,
            row,
            'transmissao_iniciada',
            {
                'guia_recurso': appeal_id,
                'itens_no_recurso': len(rows),
                'fonte': source,
                'justificativa': justification,
            },
            user_id,
        )
    db.commit()  # before network: a crash cannot trigger a blind resend
    client = client or PetrobrasClient(config)
    try:
        answer = client.appeal_protocol(entries, appeal_id, codes)
        node = answer.find(
            '{%s}recebimentoRecurso/{%s}protocoloRecebimento' % (NS, NS)
        )
        protocol = (
            value(node, 'nrProtocoloRecursoGlosa')
            if node is not None
            else None
        )
        if not protocol or value(node, 'nrProtocoloRecursado') != protocolo:
            raise TissError(
                'Resposta sem protocolo correspondente; confira o portal.'
            )
        for row in rows:
            row.recurso_estado, row.recurso_protocolo = 'protocolado', protocol
            event(db, row, 'protocolado', {'protocolo': protocol}, user_id)
        db.commit()
    except Exception as exc:  # noqa: BLE001 - every failure is recorded
        if config.portal_fallback:
            return appeal_by_portal(db, config, ids, exc, appeal_id, user_id)
        failed(db, exc, ids, appeal_id, user_id)
    # Proofs cite the appeal received above; a failure never undoes it.
    for row, (source, _) in zip(rows, entries):
        for a in files[row.id]:
            if a.estado != 'protocolado':
                send_document(db, client, row, source, a, user_id)
    if config.portal_fallback:
        accept_on_screen(db, config, rows, user_id)
    return rows


def locked_rows(db, ids):
    return list(
        db.scalars(
            select(PetrobrasGlosa)
            .where(PetrobrasGlosa.id.in_(ids))
            .order_by(PetrobrasGlosa.id)
            .with_for_update()
        )
    )


def find_lot(portal, source):
    """Default values and account holding this guide in the demonstrativo."""
    for lot in portal.lots(source['demonstrativo']):
        for account in portal.accounts(lot):
            if account.get('numeroContaPrestador') == source.get(
                'guia_prestador'
            ) and account.get('numeroContaOperadora') == source.get(
                'guia_operadora'
            ):
                return lot, account
    raise TissError('Guia não localizada no extrato do portal.')


def find_item(portal, lot, account, source):
    matches = [
        i
        for i in portal.items(lot, account)
        if is_denied(i)
        and i.get('codigoItem') == source.get('tuss')
        and same_value(i, source.get('valor_glosa'))
    ]
    if len(matches) != 1:
        raise TissError(
            f'Item {source.get("tuss")} sem correspondência única no portal.'
        )
    return matches[0]


def item_key(item):
    return (
        item.get('numeroContaPrestador'),
        item.get('codigoItem'),
        Decimal(str(item.get('valorGlosado') or 0)),
    )


def row_key(row):
    return (row.guia_prestador, row.tuss, Decimal(str(row.valor_glosa or 0)))


def fully_accepted(row, acato):
    """The screen accepts a whole item; a partial acato cannot be sent."""
    return Decimal(str((acato or {}).get('valor') or 0)) >= Decimal(
        str(row.valor_glosa or 0)
    )


def accepted_rows(db, demonstrativo):
    """Rows fully marked as acato in the demonstrativo, by screen key."""
    rows = list(
        db.scalars(
            select(PetrobrasGlosa).where(
                PetrobrasGlosa.demonstrativo == demonstrativo,
                PetrobrasGlosa.presente_na_fonte.is_(True),
            )
        )
    )
    accepted = acatos(db, (r.id for r in rows))
    by_key = {}
    for row in rows:
        if row.id in accepted and fully_accepted(row, accepted[row.id]):
            by_key.setdefault(row_key(row), []).append(row)
    return by_key


def lot_items(portal, lot):
    return [
        item
        for account in portal.accounts(lot)
        for item in portal.items(lot, account)
    ]


def accept_treated(db, portal, lot, demonstrativo, user_id, appealing=()):  # noqa: PLR0913, PLR0917 - audit context
    """Aceitar Selecionados: items marked as acato, except those appealing."""
    by_key = accepted_rows(db, demonstrativo)
    for item in lot_items(portal, lot):
        if (
            not is_denied(item)
            or is_saved(item)
            or is_accepted(item)
            or item.get('numeroItem') in appealing
        ):
            continue
        rows = by_key.get(item_key(item))
        if not rows:
            continue
        portal.accept(lot, item)
        for row in rows:
            event(
                db,
                row,
                'aceito_portal',
                {'numero_item': item.get('numeroItem')},
                user_id,
            )
        db.commit()  # Each accept stays recorded if a later one fails.


def was_sent(item):
    """Efetivar assigns these; a saved item has neither."""
    return bool(item.get('protocoloRecurso') or item.get('efetivado'))


def appeal_protocols(portal, lot, rows):
    """Protocolo de Recurso the portal assigned to each row, when listed."""
    numbers = {
        item_key(item): item.get('protocoloRecurso')
        for item in lot_items(portal, lot)
        if is_saved(item) and item.get('protocoloRecurso')
    }
    return {r.id: numbers.get(row_key(r)) for r in rows}


def submit_lot(db, portal, lot, account, rows, user_id):  # noqa: PLR0913, PLR0917 - audit context
    """Efetivar, then confirm in the portal; unconfirmed stays uncertain."""
    try:
        answer = portal.efetivar(lot)
        refreshed = {a['numeroConta']: a for a in portal.accounts(lot)}.get(
            account['numeroConta'], {}
        )
        sent = refreshed.get('qtdItemEfetivado') or any(
            was_sent(i) for i in lot_items(portal, lot)
        )
        if not sent:
            raise TissError('Portal não confirmou a efetivação.')
        protocols = appeal_protocols(portal, lot, rows)
    except Exception as exc:  # noqa: BLE001 - Efetivar may have been applied
        reason = str(exc)[:300] if isinstance(exc, TissError) else 'falha'
        for row in rows:
            row.recurso_estado = 'envio_incerto'
            event(db, row, 'envio_incerto', {'causa': reason}, user_id)
        db.commit()
        raise TissError(
            'Envio pelo portal não confirmado. Confira a capa de lote '
            f'{account.get("numeroCapaLotePrestador")} no portal; não haverá '
            f'reenvio automático. Motivo: {reason}'
        ) from None
    for row in rows:
        row.recurso_estado = 'enviado_portal'
        protocol = protocols.get(row.id)
        row.recurso_protocolo = str(protocol) if protocol else None
        event(
            db,
            row,
            'enviado_portal',
            {
                'capa_lote': account.get('numeroCapaLotePrestador'),
                'protocolo': row.recurso_protocolo,
                'retorno': str(answer.get('mensagemRetorno') or '')[:300],
            },
            user_id,
        )
    for attachment in db.scalars(
        select(PetrobrasAnexo).where(
            PetrobrasAnexo.glosa_id.in_([r.id for r in rows])
        )
    ):
        attachment.estado = 'protocolado'
    db.commit()


def appeal_by_portal(db, config, ids, cause, appeal_id, user_id, portal=None):  # noqa: PLR0912, PLR0913, PLR0917 - audit context
    """Webservice failed: same appeal through the payer's own screen."""
    db.rollback()
    rows = locked_rows(db, ids)
    files = {
        r.id: list(
            db.scalars(
                select(PetrobrasAnexo).where(PetrobrasAnexo.glosa_id == r.id)
            )
        )
        for r in rows
    }
    saved = []
    client = portal or PortalAppeal(config)
    try:
        for row in rows:
            if len((row.justificativa or '').strip()) < MIN_JUSTIFICATION:
                raise TissError(
                    'O portal exige justificativa com ao menos '
                    f'{MIN_JUSTIFICATION} caracteres.'
                )
            if any(a.mime not in DOCUMENT_TYPES for a in files[row.id]):
                raise TissError(
                    'O portal aceita comprovantes PDF, JPG ou BMP.'
                )
        located = [(r, *find_lot(client, r.fonte)) for r in rows]
        targets = [
            (r, lot, a, find_item(client, lot, a, r.fonte))
            for r, lot, a in located
        ]
        # The account counts saved items too, so one item at a time still
        # works; only what Efetivar already sent blocks a new save.
        if any(
            a.get('qtdItemEfetivado') or was_sent(i) for *_, a, i in targets
        ):
            raise TissError('Conta já tem item enviado no portal; confira.')
        by_lot = {}
        for row, lot, _, _ in targets:
            by_lot.setdefault(lot['numeroLote'], (lot, []))[1].append(row)
        appealing = {item.get('numeroItem') for *_, item in targets}
        # The screen's order: Aceitar Selecionados, then Salvar Recurso.
        for lot, lot_rows in by_lot.values():
            accept_treated(
                db, client, lot, lot_rows[0].demonstrativo, user_id, appealing
            )
        for row, lot, account, item in targets:
            if not is_saved(item):
                client.save(lot, item, row.justificativa, files[row.id])
                if not is_saved(find_item(client, lot, account, row.fonte)):
                    raise TissError('Portal não confirmou o recurso salvo.')
            row.recurso_estado, row.recurso_guia = 'salvo_portal', None
            event(
                db,
                row,
                'salvo_portal',
                {
                    'capa_lote': account.get('numeroCapaLotePrestador'),
                    'numero_item': item.get('numeroItem'),
                    'motivo_webservice': str(cause)[:300],
                },
                user_id,
            )
            db.commit()
            saved.append(row.id)
        return rows
    except Exception as exc:  # noqa: BLE001 - recorded on the items
        if any(
            r.recurso_estado in {'envio_incerto', 'enviado_portal'}
            for r in rows
        ):
            raise
        pending = [i for i in ids if i not in saved]
        detail = str(exc)[:300] if isinstance(exc, TissError) else 'falha'
        if pending:
            failed(
                db,
                cause,
                pending,
                appeal_id,
                user_id,
                f' Portal: {detail}',
            )
        raise TissError(f'Recurso salvo no portal; envio pendente: {detail}')
    finally:
        client.close()


def accept_on_screen(db, config, rows, user_id, portal=None):
    """Webservice protocolled: the lot's acatos still go to the screen.

    Never Efetivar here: the screen may not list what the webservice received
    and would mark those items as accepted. A failure never undoes the appeal.
    """
    client = None
    try:
        client = portal or PortalAppeal(config)
        seen = set()
        for row in rows:
            lot, _ = find_lot(client, row.fonte)
            if lot['numeroLote'] not in seen:
                seen.add(lot['numeroLote'])
                accept_treated(db, client, lot, row.demonstrativo, user_id)
    except Exception as exc:  # noqa: BLE001 - recorded on the items
        db.rollback()
        reason = str(exc)[:300] if isinstance(exc, TissError) else 'falha'
        for row in rows:
            event(db, row, 'aceite_portal_falhou', {'causa': reason}, user_id)
        db.commit()
    finally:
        if client:
            client.close()


def efetivar_preview(db, config, row, portal=None):
    """What Efetivar would do to the lot; asked of the user before it runs."""
    if row.recurso_estado != 'salvo_portal':
        raise TissError('Recurso não está salvo no portal.')
    client = portal or PortalAppeal(config)
    try:
        lot, _ = find_lot(client, row.fonte)
        denied = [i for i in lot_items(client, lot) if is_denied(i)]
        marked = accepted_rows(db, row.demonstrativo)
    finally:
        client.close()
    saved = [i for i in denied if is_saved(i)]
    accepted = [
        i
        for i in denied
        if not is_saved(i) and (is_accepted(i) or item_key(i) in marked)
    ]
    untreated = [i for i in denied if i not in saved and i not in accepted]
    return {
        'lote': (row.fonte or {}).get('lote'),
        'recursos': len(saved),
        'acatos': len(accepted),
        'nao_recursados': [
            {
                'guia': i.get('numeroContaPrestador'),
                'tuss': i.get('codigoItem'),
                'descricao': i.get('descricaoItem'),
                'valor_glosa': i.get('valorGlosado'),
            }
            for i in untreated
        ],
    }


def record_accepted(db, row, keys, user_id):
    """Rows of the lot Efetivar accepted can no longer be appealed here."""
    rows = [
        r
        for r in db.scalars(
            select(PetrobrasGlosa).where(
                PetrobrasGlosa.demonstrativo == row.demonstrativo,
                PetrobrasGlosa.presente_na_fonte.is_(True),
                PetrobrasGlosa.recurso_estado == 'rascunho',
            )
        )
        if (r.fonte or {}).get('lote') == (row.fonte or {}).get('lote')
        and row_key(r) in keys
    ]
    current = acatos(db, (r.id for r in rows))
    today = datetime.now(ZoneInfo('America/Sao_Paulo')).date()
    for accepted in rows:
        if not fully_accepted(accepted, current.get(accepted.id)):
            source = accepted.fonte or {}
            event(
                db,
                accepted,
                'acato',
                {
                    'data': today.isoformat(),
                    'quantidade': str(source.get('quantidade') or 1),
                    'valor': str(accepted.valor_glosa),
                    'lote': source.get('lote'),
                    'processo': None,
                    'descricao': 'Aceito ao efetivar o lote no portal.',
                },
                user_id,
            )
        accepted.recurso_estado = 'aceito_portal'
        event(db, accepted, 'aceito_efetivacao', {}, user_id)
    db.commit()


def efetivar_portal(db, config, row, user_id, portal=None):
    """Only on the user's request: every unappealed item becomes accepted."""
    if row.recurso_estado != 'salvo_portal':
        raise TissError('Recurso não está salvo no portal.')
    client = portal or PortalAppeal(config)
    try:
        lot, account = find_lot(client, row.fonte)
        lot_rows = [
            r
            for r in db.scalars(
                select(PetrobrasGlosa)
                .where(
                    PetrobrasGlosa.demonstrativo == row.demonstrativo,
                    PetrobrasGlosa.recurso_estado == 'salvo_portal',
                )
                .with_for_update()
            )
            if (r.fonte or {}).get('lote') == (row.fonte or {}).get('lote')
        ]
        accept_treated(db, client, lot, row.demonstrativo, user_id)
        accepted_keys = {
            item_key(i)
            for i in lot_items(client, lot)
            if is_denied(i) and not is_saved(i)
        }
        submit_lot(db, client, lot, account, lot_rows, user_id)
        record_accepted(db, row, accepted_keys, user_id)
    finally:
        client.close()
    return row


def send_document(db, client, row, source, attachment, user_id):  # noqa: PLR0913, PLR0917 - audit context
    """Send one proof of a protocolled appeal; record, never raise."""
    attachment.estado = 'transmitindo'
    db.commit()
    try:
        answer = client.document(
            source, attachment, (row.recurso_guia, row.recurso_protocolo)
        )
        receipt = value(
            answer, 'recebimentoDocumento/reciboDocumentos/protocoloDoc'
        )
        doc_id = value(
            answer, 'recebimentoDocumento/reciboDocumentos/numeroDocumento'
        )
        if not receipt or doc_id != str(attachment.id):
            raise TissError('Documento sem recibo correspondente.')
    except Exception as exc:  # noqa: BLE001 - recorded on the attachment
        db.rollback()
        cause = (
            str(exc)[:300]
            if isinstance(exc, TissError)
            else type(exc).__name__
        )
        attachment.estado = (
            'rascunho' if isinstance(exc, TissRejected) else 'envio_incerto'
        )
        event(
            db,
            row,
            'documento_nao_enviado',
            {'anexo_id': attachment.id, 'causa': cause},
            user_id,
        )
        db.commit()
        return
    attachment.estado, attachment.protocolo = 'protocolado', receipt
    event(
        db,
        row,
        'documento_protocolado',
        {'anexo_id': attachment.id, 'protocolo': receipt},
        user_id,
    )
    db.commit()


def failed(db, exc, ids, appeal_id, user_id, detail=''):  # noqa: PLR0913, PLR0917 - audit context
    """Appeal not confirmed: refused back to draft, otherwise uncertain."""
    # Safe cause for diagnosis: TissError text never carries credentials.
    cause = (
        str(exc)[:300] if isinstance(exc, TissError) else type(exc).__name__
    ) + detail
    db.rollback()
    rows = list(
        db.scalars(
            select(PetrobrasGlosa)
            .where(PetrobrasGlosa.id.in_(ids))
            .order_by(PetrobrasGlosa.id)
            .with_for_update()
        )
    )
    if isinstance(exc, TissRejected):
        rejected(db, rows, ids, cause, appeal_id, user_id)
        raise TissError(
            f'Recurso não aceito pela Petrobras. Motivo: {cause} '
            'Corrija e transmita novamente.'
        ) from None
    for row in rows:
        row.recurso_estado = 'envio_incerto'
        event(
            db,
            row,
            'envio_incerto',
            {
                'orientacao': (
                    'Conferir protocolo no portal antes de qualquer reenvio.'
                ),
                'causa': cause,
                'guia_recurso': appeal_id,
            },
            user_id,
        )
    db.commit()
    raise TissError(
        'Envio não confirmado. Consulte o portal; não haverá reenvio '
        f'automático. Motivo: {cause}'
    ) from None


def rejected(db, rows, ids, cause, appeal_id, user_id):  # noqa: PLR0913, PLR0917 - audit context
    """SOAP fault: the operator refused it, so the draft can be fixed."""
    for row in rows:
        row.recurso_estado, row.recurso_guia = 'rascunho', None
        event(
            db,
            row,
            'recusado_operadora',
            {'causa': cause, 'guia_recurso': appeal_id},
            user_id,
        )
    for a in db.scalars(
        select(PetrobrasAnexo).where(
            PetrobrasAnexo.glosa_id.in_(ids),
            PetrobrasAnexo.estado == 'transmitindo',
        )
    ):
        a.estado = 'rascunho'
    db.commit()


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
