"""Exact guide/ISSEC lot filters, resolved before pagination."""

from collections import defaultdict
from decimal import Decimal

from sqlalchemy import text


def registros_no_escopo(session, guia='', lote=''):
    return list(
        session.execute(
            text("""
        SELECT DISTINCT g.id, g.cd_remessa, g.conta, g.cd_lancamento,
                        g.cd_atendimento, g.guia
          FROM api_prontocardio.registros_glosa g
          LEFT JOIN api_prontocardio.registros_glosa_demonstrativo_ipm t
            ON t.registro_glosa_id = g.id
          LEFT JOIN api_prontocardio.demonstrativo_conta_ipm d
            ON d.id_registro = t.id_registro
         WHERE g.sn_ativo = 'true'
           AND (:guia = '' OR TRIM(g.guia) = :guia
                            OR TRIM(d.numero_guia_senha) = :guia)
           AND (:lote = '' OR (
               UPPER(TRIM(g.convenio)) = 'ISSEC'
               AND (TRIM(g.numero_lote) = :lote
                    OR (d.origem_maida IS NOT NULL
                        AND TRIM(d.numero_lote) = :lote))
           ))
    """),
            {'guia': guia, 'lote': lote},
        ).mappings()
    )


def filtrar_cards(cards, escopo):
    identities = {
        (r['cd_remessa'], r['cd_atendimento'], r['conta'], r['cd_lancamento'])
        for r in escopo
    }
    ids = {r['id'] for r in escopo}
    result = []
    for card in cards:
        patients = []
        for patient in card.get('pacientes') or []:
            items = []
            for item in patient.get('itens') or []:
                identity = tuple(
                    item.get(k)
                    for k in (
                        'cd_remessa',
                        'cd_atendimento',
                        'cd_reg',
                        'cd_lancamento',
                    )
                )
                records = [
                    item.get(k)
                    for k in (
                        'registro_glosa',
                        'registro_recusa',
                        'registro_acato',
                    )
                ]
                linked = [r for r in records if r is not None]
                matches = any(
                    (
                        r.get('id')
                        if isinstance(r, dict)
                        else getattr(r, 'id', None)
                    )
                    in ids
                    for r in linked
                )
                if matches or (not linked and identity in identities):
                    items.append(item)
            if items:
                p = {**patient, 'itens': items}
                for dest, source in [
                    ('valor_itens', 'vl_total_conta'),
                    ('valor_glosado', 'valor_glosa'),
                    ('valor_total_tratado', 'valor_total_tratado'),
                ]:
                    p[dest] = sum(
                        (Decimal(str(i.get(source) or 0)) for i in items),
                        Decimal(0),
                    )
                patients.append(p)
        if patients:
            c = {**card, 'pacientes': patients}
            for key in ('valor_itens', 'valor_glosado', 'valor_total_tratado'):
                c[key] = sum((p[key] for p in patients), Decimal(0))
            c['valor_glosa_pendente'] = max(
                c['valor_glosado'] - c['valor_total_tratado'], Decimal(0)
            )
            result.append(c)
    return result


def consultar_filtrado(session, consultar, parametros, guia, lote):
    scope = registros_no_escopo(session, guia, lote)
    remessas = sorted({r['cd_remessa'] for r in scope})
    if parametros.get('cd_remessa') is not None:
        remessas = [r for r in remessas if r == parametros['cd_remessa']]
    cards = []
    for remessa in remessas:
        cursor = 0
        while True:
            response = consultar(**{
                **parametros,
                'cd_remessa': remessa,
                'incluir_detalhes': True,
                'limit': 100,
                'offset': cursor,
            })
            cards.extend(response['cards'])
            cursor += 100
            if cursor >= response['total']:
                break
    cards = filtrar_cards(cards, scope)
    groups = defaultdict(list)
    cards.sort(
        key=lambda c: str(c.get('data_competencia') or ''), reverse=True
    )
    for c in cards:
        process = (
            str((c.get('processo') or {}).get('numero_processo') or '')
            .strip()
            .casefold()
        )
        key = (
            ('processo', process)
            if process and parametros['agrupar_por_processo']
            else ('remessa', c.get('cd_remessa'), process)
        )
        groups[key].append(c)
    items = [i for c in cards for p in c['pacientes'] for i in p['itens']]

    def total(field):
        return sum(
            (Decimal(str(i.get(field) or 0)) for i in items), Decimal(0)
        )

    def treated(field):
        records = {}
        for item in items:
            r = item.get(field)
            if r is not None:
                ident = r.get('id') if isinstance(r, dict) else r.id
                value = (
                    r.get('valor_recursado')
                    if isinstance(r, dict)
                    else r.valor_recursado
                )
                records[ident] = Decimal(str(value or 0))
        return sum(records.values(), Decimal(0))

    offset, limit = parametros['offset'], parametros['limit']
    return dict(
        cards=[
            c
            for key in list(groups)[offset : offset + limit]
            for c in groups[key]
        ],
        total=len(groups),
        quantidade_glosas=len(items),
        valor_total_glosado=total('valor_glosa'),
        valor_total_tratado=total('valor_total_tratado'),
        valor_total_pendente=sum(
            (
                max(
                    Decimal(str(i.get('valor_glosa') or 0))
                    - Decimal(str(i.get('valor_total_tratado') or 0)),
                    Decimal(0),
                )
                for i in items
            ),
            Decimal(0),
        ),
        valor_total_recursado=treated('registro_recusa'),
        valor_total_acatado=treated('registro_acato'),
        limit=limit,
        offset=offset,
    )
