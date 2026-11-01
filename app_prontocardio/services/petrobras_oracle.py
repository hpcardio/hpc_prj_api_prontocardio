"""Oracle access restricted to SELECTs in an explicit read-only transaction."""

from contextlib import contextmanager

from sqlalchemy import text

from app_prontocardio.database import oracle_engine
from app_prontocardio.services.petrobras_match import (
    GUIDE_FIELD,
    provider_guide,
)


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
# MV keeps NR_GUIA without the TISS left zero padding (B386348).
GUIDE_COLUMN = "NVL(LTRIM(TRIM(nr_guia), '0'), '0')"


def _date(value):
    return value.date().isoformat() if value else None


def _text(value):
    return str(value) if value is not None else None


def candidates(sources, config):
    if config.oracle_guia != GUIDE_FIELD:
        return []
    guias = sorted({
        provider_guide(s.get(GUIDE_FIELD))
        for s in sources
        if provider_guide(s.get(GUIDE_FIELD))
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
            # Full item identity preserves repeats; display fields fill the
            # standard Follow-Up cards when the composite key is confirmed.
            rows = conn.execute(
                text(f"""
                SELECT cd_convenio, nm_convenio, cd_reg, cd_lancamento,
                       cd_atendimento, cd_paciente, nm_paciente, cd_remessa,
                       tp_atendimento, nr_guia, cd_pro_fat, cd_tuss,
                       nr_carteira, cd_gru_fat, ds_gru_fat, dt_atendimento,
                       dt_alta, dt_lancamento, qt_lancamento, vl_total_conta,
                       descricao
                  FROM dbamv.hpc_v_conta_atendimento
                 WHERE cd_convenio=:convenio
                   AND {GUIDE_COLUMN} IN ({placeholders})
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
                    'nm_convenio': r['nm_convenio'],
                    'cd_reg': r['cd_reg'],
                    'cd_lancamento': r['cd_lancamento'],
                    'cd_atendimento': r['cd_atendimento'],
                    'cd_paciente': r['cd_paciente'],
                    'nm_paciente': r['nm_paciente'],
                    'cd_remessa': r['cd_remessa'],
                    'tp_atendimento': r['tp_atendimento'],
                    'nr_guia': r['nr_guia'],
                    'cd_pro_fat': r['cd_pro_fat'],
                    'cd_tuss': r['cd_tuss'],
                    'nr_carteira': r['nr_carteira'],
                    'cd_gru_fat': r['cd_gru_fat'],
                    'ds_gru_fat': r['ds_gru_fat'],
                    'dt_atendimento': _date(r['dt_atendimento']),
                    'dt_alta': _date(r['dt_alta']),
                    'data_realizacao': _date(r['dt_lancamento']),
                    'quantidade': _text(r['qt_lancamento']),
                    'valor': _text(r['vl_total_conta']),
                    'descricao': r['descricao'],
                })
    return result
