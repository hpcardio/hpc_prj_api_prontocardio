_UNIDADES_PUBLICAS = {
    '1': (
        'Unidade Diagnóstica 01 - Endereço: Avenida Barão de Studart '
        '- Nº 560 - Meireles'
    ),
    '2': (
        'Unidade Diagnóstica 02 - Endereço: Avenida Barão de Studart '
        '- Nº 600 - Meireles'
    ),
}


def nome_unidade_publica(
    codigo: object,
    nome: str | None,
) -> str | None:
    codigo_normalizado = None if codigo is None else str(codigo).strip()
    return _UNIDADES_PUBLICAS.get(codigo_normalizado, nome)
