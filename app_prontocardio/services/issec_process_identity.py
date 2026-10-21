"""Distinguish imported Maida lot placeholders from real ISSEC processes."""

from sqlalchemy import text


def clear_lot_processes(session, cards):
    candidates = [
        card
        for card in cards
        if str(card.get('convenio') or '').strip().upper() == 'ISSEC'
        and card.get('cd_remessa') is not None
        and str(
            (card.get('processo') or {}).get('numero_processo') or ''
        ).strip()
    ]
    if not candidates:
        return
    from app_prontocardio.routers.financeiro import (  # noqa: PLC0415
        _tabela_ipm_existe,
        _tabela_schema_existe,
    )

    if not all(
        _tabela_ipm_existe(session, name)
        for name in (
            'registros_glosa_demonstrativo_ipm',
            'demonstrativo_conta_ipm',
        )
    ) or not _tabela_schema_existe(session, 'raw_issec_portal', 'processos'):
        return
    rows = session.execute(
        text("""
        SELECT DISTINCT g.cd_remessa, BTRIM(d.numero_lote) AS lote
          FROM api_prontocardio.registros_glosa g
          JOIN api_prontocardio.registros_glosa_demonstrativo_ipm t
            ON t.registro_glosa_id = g.id
          JOIN api_prontocardio.demonstrativo_conta_ipm d
            ON d.id_registro = t.id_registro
         WHERE g.sn_ativo = 'true'
           AND UPPER(BTRIM(g.convenio)) = 'ISSEC'
           AND g.cd_remessa = ANY(:remessas)
           AND d.origem_maida IS NOT NULL
           AND NULLIF(BTRIM(d.numero_lote), '') IS NOT NULL
           AND NOT EXISTS (
               SELECT 1 FROM raw_issec_portal.processos p
                WHERE BTRIM(p.processo) = BTRIM(d.numero_lote)
           )
    """),
        {'remessas': sorted({int(c['cd_remessa']) for c in candidates})},
    ).mappings()
    lots = {(int(row['cd_remessa']), str(row['lote'])) for row in rows}
    for card in candidates:
        process = card['processo']
        if (
            int(card['cd_remessa']),
            str(process['numero_processo']).strip(),
        ) in lots:
            card['processo'] = {**process, 'numero_processo': ''}
