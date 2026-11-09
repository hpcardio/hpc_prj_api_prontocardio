"""Bring to Receita Certa what was done directly on the payer's portal.

Read-only on the portal: lists the lots of each demonstrativo that still has
open items here and records the screen's situation (saved, accepted,
appealed, efetivado) on the matching rows. Never writes to the portal.
"""

import time
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app_prontocardio.petrobras_models import (
    PetrobrasGlosa,
    PetrobrasProtocolo,
)
from app_prontocardio.services.petrobras_appeals import (
    acatos,
    account_groups,
    fully_accepted,
    item_key,
    row_key,
    was_sent,
)
from app_prontocardio.services.petrobras_portal_appeal import (
    ACCEPTED,
    APPEALED,
    PortalAppeal,
    is_denied,
)
from app_prontocardio.services.petrobras_sync import event
from app_prontocardio.services.petrobras_tiss import TissError

# States the portal may still change; webservice and final ones are kept.
OPEN_STATES = ('rascunho', 'salvo_portal', 'envio_incerto', 'transmitindo')
# Below the screen proxy's wait; the screen calls again with `proximo`.
TIME_BUDGET_SECONDS = 25


def open_demonstrativos(db, config):
    """Demonstrativos with items still open here, most recent first."""
    return list(
        db.scalars(
            select(PetrobrasGlosa.demonstrativo)
            .join(PetrobrasProtocolo)
            .where(
                PetrobrasProtocolo.prestador == config.prestador_cnpj,
                PetrobrasGlosa.presente_na_fonte.is_(True),
                PetrobrasGlosa.demonstrativo.is_not(None),
                PetrobrasGlosa.recurso_estado.in_(OPEN_STATES),
            )
            .distinct()
            .order_by(PetrobrasGlosa.demonstrativo.desc())
        )
    )


def demonstrativo_order(value):
    """Newest first: numeric demonstrativos compare as numbers."""
    return (1, int(value), '') if value.isdigit() else (0, 0, value)


def sync_portal_situation(  # noqa: PLR0913 - cursor, budget and injections
    db,
    config,
    user_id,
    after=None,
    budget=TIME_BUDGET_SECONDS,
    portal=None,
    clock=time.monotonic,
):
    """Read the portal and record its situation on the open items.

    One call stays short for the screen's proxy: it reads demonstrativos
    until the time budget is spent and returns `proximo`, the cursor for
    the next call (None when all were read).
    """
    pending = sorted(
        (str(d) for d in open_demonstrativos(db, config)),
        key=demonstrativo_order,
        reverse=True,
    )
    if after:
        pending = [
            d
            for d in pending
            if demonstrativo_order(d) < demonstrativo_order(str(after))
        ]
    result = {
        'demonstrativos': 0,
        'atualizados': 0,
        'ambiguos': 0,
        'falhas': {},
        'restantes': len(pending),
        'proximo': None,
    }
    if not pending:
        return result
    start = clock()
    client = portal or PortalAppeal(config)
    try:
        for index, demonstrativo in enumerate(pending):
            if index and clock() - start >= budget:
                result['proximo'] = pending[index - 1]
                break
            result['restantes'] -= 1
            try:
                changed, ambiguous = sync_demonstrativo(
                    db, client, demonstrativo, user_id
                )
                db.commit()
            except Exception as exc:  # noqa: BLE001 - one lot never stops the rest
                db.rollback()
                result['falhas'][demonstrativo] = (
                    str(exc)[:200]
                    if isinstance(exc, TissError)
                    else type(exc).__name__
                )
                continue
            result['demonstrativos'] += 1
            result['atualizados'] += changed
            result['ambiguos'] += ambiguous
    finally:
        client.close()
    return result


def sync_demonstrativo(db, portal, demonstrativo, user_id):
    """(rows changed, portal items matching more than one row)."""
    rows = list(
        db.scalars(
            select(PetrobrasGlosa)
            .where(
                PetrobrasGlosa.demonstrativo == demonstrativo,
                PetrobrasGlosa.presente_na_fonte.is_(True),
            )
            .order_by(PetrobrasGlosa.id)
            .with_for_update()
        )
    )
    by_key = {}
    for row in rows:
        by_key.setdefault(row_key(row), []).append(row)
    current = acatos(db, (r.id for r in rows))
    changed = ambiguous = 0
    for lot in portal.lots(demonstrativo):
        for account, items in account_groups(portal, lot):
            account_sent = bool(account.get('qtdItemEfetivado'))
            for item in items:
                situation = item.get('situacaoRecursadoAceito')
                if not is_denied(item) or situation not in {
                    APPEALED,
                    ACCEPTED,
                }:
                    continue
                matches = by_key.get(item_key(item), [])
                if len(matches) != 1:
                    ambiguous += bool(matches)
                    continue
                sent = was_sent(item) or account_sent
                changed += apply_situation(
                    db, matches[0], item, account, sent, current, user_id
                )
    return changed, ambiguous


def apply_situation(db, row, item, account, sent, current, user_id):  # noqa: PLR0913, PLR0917 - audit context
    """Record the portal's situation of one item; True when it changed."""
    situation = item.get('situacaoRecursadoAceito')
    before = row.recurso_estado
    if before not in OPEN_STATES and before not in {
        'enviado_portal',
        'aceito_portal',
    }:
        return False  # Webservice appeals keep their own protocol and state.
    target = before
    if situation == APPEALED:
        target = 'enviado_portal' if sent else 'salvo_portal'
    elif sent:
        target = 'aceito_portal'
    if before in {'enviado_portal', 'aceito_portal'} and target != before:
        return False  # The portal never reopens what was efetivado.
    protocol = item.get('protocoloRecurso')
    protocol = str(protocol) if situation == APPEALED and protocol else None
    accept = situation == ACCEPTED and not fully_accepted(
        row, current.get(row.id)
    )
    new_protocol = protocol and row.recurso_protocolo != protocol
    if target == before and not new_protocol and not accept:
        return False
    if accept:
        source = row.fonte or {}
        data = {
            'data': datetime
            .now(ZoneInfo('America/Sao_Paulo'))
            .date()
            .isoformat(),
            'quantidade': str(source.get('quantidade') or 1),
            'valor': str(row.valor_glosa),
            'lote': source.get('lote'),
            'processo': None,
            'descricao': 'Aceito no portal Petrobras (situação do portal).',
        }
        event(db, row, 'acato', data, user_id)
        current[row.id] = data
    if target != before:
        row.recurso_estado = target
        row.versao += 1
    if new_protocol:
        row.recurso_protocolo = protocol
    event(
        db,
        row,
        'situacao_portal',
        {
            'origem': 'portal',
            'situacao': situation,
            'efetivado': sent,
            'protocolo': protocol,
            'antes': before,
            'depois': target,
            'numero_item': item.get('numeroItem'),
            'conta': account.get('numeroConta'),
        },
        user_id,
    )
    return True
