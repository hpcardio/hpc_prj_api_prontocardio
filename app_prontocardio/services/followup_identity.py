"""Read-only recovery of missing ISSEC patient identity from Oracle."""

from collections import defaultdict
from decimal import Decimal


def missing_identity(item):
    return (
        not item.get('cd_atendimento')
        or not item.get('cd_paciente')
        or str(item.get('nm_paciente') or '').startswith('Guia ')
    )


def enrich_cards(cards, by_remittance):
    for card in cards:
        if str(card.get('convenio') or '').strip().upper() != 'ISSEC':
            continue
        index = defaultdict(dict)
        for origin in by_remittance.get(card.get('cd_remessa'), []):
            identity = (
                origin.get('cd_paciente'),
                origin.get('cd_atendimento'),
            )
            if not all(identity) or not origin.get('nm_paciente'):
                continue
            guide = str(origin.get('nr_guia') or '').strip()
            for code in (origin.get('cd_pro_fat'), origin.get('cd_tuss')):
                code = str(code or '').strip()
                if guide and code:
                    index[guide, code][identity] = origin
        groups = {}
        for patient in card.get('pacientes') or []:
            for item in patient.get('itens') or []:
                recovered = None
                if missing_identity(item):
                    candidates = {}
                    guide = str(item.get('nr_guia') or '').strip()
                    for code in (
                        item.get('codigo_servico'),
                        item.get('cd_pro_fat'),
                        item.get('cd_tuss'),
                    ):
                        candidates.update(
                            index.get((guide, str(code or '').strip()), {})
                        )
                    # Existing nonzero identifiers must agree with Oracle.
                    if len(candidates) == 1:
                        origin = next(iter(candidates.values()))
                        if all(
                            not item.get(k) or item[k] == origin[k]
                            for k in ('cd_paciente', 'cd_atendimento')
                        ):
                            recovered = origin
                            for key in (
                                'cd_paciente',
                                'nm_paciente',
                                'cd_atendimento',
                            ):
                                item[key] = origin[key]
                code = (
                    recovered['cd_paciente']
                    if recovered
                    else patient.get('codigo_paciente')
                )
                name = (
                    recovered['nm_paciente']
                    if recovered
                    else patient.get('nm_paciente')
                )
                group = groups.setdefault(
                    (code, name),
                    dict(
                        codigo_paciente=code,
                        nm_paciente=name,
                        valor_itens=Decimal('0'),
                        valor_glosado=Decimal('0'),
                        valor_total_tratado=Decimal('0'),
                        itens=[],
                    ),
                )
                group['itens'].append(item)
                for target, source in (
                    ('valor_itens', 'vl_total_conta'),
                    ('valor_glosado', 'valor_glosa'),
                    ('valor_total_tratado', 'valor_total_tratado'),
                ):
                    group[target] += Decimal(str(item.get(source) or 0))
        if groups:
            card['pacientes'] = list(groups.values())
