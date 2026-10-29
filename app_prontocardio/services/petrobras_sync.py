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


def synchronize(  # noqa: PLR0913 - explicit source and audit context
    session, config, protocol_id, oracle_loader, user_id=None, client=None
):
    return synchronize_many(
        session, config, [protocol_id], oracle_loader, user_id, client
    )


def synchronize_many(  # noqa: PLR0913 - injected sources and audit context
    session,
    config,
    protocol_ids,
    oracle_loader,
    user_id=None,
    client=None,
    competencia=None,
):
    """Import explicit returned details; missing protocols stay pending."""
    config.require_connection()
    run = PetrobrasExecucao(usuario_id=user_id, competencia=competencia)
    session.add(run)
    session.commit()
    run_id = run.id
    try:
        protocols = list(
            session.scalars(
                select(PetrobrasProtocolo)
                .where(
                    PetrobrasProtocolo.id.in_(protocol_ids),
                    PetrobrasProtocolo.prestador == config.prestador_cnpj,
                )
                .order_by(PetrobrasProtocolo.id)
                .with_for_update(nowait=True)
            )
        )
        if not protocols or len(protocols) != len(set(protocol_ids)):
            raise TissError('Protocolo Petrobras não encontrado.')
        requested = {p.numero for p in protocols}
        transport = client or PetrobrasClient(config)
        response = transport.demonstrativos(sorted(requested))
        rows, returned_scopes = parse_demonstrativos(
            response, config.prestador_cnpj, config.registro_ans
        )
        returned = {number for number, _ in returned_scopes}
        if returned - requested:
            raise TissError(
                'Resposta contém protocolo diferente do solicitado.'
            )
        warnings = ['Retorno parcial: cobertura das guias não confirmada.']
        transport_warnings = getattr(transport, 'response_warnings', [])
        if isinstance(transport_warnings, list):
            warnings.extend(transport_warnings)
        if requested - returned:
            warnings.append(
                f'{len(requested - returned)} protocolos '
                'sem detalhes no retorno.'
            )
        if any(r.get('divergencia_totais') for r in rows):
            warnings.append('Totais divergentes preservados para revisão.')
        oracle_error = None
        try:
            oracle_rows = oracle_loader(rows, config)
        except (SQLAlchemyError, ValueError):
            oracle_rows = []
            oracle_error = 'Oracle indisponível; último cruzamento preservado.'
            warnings.append(oracle_error)
        for protocol in protocols:
            protocol.proxima_sincronizacao = utcnow() + timedelta(
                minutes=config.interval_minutes
            )
            if protocol.numero not in returned:
                protocol.erro = 'Protocolo sem detalhes no retorno parcial.'
                continue
            run.quantidade += import_rows(
                session,
                protocol,
                rows,
                set(),
                oracle_rows,
                config,
                user_id,
                oracle_error,
            )
            # A returned subset is never authoritative evidence of deletion.
            protocol.ultima_sincronizacao = utcnow()
            protocol.erro = oracle_error or warnings[0]
        run.situacao, run.erro = 'parcial', '; '.join(warnings)[:200]
        run.fim = utcnow()
        session.commit()
        return {
            'execucao_id': run_id,
            'situacao': run.situacao,
            'quantidade': run.quantidade,
            'avisos': warnings,
            'protocolos_retornados': len(returned),
            'protocolos_pendentes': len(requested - returned),
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
        session.commit()
        raise TissError(safe) from None


def synchronize_competencia(  # noqa: PLR0913 - injected sources and audit context
    session, config, competencia, oracle_loader, user_id=None, client=None
):
    transport = client or PetrobrasClient(config)
    result = discover_competencia(
        session, config, competencia, user_id, transport
    )
    numbers = result.pop('numeros_protocolos')
    if not numbers:
        return {
            **result,
            'situacao': 'parcial',
            'quantidade': 0,
            'avisos': result['avisos'] + ['Nenhum protocolo consultável.'],
        }
    ids = list(
        session.scalars(
            select(PetrobrasProtocolo.id).where(
                PetrobrasProtocolo.prestador == config.prestador_cnpj,
                PetrobrasProtocolo.numero.in_(numbers),
            )
        )
    )
    count, returned, pending = 0, 0, 0
    warnings = result['avisos'][:]
    for start in range(0, len(ids), 30):
        try:
            batch = synchronize_many(
                session,
                config,
                ids[start : start + 30],
                oracle_loader,
                user_id,
                transport,
                competencia,
            )
            count += batch['quantidade']
            returned += batch['protocolos_retornados']
            pending += batch['protocolos_pendentes']
            warnings.extend(batch['avisos'])
        except TissError as exc:
            pending += len(ids[start : start + 30])
            warnings.append(str(exc))
    return {
        **result,
        'situacao': 'parcial',
        'quantidade': count,
        'protocolos_retornados': returned,
        'protocolos_pendentes': pending,
        'avisos': list(dict.fromkeys(warnings)),
    }


def discover_competencia(
    session, config, competencia, user_id=None, client=None
):
    """Discover payer protocols at Petrobras, never from Oracle."""
    config.require_connection()
    run = PetrobrasExecucao(usuario_id=user_id, competencia=competencia)
    session.add(run)
    session.commit()
    try:
        transport = client or PetrobrasClient(config)
        response = transport.pagamentos(competencia)
        numbers = payment_protocols(response, config)
        warnings = getattr(transport, 'response_warnings', [])
        if not isinstance(warnings, list):
            warnings = []
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
        run.erro = '; '.join(warnings)[:200] or None
        run.situacao, run.quantidade, run.fim = (
            'concluida',
            len(numbers),
            utcnow(),
        )
        session.commit()
        return {
            'protocolos_localizados': len(numbers),
            'numeros_protocolos': numbers,
            'avisos': warnings,
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
