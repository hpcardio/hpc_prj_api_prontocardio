"""Combine overlapping ISSEC cards without discarding existing treatments."""

from decimal import Decimal


def money(value):
    return Decimal(str(value or 0))


def identity(item):
    fields = ('cd_reg', 'cd_lancamento', 'nr_guia', 'cd_pro_fat')
    if any(not item.get(k) for k in fields):
        return None
    return tuple(str(item[k]).strip() for k in fields) + (
        str(item.get('dt_lancamento') or item.get('dt_atendimento') or ''),
        money(item.get('valor_glosa')),
    )


def indexed(card):
    result = {}
    for patient in card.get('pacientes') or []:
        for item in patient.get('itens') or []:
            key = identity(item)
            if key is None or key in result:
                return None
            result[key] = (patient, item)
    return result


def merge_issec_cards(cards):
    result = []
    for source in cards:
        process = str(
            (source.get('processo') or {}).get('numero_processo') or ''
        ).strip()
        if (
            str(source.get('convenio') or '').strip().upper() != 'ISSEC'
            or not process
        ):
            result.append(source)
            continue
        incoming = indexed(source)
        for pos, target in enumerate(result):
            if (
                str(target.get('convenio') or '').strip().upper() != 'ISSEC'
                or target.get('cd_remessa') != source.get('cd_remessa')
                or str(
                    (target.get('processo') or {}).get('numero_processo') or ''
                ).strip()
                != process
            ):
                continue
            existing = indexed(target)
            if (
                not incoming
                or not existing
                or not (incoming.keys() & existing.keys())
            ):
                continue
            overlap = incoming.keys() & existing.keys()
            # Two independently treated records need reconciliation, not guessing.
            if any(
                money(existing[k][1].get('valor_total_tratado')) > 0
                and money(incoming[k][1].get('valor_total_tratado')) > 0
                and existing[k][1] != incoming[k][1]
                for k in overlap
            ):
                continue
            combined = dict(existing)
            for key, pair in incoming.items():
                if key not in combined or money(
                    pair[1].get('valor_total_tratado')
                ) > money(combined[key][1].get('valor_total_tratado')):
                    combined[key] = pair
            merged = dict(target)
            patients = {}
            for patient, item in combined.values():
                key = (
                    patient.get('codigo_paciente'),
                    patient.get('nm_paciente'),
                )
                if key not in patients:
                    patients[key] = {
                        **patient,
                        'itens': [],
                        'valor_itens': Decimal(0),
                        'valor_glosado': Decimal(0),
                        'valor_total_tratado': Decimal(0),
                    }
                group = patients[key]
                # Keep session-bound treatment records intact for response validation.
                group['itens'].append(dict(item))
                for dest, field in [
                    ('valor_itens', 'vl_total_conta'),
                    ('valor_glosado', 'valor_glosa'),
                    ('valor_total_tratado', 'valor_total_tratado'),
                ]:
                    group[dest] += money(item.get(field))
            merged['pacientes'] = list(patients.values())
            for field in [
                'valor_itens',
                'valor_glosado',
                'valor_total_tratado',
            ]:
                merged[field] = sum(
                    (p[field] for p in patients.values()), Decimal(0)
                )
            merged['valor_glosa_pendente'] = max(
                merged['valor_glosado'] - merged['valor_total_tratado'],
                Decimal(0),
            )
            merged['possui_recurso'] = bool(
                target.get('possui_recurso') or source.get('possui_recurso')
            )
            result[pos] = merged
            break
        else:
            result.append(source)
    return result
