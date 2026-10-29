"""Conservative, exact identifiers. No fuzzy names or first-row fallback."""

from decimal import Decimal


def norm(value):
    return str(value).strip() if value is not None else ''


def reconcile(source, candidates, convenio_id, guia_field):  # noqa: PLR0912 - explicit conservative matching decisions
    if guia_field not in {'guia_prestador', 'guia_operadora'}:
        return {
            'status': 'dados_insuficientes',
            'oracle': None,
            'count': 0,
            'divergencias': ['Mapeamento da guia Oracle pendente.'],
        }
    guia, tuss = norm(source.get(guia_field)), norm(source.get('tuss'))
    if not guia or not tuss or source.get('escopo') != 'item':
        return {
            'status': 'dados_insuficientes',
            'oracle': None,
            'count': 0,
            'divergencias': [],
        }
    rows = [
        r
        for r in candidates
        if r.get('cd_convenio') == convenio_id
        and norm(r.get('nr_guia')) == guia
        and norm(r.get('cd_tuss')) == tuss
    ]
    # Identical join duplicates are not multiple items; different mappings
    # remain ambiguous.
    unique = {
        tuple(sorted((k, norm(v)) for k, v in r.items())): r for r in rows
    }
    rows = list(unique.values())
    wallet = norm(source.get('carteira'))
    wallet_notes = []
    if wallet and rows:
        compatible = [
            r
            for r in rows
            if not norm(r.get('nr_carteira'))
            or norm(r.get('nr_carteira')) == wallet
        ]
        if not compatible:
            return {
                'status': 'ambiguo',
                'oracle': None,
                'count': len(rows),
                'divergencias': [
                    (
                        'Guia e TUSS encontrados, mas a carteira '
                        'é divergente. Vínculo não confirmado.'
                    )
                ],
            }
        rows = compatible
    elif not wallet:
        wallet_notes.append(
            (
                'Carteira não informada pela Petrobras; '
                'conferência adicional indisponível.'
            )
        )
    if len(rows) > 1:
        # TISS sequencialItem != MV cd_lancamento. Never equate them without a
        # map.
        for sf, of in [
            ('data_realizacao', 'data_realizacao'),
            ('quantidade', 'quantidade'),
        ]:
            value = source.get(sf)
            if value is None or not norm(value):
                continue
            refined = [
                r
                for r in rows
                if r.get(of) is not None
                and (
                    Decimal(str(r[of])) == Decimal(str(value))
                    if sf == 'quantidade'
                    else norm(r.get(of)) == norm(value)
                )
            ]
            if refined:
                rows = refined
    if len(rows) != 1:
        return {
            'status': 'ambiguo' if rows else 'nao_localizado',
            'oracle': None,
            'count': len(rows),
            'divergencias': [],
        }
    row = rows[0]
    diffs = wallet_notes
    if wallet and not norm(row.get('nr_carteira')):
        diffs.append(
            (
                'Carteira não informada no Oracle; '
                'conferência adicional indisponível.'
            )
        )
    for sf, of in [('valor_informado', 'valor'), ('quantidade', 'quantidade')]:
        if (
            source.get(sf) is not None
            and row.get(of) is not None
            and Decimal(str(source[sf])) != Decimal(str(row[of]))
        ):
            diffs.append(f'{sf}: Petrobras={source[sf]}; Oracle={row[of]}')
    if (
        source.get('data_realizacao')
        and row.get('data_realizacao')
        and source['data_realizacao'] != row['data_realizacao']
    ):
        diffs.append('Data de realização divergente.')
    return {
        'status': 'conciliado',
        'oracle': row,
        'count': 1,
        'divergencias': diffs,
    }
