"""Read-only ISSEC portal fallback; amounts are never inferred from MV."""

from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import text


def month(value):
    if isinstance(value, (date, datetime)):
        return date(value.year, value.month, 1)
    try:
        return datetime.strptime(str(value).strip(), '%m/%Y').date()
    except (ValueError, TypeError):
        return None


def money(value):
    return Decimal(str(value or 0)).quantize(Decimal('0.01'))


def portal_months(row):
    return {
        value
        for value in (
            month(row.get('competencia')),
            month(row.get('mes_producao')),
        )
        if value is not None
    }


def match_rows(portal, oracle):
    """Match either portal month, requiring uniqueness across both alternatives."""
    portal = list(portal)
    remittances = defaultdict(set)
    for row in oracle:
        competence = month(row['competencia'])
        if competence:
            remittances[competence, money(row['total'])].add(
                int(row['cd_remessa'])
            )
    by_process = defaultdict(set)
    by_remittance = defaultdict(set)
    for row in portal:
        number = row['processo']
        for competence in portal_months(row):
            for remittance in remittances[
                competence, money(row['valor_cobrado'])
            ]:
                by_process[number].add(remittance)
                by_remittance[remittance].add(number)
    seen = set()
    for row in portal:
        number = row['processo']
        candidates = by_process[number]
        if number not in seen and len(candidates) == 1:
            remittance = next(iter(candidates))
            if len(by_remittance[remittance]) == 1:
                seen.add(number)
                yield row, remittance


def portal_cards(
    session,
    oracle,
    existing,
    *,
    cd_remessa=None,
    processo=None,
    protocolo=None,
    convenio=None,
    q=None,
    unsupported=False,
    processos_permitidos=None,
    remessas_permitidas=None,
):
    if unsupported or (
        convenio and convenio.strip().casefold() not in 'issec'
    ):
        return []
    rows = [
        dict(row)
        for row in session.execute(
            text(
                """SELECT BTRIM(processo) AS processo, competencia, mes_producao,
                  valor_cobrado, valor_total_itens_glosados,
                  EXISTS (SELECT 1 FROM api_prontocardio.registros_glosa r
                           WHERE r.sn_ativo = 'true'
                             AND BTRIM(r.processo_controle_fatura_gab) = BTRIM(p.processo)) AS tratado
             FROM raw_issec_portal.processos p
            WHERE NULLIF(BTRIM(processo), '') IS NOT NULL"""
            )
        ).mappings()
    ]
    # Preserve ambiguity detection even if only one of two equal processes
    # matches the user's search. Never filter candidates before matching.
    months = {value for row in rows for value in portal_months(row)}
    months.discard(None)
    if not months:
        return []
    last = max(months)
    end = date(last.year + (last.month == 12), last.month % 12 + 1, 1)
    totals = list(
        oracle.execute(
            text(
                """SELECT cd_remessa, competencia, SUM(valor) AS total
             FROM (
                 SELECT cd_remessa, TRUNC(dt_competencia, 'MM') competencia,
                        cd_reg, MIN(vl_total_registro) valor,
                        CASE WHEN MIN(vl_total_registro) = MAX(vl_total_registro)
                             THEN 0 ELSE 1 END ambiguo
                   FROM DBAMV.HPC_V_CONTA_ATENDIMENTO
                  WHERE UPPER(TRIM(nm_convenio)) = 'ISSEC'
                    AND dt_competencia >= :inicio AND dt_competencia < :fim
                    AND cd_remessa IS NOT NULL
                  GROUP BY cd_remessa, TRUNC(dt_competencia, 'MM'), cd_reg
             ) GROUP BY cd_remessa, competencia
             HAVING SUM(ambiguo) = 0"""
            ),
            {'inicio': min(months), 'fim': end},
        ).mappings()
    )
    cards = []
    seen = set(existing)
    for row, remittance in match_rows(rows, totals):
        number = row['processo']
        if (
            row.get('tratado')
            or number in seen
            or money(row['valor_total_itens_glosados']) <= 0
        ):
            continue
        if cd_remessa is not None and cd_remessa != remittance:
            continue
        if any(
            term and str(term).strip().casefold() not in number.casefold()
            for term in (processo, protocolo)
        ):
            continue
        if q and q.strip().casefold() not in f'{number} {remittance} issec':
            continue
        if (
            processos_permitidos is not None
            and number.casefold() not in processos_permitidos
        ):
            continue
        if (
            remessas_permitidas is not None
            and remittance not in remessas_permitidas
        ):
            continue
        seen.add(number)
        glosa = money(row['valor_total_itens_glosados'])
        cards.append({
            'conciliacao_remessa_id': None,
            'cd_remessa': remittance,
            'numero_protocolo': number,
            'convenio': 'ISSEC',
            'data_competencia': month(row['competencia']),
            'data_entrega': None,
            'numero_nfse': '',
            'valor_remessa': money(row['valor_cobrado']),
            'valor_itens': money(row['valor_cobrado']),
            'valor_glosado': glosa,
            'valor_glosa_pendente': glosa,
            'valor_total_tratado': Decimal('0.00'),
            'possui_recurso': False,
            'processo': {
                'numero_processo': number,
                'data_abertura': None,
                'status_processo': None,
                'motivo_finalizacao': None,
            },
            'recebimentos': [],
            'pacientes': [],
            'fiscal': {
                'numero_nfse': '',
                'valor_servicos': Decimal('0.00'),
                'impostos': Decimal('0.00'),
                'valor_liquido_nfse': Decimal('0.00'),
                'data_emissao': None,
            },
        })
    return cards
