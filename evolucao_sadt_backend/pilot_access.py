import os


def _parse_attendances() -> tuple[int, ...]:
    raw = os.getenv(
        'EVOLUCAO_SADT_ATENDIMENTOS',
        os.getenv('EVOLUCAO_SADT_ATENDIMENTO', '339070'),
    )
    values: list[int] = []
    for item in raw.split(','):
        item = item.strip()
        if not item:
            continue
        value = int(item)
        if value > 0 and value not in values:
            values.append(value)
    if not values:
        raise RuntimeError('Nenhum atendimento autorizado foi configurado.')
    return tuple(values)


ATENDIMENTOS_PILOTO = _parse_attendances()
ATENDIMENTO_PILOTO = ATENDIMENTOS_PILOTO[0]
