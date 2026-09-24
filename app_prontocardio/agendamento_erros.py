from __future__ import annotations

import re


_CODIGO_MV = re.compile(r'\b(?:ORA|DPY|DPI)-\d+\b')


def detalhe_seguro_atualizacao_paciente(
    exc: Exception,
    *,
    etapa: str,
) -> str:
    correspondencia = _CODIGO_MV.search(str(exc))
    codigo = correspondencia.group(0) if correspondencia else 'ERRO-INTERNO'
    return (
        'Não foi possível atualizar os dados do paciente no MV. '
        f'Etapa: {etapa}. Código: {codigo}.'
    )
