from datetime import timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from app_prontocardio.petrobras_models import (
    PetrobrasEvento,
    PetrobrasExecucao,
    PetrobrasGlosa,
    PetrobrasProtocolo,
    utcnow,
)
from app_prontocardio.services.petrobras_match import reconcile
from app_prontocardio.services.petrobras_tiss import (
    PetrobrasClient,
    TissError,
    parse_demonstrativos,
    payment_protocols,
)


def event(session, row, kind, data, user_id=None):
    session.add(
        PetrobrasEvento(
            glosa_id=row.id, tipo=kind, dados=data, usuario_id=user_id
        )
    )


def import_rows(  # noqa: PLR0913 - explicit source, Oracle and audit context
    session,
    protocol,
    rows,
    scopes,
    oracle_rows,
    config,
    user_id=None,
    oracle_error=None,
):
    existing = {
        r.chave: r
        for r in session.scalars(
            select(PetrobrasGlosa).where(
                PetrobrasGlosa.protocolo_id == protocol.id
            )
        )
    }
    seen = set()
    count = 0
    for source in rows:
        if source['protocolo'] != protocol.numero:
            continue
        key = source['chave']
        seen.add(key)
        row = existing.get(key)
        # Keep resolved items if previously imported; don't flood UI with
        # never-glosed items.
        if (
            row is None
            and Decimal(source['valor_glosa']) == 0
            and not source.get('motivos')
        ):
            continue
        if row is None:
            row = PetrobrasGlosa(
                chave=key,
                protocolo_id=protocol.id,
                demonstrativo=source['demonstrativo'],
                fonte=source,
            )
            session.add(row)
            session.flush()
            event(session, row, 'importado', {'fonte': source}, user_id)
        elif row.fonte != source or not row.presente_na_fonte:
            row.versao += 1
            event(
                session,
                row,
                'fonte_alterada',
                {'antes': row.fonte, 'depois': source},
                user_id,
            )
        row.fonte = source
        row.guia_prestador, row.guia_operadora, row.tuss = (
            source.get('guia_prestador'),
            source.get('guia_operadora'),
            source.get('tuss'),
        )
        row.valor_glosa = Decimal(source['valor_glosa'])
        row.presente_na_fonte = True
        row.atualizado_em = utcnow()
        if oracle_error:
            row.erro_oracle = (
                oracle_error  # Preserve last successful match on outage.
            )
        else:
            match = reconcile(
                source, oracle_rows, config.convenio_id, config.oracle_guia
            )
            new = (match['status'], match['oracle'], match['divergencias'])
            if (row.cruzamento, row.oracle, row.divergencias) != new:
                event(
                    session,
                    row,
                    'cruzamento',
                    {
                        'situacao': match['status'],
                        'oracle': match['oracle'],
                        'divergencias': match['divergencias'],
                    },
                    user_id,
                )
            row.cruzamento, row.oracle, row.divergencias = new
            row.candidatos, row.erro_oracle = match['count'], None
        count += 1
    # Only authoritative returned demonstrativos; absence from a
    # partial/asynchronous response is not removal.
    for key, row in existing.items():
        if (
            key not in seen
            and (protocol.numero, row.demonstrativo) in scopes
            and row.presente_na_fonte
        ):
            row.presente_na_fonte = False
            event(
                session,
                row,
                'ausente_demonstrativo',
                {'demonstrativo': row.demonstrativo},
                user_id,
            )
    return count


def synchronize(  # noqa: PLR0913 - injected data sources and audit user
    session, config, protocol_id, oracle_loader, user_id=None, client=None
):
    config.require_connection()
    run = PetrobrasExecucao(usuario_id=user_id)
    session.add(run)
    session.commit()
    run_id = run.id
    try:
        protocol = session.scalar(
            select(PetrobrasProtocolo)
            .where(
                PetrobrasProtocolo.id == protocol_id,
                PetrobrasProtocolo.prestador == config.prestador_cnpj,
            )
            .with_for_update(nowait=True)
        )
        if protocol is None:
            raise TissError('Protocolo Petrobras não encontrado.')
        response = (client or PetrobrasClient(config)).demonstrativos([
            protocol.numero
        ])
        rows, scopes = parse_demonstrativos(
            response, config.prestador_cnpj, config.registro_ans
        )
        if any(number != protocol.numero for number, _ in scopes):
            raise TissError(
                'Resposta contém protocolo diferente do solicitado.'
            )
        oracle_error = None
        try:
            oracle_rows = oracle_loader(rows, config)
        except (SQLAlchemyError, ValueError):
            oracle_rows = []
            oracle_error = 'Oracle indisponível; último cruzamento preservado.'
        run.quantidade = import_rows(
            session,
            protocol,
            rows,
            scopes,
            oracle_rows,
            config,
            user_id,
            oracle_error,
        )
        protocol.ultima_sincronizacao = utcnow()
        protocol.proxima_sincronizacao = utcnow() + timedelta(
            minutes=config.interval_minutes
        )
        protocol.erro = oracle_error
        run.situacao = 'parcial' if oracle_error else 'concluida'
        run.fim = utcnow()
        session.commit()
        return {
            'execucao_id': run_id,
            'situacao': run.situacao,
            'quantidade': run.quantidade,
        }
    except Exception as exc:
        session.rollback()
        run = session.get(PetrobrasExecucao, run_id)
        safe = (
            str(exc)
            if isinstance(exc, TissError)
            else 'Falha na sincronização; dados anteriores preservados.'
        )
        run.situacao, run.erro, run.fim = 'falha', safe[:200], utcnow()
        # Avoid a second lock wait if another process owns the protocol.
        session.commit()
        raise TissError(safe) from None


def discover_competencia(
    session, config, competencia, user_id=None, client=None
):
    """Discover payer protocols at Petrobras, never from Oracle."""
    config.require_connection()
    run = PetrobrasExecucao(usuario_id=user_id, competencia=competencia)
    session.add(run)
    session.commit()
    try:
        response = (client or PetrobrasClient(config)).pagamentos(competencia)
        numbers = payment_protocols(response, config)
        existing = set(
            session.scalars(
                select(PetrobrasProtocolo.numero).where(
                    PetrobrasProtocolo.prestador == config.prestador_cnpj
                )
            )
        )
        for number in numbers:
            if number not in existing:
                session.add(
                    PetrobrasProtocolo(
                        prestador=config.prestador_cnpj, numero=number
                    )
                )
        run.situacao, run.quantidade, run.fim = (
            'concluida',
            len(numbers),
            utcnow(),
        )
        session.commit()
        return {
            'protocolos_localizados': len(numbers),
            'competencia': competencia,
            'orientacao': (
                'Protocolos disponíveis para sincronização dos itens.'
            ),
        }
    except Exception as exc:
        session.rollback()
        run.situacao, run.fim = 'falha', utcnow()
        run.erro = (
            str(exc)[:200]
            if isinstance(exc, TissError)
            else (
                'Falha ao consultar competência; dados anteriores preservados.'
            )
        )
        session.commit()
        raise TissError(run.erro) from None
