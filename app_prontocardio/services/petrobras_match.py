"""Conservative, exact identifiers. No fuzzy names or first-row fallback.

Confirmed key: numeroCarteira = NR_CARTEIRA, senha = NR_GUIA (compared
without TISS left zero padding) and codigoProcedimento = CD_PRO_FAT.
numeroGuiaPrestador and numeroGuiaOperadora are never part of the key.
"""

from decimal import Decimal

GUIDE_FIELD = 'senha'


def norm(value):
    return str(value).strip() if value is not None else ''


def provider_guide(value):
    """Remove only the left zero padding sent by the TISS field."""
    value = norm(value)
    return (value.lstrip('0') or '0') if value else ''


def _result(status, oracle=None, count=0, divergencias=None):
    return {
        'status': status,
        'oracle': oracle,
        'count': count,
        'divergencias': divergencias or [],
    }


def _refine(rows, source):
    # TISS sequencialItem != MV cd_lancamento. Never equate them without a
    # map; only exact date and quantity may narrow repeated procedures.
    for field in ('data_realizacao', 'quantidade'):
        value = source.get(field)
        if value is None or not norm(value):
            continue
        refined = [
            r
            for r in rows
            if r.get(field) is not None
            and (
                Decimal(str(r[field])) == Decimal(str(value))
                if field == 'quantidade'
                else norm(r.get(field)) == norm(value)
            )
        ]
        if refined:
            rows = refined
    return rows


def reconcile(source, candidates, convenio_id):  # noqa: PLR0911 - explicit conservative matching decisions
    guia = provider_guide(source.get(GUIDE_FIELD))
    procedure = norm(source.get('tuss'))
    wallet = norm(source.get('carteira'))
    if (
        not guia
        or not procedure
        or not wallet
        or source.get('escopo') != 'item'
    ):
        return _result(
            'dados_insuficientes',
            divergencias=['Vínculo exige carteira, senha e procedimento.'],
        )
    same_guide = [
        r
        for r in candidates
        if r.get('cd_convenio') == convenio_id
        and provider_guide(r.get('nr_guia')) == guia
        and norm(r.get('cd_pro_fat')) == procedure
    ]
    rows = [r for r in same_guide if norm(r.get('nr_carteira')) == wallet]
    if same_guide and not rows:
        return _result(
            'ambiguo',
            count=len(same_guide),
            divergencias=[
                'Senha e procedimento encontrados, mas a carteira está '
                'ausente ou é divergente. Vínculo não confirmado.'
            ],
        )
    # Identical join duplicates are not multiple items; different mappings
    # remain ambiguous.
    rows = list(
        {
            tuple(sorted((k, norm(v)) for k, v in r.items())): r for r in rows
        }.values()
    )
    if len(rows) > 1:
        rows = _refine(rows, source)
    if len(rows) != 1:
        return _result(
            'ambiguo' if rows else 'nao_localizado', count=len(rows)
        )
    row = rows[0]
    diffs = []
    if source.get('senha_da_guia'):
        diffs.append(
            'Senha obtida da mesma guia em outro demonstrativo do protocolo.'
        )
    if norm(source.get(GUIDE_FIELD)) != norm(row.get('nr_guia')):
        diffs.append('Senha conferida sem zeros à esquerda.')
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
    return _result('conciliado', row, 1, diffs)
