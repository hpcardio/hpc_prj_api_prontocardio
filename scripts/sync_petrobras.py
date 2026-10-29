"""Run periodically (e.g. every 15 minutes); due intervals are enforced in DB.
Usage: python -m scripts.sync_petrobras
"""

import argparse
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
    synchronize,
    synchronize_competencia,
)
from app_prontocardio.services.petrobras_tiss import (
    COMPETENCE_LENGTH,
    MONTHS_PER_YEAR,
    TissError,
)


def competencias(today, months, inicio=None, fim=None):
    if (inicio is None) != (fim is None):
        raise ValueError('Informe --inicio e --fim juntos (AAAAMM).')
    if inicio is None:
        end = today.year * 12 + today.month - 1
        return [
            f'{year:04}{month + 1:02}'
            for year, month in (divmod(end - i, 12) for i in range(months))
        ]
    for value in (inicio, fim):
        if (
            len(value) != COMPETENCE_LENGTH
            or not value.isascii()
            or not value.isdigit()
            or not 1 <= int(value[4:]) <= MONTHS_PER_YEAR
        ):
            raise ValueError('Competência inválida; informe AAAAMM.')
    start = int(inicio[:4]) * 12 + int(inicio[4:]) - 1
    end = int(fim[:4]) * 12 + int(fim[4:]) - 1
    if not 0 <= end - start < MONTHS_PER_YEAR:
        raise ValueError(
            'Informe um período crescente de até 12 competências.'
        )
    return [
        f'{year:04}{month + 1:02}'
        for year, month in (divmod(i, 12) for i in range(start, end + 1))
    ]


def run(inicio=None, fim=None):  # noqa: PLR0912 - sequential discovery, import and status phases
    config = PetrobrasConfig()
    today = datetime.now(ZoneInfo('America/Sao_Paulo')).date()
    selected = competencias(today, config.discovery_months, inicio, fim)
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
                for comp in selected:
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
                        synchronize_competencia(db, config, comp, candidates)
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
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--inicio', help='Competência inicial AAAAMM')
    parser.add_argument('--fim', help='Competência final AAAAMM')
    args = parser.parse_args()
    run(args.inicio, args.fim)
