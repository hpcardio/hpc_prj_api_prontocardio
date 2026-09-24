def numero_oracle(valor: str | None) -> int | None:
    """Converte uma sequência de dígitos para bind NUMBER do Oracle."""
    return int(valor) if valor else None


def binds_contato_paciente(
    *,
    ddi_celular: str | None,
    ddd_celular: str | None,
    celular: str | None,
    ddi_fone: str | None,
    ddd_fone: str | None,
    fone: str | None,
    ddi_comercial: str | None,
    ddd_comercial: str | None,
    fone_comercial: str | None,
) -> dict[str, int | str | None]:
    """Monta os binds de contato conforme os tipos de DBAMV.PACIENTE."""
    return {
        'nr_ddi_celular': numero_oracle(ddi_celular),
        'nr_ddd_celular': numero_oracle(ddd_celular),
        'nr_celular': celular,
        'nr_ddi_fone': numero_oracle(ddi_fone),
        'nr_ddd_fone': numero_oracle(ddd_fone),
        'nr_fone': fone,
        'nr_ddi_fone_comercial': numero_oracle(ddi_comercial),
        'nr_ddd_fone_comercial': numero_oracle(ddd_comercial),
        'nr_fone_comercial': numero_oracle(fone_comercial),
    }
