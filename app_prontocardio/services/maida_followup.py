"""Match Maida glosa lines using only guide and procedure within a remittance."""

from collections import defaultdict


def key(value):
    return str(value or '').strip()


def match_maida_items(demonstrativos, oracle_items):
    index = defaultdict(dict)
    for item in oracle_items:
        if key(item.get('nm_convenio')).upper() != 'ISSEC':
            continue
        guide = key(item.get('nr_guia'))
        identity = (item['cd_remessa'], item['cd_reg'], item['cd_lancamento'])
        for procedure in {
            key(item.get('cd_pro_fat')),
            key(item.get('cd_tuss')),
        }:
            if guide and procedure:
                index[guide, procedure][identity] = item
    seen = set()
    for demo in demonstrativos:
        identity = key(demo.get('id_registro'))
        if not identity or identity in seen:
            continue
        if not demo.get('origem_maida') or (demo.get('valor_glosa') or 0) <= 0:
            continue
        candidates = index.get(
            (
                key(demo.get('numero_guia_senha')),
                key(demo.get('codigo_servico')),
            ),
            {},
        )
        if len(candidates) == 1:
            seen.add(identity)
            yield demo, next(iter(candidates.values()))
