"""Match Maida glosa lines using only guide and procedure within a remittance."""

from collections import defaultdict


def key(value):
    return str(value or '').strip()


def code(value):
    value = key(value)
    return (value.lstrip('0') or '0') if value.isdigit() else value


def load_brasindice(session):
    """Read the unique official catalog loaded by the materialization DAG."""
    from sqlalchemy import text

    table = 'api_prontocardio_staging.ipm_brasindice_oracle'
    if not session.execute(text('SELECT to_regclass(:table)'),
                           {'table': table}).scalar():
        return {}
    rows = session.execute(text(
        "SELECT cd_tiss, MIN(cd_pro_fat) AS cd_pro_fat "
        "FROM api_prontocardio_staging.ipm_brasindice_oracle "
        "GROUP BY cd_tiss HAVING COUNT(DISTINCT cd_pro_fat) = 1"
    )).mappings()
    return {code(r['cd_tiss']): code(r['cd_pro_fat']) for r in rows}


def match_maida_items(demonstrativos, oracle_items, brasindice=None):
    index = defaultdict(dict)
    catalog_index = defaultdict(dict)
    brasindice = brasindice or {}
    for item in oracle_items:
        if key(item.get('nm_convenio')).upper() != 'ISSEC':
            continue
        guide = key(item.get('nr_guia'))
        identity = (item['cd_remessa'], item['cd_reg'], item['cd_lancamento'])
        for procedure in {
            code(item.get('cd_pro_fat')),
            code(item.get('cd_tuss')),
        }:
            if guide and procedure:
                index[guide, procedure][identity] = item
        procedure = code(item.get('cd_pro_fat'))
        if guide and procedure:
            catalog_index[guide, procedure][identity] = item
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
                code(demo.get('codigo_servico')),
            ),
            {},
        )
        candidates = dict(candidates)
        mapped = brasindice.get(code(demo.get('codigo_servico')))
        if mapped:
            candidates.update(catalog_index.get(
                (key(demo.get('numero_guia_senha')), code(mapped)), {},
            ))
        if len(candidates) == 1:
            seen.add(identity)
            yield demo, next(iter(candidates.values()))
