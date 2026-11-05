"""dlt pipeline: portal analysis of lots whose glosa came only per guide.

1. Lots with "Glosa da guia (itens não detalhados)" not yet detailed.
2. dlt loads each sanitized portal XML into petrobras_portal.analise_conta_lote
   (merge by lote), keeping the raw source auditable and reprocessable.
3. The stored XML goes through the same TISS parser and import as the
   webservice analysis; item rows then hide the untouched guide rows.

Read-only towards the payer. Runs only on explicit request.
"""

import hashlib
import os
import shutil
import tempfile

os.environ.setdefault('RUNTIME__DLTHUB_TELEMETRY', 'false')

import dlt  # noqa: E402 - telemetry must be disabled before import
from lxml import etree as ET  # noqa: E402
from sqlalchemy import select  # noqa: E402
from sqlalchemy.exc import SQLAlchemyError  # noqa: E402

from app_prontocardio import database  # noqa: E402
from app_prontocardio.petrobras_models import (  # noqa: E402
    PetrobrasExecucao,
    PetrobrasGlosa,
    PetrobrasProtocolo,
    utcnow,
)
from app_prontocardio.services.petrobras_match import (  # noqa: E402
    GUIDE_ONLY_SCOPE,
    provider_guide,
)
from app_prontocardio.services.petrobras_portal import (  # noqa: E402
    PortalClient,
)
from app_prontocardio.services.petrobras_sync import (  # noqa: E402
    guide_senhas,
    import_rows,
    matching_source,
)
from app_prontocardio.services.petrobras_tiss import (  # noqa: E402
    TissError,
    parse_demonstrativos,
    parse_xml,
)

DATASET = 'petrobras_portal'
TABLE = 'analise_conta_lote'
MAX_LOTS = 20  # Fits the 120 s frontend bridge; later runs take the rest.
SOURCE = 'portal_analise_conta'


@dlt.resource(name=TABLE, write_disposition='merge', primary_key='lote')
def analise_conta_lote(client, lots, errors):
    for lote, protocolo in lots.items():
        try:
            xml = ET.tostring(client.analysis(lote), encoding='unicode')
        except TissError as exc:
            errors[lote] = str(exc)
            continue
        yield {
            'lote': lote,
            'protocolo': protocolo,
            'xml': xml,
            'sha256': hashlib.sha256(xml.encode()).hexdigest(),
            'baixado_em': utcnow(),
        }


def pending_lots(session, prestador):
    """{lote: protocolo} of present guide glosas without item detail."""
    rows = session.execute(
        select(
            PetrobrasGlosa.protocolo_id,
            PetrobrasGlosa.guia_prestador,
            PetrobrasGlosa.fonte,
        )
        .join(PetrobrasProtocolo)
        .where(
            PetrobrasProtocolo.prestador == prestador,
            PetrobrasGlosa.presente_na_fonte.is_(True),
        )
    ).all()
    detailed = {
        (pid, provider_guide(guia))
        for pid, guia, fonte in rows
        if fonte.get('escopo') != GUIDE_ONLY_SCOPE
    }
    lots = {}
    for pid, guia, fonte in rows:
        if (
            fonte.get('escopo') == GUIDE_ONLY_SCOPE
            and fonte.get('lote')
            and (pid, provider_guide(guia)) not in detailed
        ):
            lots.setdefault(str(fonte['lote']), fonte.get('protocolo'))
    return dict(sorted(lots.items())[:MAX_LOTS])


def make_pipeline(destination, pipelines_dir):
    if destination is None:
        destination = dlt.destinations.sqlalchemy(database.postgres_engine)
    return dlt.pipeline(
        pipeline_name='petrobras_portal',
        destination=destination,
        dataset_name=DATASET,
        pipelines_dir=pipelines_dir,
    )


def stored_xml(pipeline, lots):
    with pipeline.sql_client() as client:
        table = client.make_qualified_table_name(TABLE)
        rows = client.execute_sql(f'SELECT lote, xml FROM {table}')  # noqa: S608 - dlt-qualified table name, no input
    return {lote: xml for lote, xml in rows or [] if lote in lots}


def import_lot(session, config, lote, protocolo, xml, oracle_loader, user_id):  # noqa: PLR0913, PLR0917 - explicit source and audit context
    rows, scopes = parse_demonstrativos(
        parse_xml(xml.encode()), config.prestador_cnpj, config.registro_ans
    )
    returned = {number for number, _ in scopes}
    if returned != {protocolo}:
        raise TissError('Portal retornou protocolo diferente do lote.')
    protocol = session.scalars(
        select(PetrobrasProtocolo).where(
            PetrobrasProtocolo.prestador == config.prestador_cnpj,
            PetrobrasProtocolo.numero == protocolo,
        )
    ).one()
    for row in rows:
        row['origem'] = SOURCE
        # The portal was asked for this lot: it is the payer's number.
        row['lote_prestador'] = row.get('lote')
        row['lote'] = lote
    senhas = guide_senhas(session, [protocol.id], rows)
    oracle_error = None
    try:
        oracle_rows = oracle_loader(
            [matching_source(r, senhas) for r in rows], config
        )
    except (SQLAlchemyError, ValueError):
        oracle_rows = []
        oracle_error = 'Oracle indisponível; último cruzamento preservado.'
    # Not authoritative for removal: guide rows stay, item rows hide them.
    return import_rows(
        session,
        protocol,
        rows,
        set(),
        oracle_rows,
        config,
        user_id,
        oracle_error,
        senhas,
    )


def detail_guides(  # noqa: PLR0913 - injected sources and audit context
    session,
    config,
    oracle_loader,
    user_id=None,
    client=None,
    destination=None,
):
    """Fetch the portal analysis of pending guide lots and import items."""
    config.require_portal()
    run = PetrobrasExecucao(usuario_id=user_id)
    session.add(run)
    session.commit()
    lots = pending_lots(session, config.prestador_cnpj)
    failures = {}
    workdir = tempfile.mkdtemp(prefix='petrobras-portal-')
    try:
        if lots:
            portal = client or PortalClient(config)
            pipeline = make_pipeline(destination, workdir)
            try:
                pipeline.run(analise_conta_lote(portal, lots, failures))
            finally:
                portal.logout()
            downloaded = set(lots) - set(failures)
            loaded = stored_xml(pipeline, downloaded) if downloaded else {}
        else:
            loaded = {}
        for lote, xml in loaded.items():
            try:
                run.quantidade += import_lot(
                    session,
                    config,
                    lote,
                    lots[lote],
                    xml,
                    oracle_loader,
                    user_id,
                )
                session.commit()
            except (TissError, SQLAlchemyError) as exc:
                session.rollback()
                run = session.get(PetrobrasExecucao, run.id)
                failures[lote] = (
                    str(exc)
                    if isinstance(exc, TissError)
                    else 'Falha ao importar o lote; dados preservados.'
                )
    except Exception:
        session.rollback()
        run = session.get(PetrobrasExecucao, run.id)
        run.situacao, run.fim = 'falha', utcnow()
        run.erro = 'Falha ao detalhar pelo portal; dados preservados.'
        session.commit()
        raise TissError(run.erro) from None
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    run.situacao = 'parcial' if failures else 'concluida'
    run.erro = (
        '; '.join(f'lote {k}: {v}' for k, v in failures.items())[:200] or None
    )
    run.fim = utcnow()
    session.commit()
    return {
        'execucao_id': run.id,
        'situacao': run.situacao,
        'lotes': len(lots),
        'lotes_detalhados': len(loaded) - len(set(failures) & set(loaded)),
        'quantidade': run.quantidade,
        'falhas': failures,
    }
