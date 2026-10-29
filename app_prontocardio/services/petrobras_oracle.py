"""Oracle access restricted to SELECTs in an explicit read-only transaction."""

from contextlib import contextmanager

from sqlalchemy import text

from app_prontocardio.database import oracle_engine


@contextmanager
def connection():
    with oracle_engine.connect() as conn:
        # The general engine has MV view access. Enforce a read-only
        # transaction even when its account also has write grants.
        conn.exec_driver_sql('SET TRANSACTION READ ONLY')
        raw = conn.connection.driver_connection
        previous = raw.call_timeout
        raw.call_timeout = 20000
        try:
            yield conn
        finally:
            raw.call_timeout = previous
            conn.rollback()


MAX_CANDIDATES = 20000


def candidates(sources, config):
    if config.oracle_guia not in {'guia_prestador', 'guia_operadora'}:
        return []
    guias = sorted({
        s[config.oracle_guia].strip()
        for s in sources
        if s.get(config.oracle_guia)
    })
    result = []
    if not guias:
        return result
    with connection() as conn:
        for start in range(0, len(guias), 100):
            batch = guias[start : start + 100]
            binds = {f'g{i}': v for i, v in enumerate(batch)}
            placeholders = ','.join(':' + k for k in binds)
            binds['convenio'] = config.convenio_id
            # No patient name is needed. Include full item identity to preserve
            # repeats.
            rows = conn.execute(
                text(f"""
                SELECT cd_convenio, cd_reg, cd_lancamento, cd_atendimento,
                       cd_remessa, tp_atendimento, nr_guia,
                       cd_tuss, nr_carteira,
                       dt_lancamento, qt_lancamento, vl_total_conta, descricao
                  FROM dbamv.hpc_v_conta_atendimento
                 WHERE cd_convenio=:convenio
                   AND TRIM(nr_guia) IN ({placeholders})
            """),
                binds,
            ).mappings()
            for r in rows:
                if len(result) >= MAX_CANDIDATES:
                    raise ValueError(
                        (
                            'Consulta Oracle excedeu o limite '
                            'de itens; refine os protocolos.'
                        )
                    )
                result.append({
                    'cd_convenio': r['cd_convenio'],
                    'cd_reg': r['cd_reg'],
                    'cd_lancamento': r['cd_lancamento'],
                    'cd_atendimento': r['cd_atendimento'],
                    'cd_remessa': r['cd_remessa'],
                    'tp_atendimento': r['tp_atendimento'],
                    'nr_guia': r['nr_guia'],
                    'cd_tuss': r['cd_tuss'],
                    'nr_carteira': r['nr_carteira'],
                    'data_realizacao': r['dt_lancamento'].date().isoformat()
                    if r['dt_lancamento']
                    else None,
                    'quantidade': str(r['qt_lancamento'])
                    if r['qt_lancamento'] is not None
                    else None,
                    'valor': str(r['vl_total_conta'])
                    if r['vl_total_conta'] is not None
                    else None,
                    'descricao': r['descricao'],
                })
    return result
