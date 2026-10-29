"""Run periodically (e.g. every 15 minutes); due intervals are enforced in DB.
Usage: python -m scripts.sync_petrobras
"""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import or_, select, text
from sqlalchemy.orm import Session

from app_prontocardio.database import postgres_engine
from app_prontocardio.petrobras_models import (
    PetrobrasExecucao,
    PetrobrasGlosa,
    PetrobrasProtocolo,
    utcnow,
)
from app_prontocardio.services.petrobras_appeals import poll_appeal
from app_prontocardio.services.petrobras_config import PetrobrasConfig
from app_prontocardio.services.petrobras_oracle import candidates
from app_prontocardio.services.petrobras_sync import (
    discover_competencia,
    synchronize,
)
from app_prontocardio.services.petrobras_tiss import TissError


def run():  # noqa: PLR0912 - sequential discovery, import and status phases
    config = PetrobrasConfig()
    if not config.enabled:
        return
    config.require_connection()
    if postgres_engine is None or postgres_engine.dialect.name != 'postgresql':
        raise RuntimeError('Configure o PostgreSQL da aplicação.')
    # Session-level advisory lock survives worker commits; release on
    # connection close.
    with postgres_engine.connect() as lock:
        if not lock.scalar(text('SELECT pg_try_advisory_lock(422631, 40100)')):
            return
        try:
            with Session(postgres_engine) as db:
                today = datetime.now(ZoneInfo('America/Sao_Paulo')).date()
                month_index = today.year * 12 + today.month - 1
                for index in range(config.discovery_months):
                    year, month = divmod(month_index - index, 12)
                    comp = f'{year:04}{month + 1:02}'
                    recent = db.scalar(
                        select(PetrobrasExecucao.id)
                        .where(
                            PetrobrasExecucao.competencia == comp,
                            PetrobrasExecucao.inicio
                            > utcnow()
                            - timedelta(minutes=config.interval_minutes),
                        )
                        .limit(1)
                    )
                    if recent:
                        continue
                    try:
                        discover_competencia(db, config, comp)
                    except TissError:
                        pass
                ids = list(
                    db.scalars(
                        select(PetrobrasProtocolo.id)
                        .where(
                            PetrobrasProtocolo.prestador
                            == config.prestador_cnpj,
                            PetrobrasProtocolo.ativo.is_(True),
                            or_(
                                PetrobrasProtocolo.proxima_sincronizacao.is_(
                                    None
                                ),
                                PetrobrasProtocolo.proxima_sincronizacao
                                <= utcnow(),
                            ),
                        )
                        .order_by(
                            PetrobrasProtocolo.proxima_sincronizacao.asc().nullsfirst(),
                            PetrobrasProtocolo.id,
                        )
                        .limit(30)
                    )
                )
                for protocol_id in ids:
                    # Reserve next attempt even on failure; no hot retry loop.
                    protocol = db.scalar(
                        select(PetrobrasProtocolo)
                        .where(PetrobrasProtocolo.id == protocol_id)
                        .with_for_update(skip_locked=True)
                    )
                    if protocol is None:
                        continue
                    protocol.proxima_sincronizacao = utcnow() + timedelta(
                        minutes=config.interval_minutes
                    )
                    db.commit()
                    try:
                        synchronize(db, config, protocol_id, candidates)
                    except TissError:
                        # Safe failure already stored in execution history.
                        pass
                appeals = list(
                    db.scalars(
                        select(PetrobrasGlosa.id)
                        .join(PetrobrasProtocolo)
                        .where(
                            PetrobrasProtocolo.prestador
                            == config.prestador_cnpj,
                            PetrobrasGlosa.recurso_estado.in_([
                                'protocolado',
                                'em_analise',
                            ]),
                            or_(
                                PetrobrasGlosa.ultima_consulta_recurso.is_(
                                    None
                                ),
                                PetrobrasGlosa.ultima_consulta_recurso
                                < utcnow()
                                - timedelta(minutes=config.interval_minutes),
                            ),
                        )
                        .order_by(
                            PetrobrasGlosa.ultima_consulta_recurso.asc().nullsfirst()
                        )
                        .limit(30)
                    )
                )
                for item_id in appeals:
                    row = db.scalar(
                        select(PetrobrasGlosa)
                        .where(PetrobrasGlosa.id == item_id)
                        .with_for_update(skip_locked=True)
                    )
                    if row is None:
                        continue
                    try:
                        poll_appeal(db, config, row)
                    except (TissError, ValueError):
                        db.rollback()
        finally:
            lock.execute(text('SELECT pg_advisory_unlock(422631, 40100)'))


if __name__ == '__main__':
    run()
