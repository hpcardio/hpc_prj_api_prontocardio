"""Why a Petrobras guide is not linked to the MV and which key unlocks it.

The manual association only stores the MV key of the guide (senha and/or
carteira); the item link itself is always the automatic reconciliation.
"""

from collections import Counter
from decimal import Decimal

from app_prontocardio.services.petrobras_match import (
    GUIDE_FIELD,
    GUIDE_ONLY_SCOPE,
    norm,
    provider_guide,
)

GAPS = {
    'senha_ausente': (
        'senha',
        'A Petrobras não informou a senha da guia.',
    ),
    'senha_nao_localizada': (
        'senha',
        'Senha não encontrada no MV junto com o procedimento da guia.',
    ),
    'senha_em_varios_atendimentos': (
        'senha',
        'Senha encontrada em mais de um atendimento do MV.',
    ),
    'carteira_ausente': (
        'carteira',
        'A Petrobras não informou a carteira do beneficiário.',
    ),
    'carteira_divergente': (
        'carteira',
        'Carteira diferente da cadastrada no atendimento do MV.',
    ),
    'procedimento_ausente': (
        None,
        'A Petrobras não informou o procedimento do item.',
    ),
    'multiplos_lancamentos': (
        None,
        'Mais de um lançamento do MV para o mesmo procedimento.',
    ),
}
FIELD_ORDER = ('senha', 'carteira')


def item_gaps(cruzamento, divergencias, source):
    """Gap codes of one unlinked item, from its effective matching source."""
    if cruzamento == 'conciliado':
        return []
    guide_only = source.get('escopo') == GUIDE_ONLY_SCOPE
    gaps = []
    if not provider_guide(source.get(GUIDE_FIELD)):
        gaps.append('senha_ausente')
    if not guide_only:
        if not norm(source.get('carteira')):
            gaps.append('carteira_ausente')
        if not norm(source.get('tuss')):
            gaps.append('procedimento_ausente')
    if gaps or cruzamento not in {'nao_localizado', 'ambiguo'}:
        return gaps
    if cruzamento == 'nao_localizado':
        code = 'senha_nao_localizada'
    elif guide_only:
        code = 'senha_em_varios_atendimentos'
    elif any('carteira' in str(d) for d in divergencias or []):
        code = 'carteira_divergente'
    else:
        code = 'multiplos_lancamentos'
    return [code]


def guide_summary(rows, sources, manual):
    """One pending guide: payer keys, gaps and only the fields to fill."""
    first = rows[0]
    fonte = first.fonte or {}
    gaps = Counter()
    for row in rows:
        gaps.update(
            item_gaps(row.cruzamento, row.divergencias, sources[row.id])
        )
    fields = {GAPS[code][0] for code in gaps} - {None}
    remessas = {
        (row.oracle or {}).get('cd_remessa')
        for row in rows
        if (row.oracle or {}).get('cd_remessa')
    }
    return {
        'protocolo_id': first.protocolo_id,
        'protocolo': fonte.get('protocolo'),
        'lote': fonte.get('lote'),
        'lote_prestador': fonte.get('lote_prestador'),
        'guia_prestador': first.guia_prestador,
        'guia_operadora': first.guia_operadora,
        'senha_petrobras': next(
            (
                (r.fonte or {}).get(GUIDE_FIELD)
                for r in rows
                if (r.fonte or {}).get(GUIDE_FIELD)
            ),
            None,
        ),
        'carteira_petrobras': next(
            (
                (r.fonte or {}).get('carteira')
                for r in rows
                if (r.fonte or {}).get('carteira')
            ),
            None,
        ),
        'chave_manual': manual or None,
        'itens': len(rows),
        'valor_glosa': sum(
            (Decimal(str(r.valor_glosa or 0)) for r in rows), Decimal(0)
        ),
        'situacoes': dict(Counter(r.cruzamento for r in rows)),
        'cd_remessa': remessas.pop() if len(remessas) == 1 else None,
        'pendencias': [
            {
                'codigo': code,
                'campo': GAPS[code][0],
                'descricao': GAPS[code][1],
                'itens': count,
            }
            for code, count in sorted(gaps.items())
        ],
        'campos': [f for f in FIELD_ORDER if f in fields],
    }
