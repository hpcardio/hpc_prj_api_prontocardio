from __future__ import annotations

import json
import re
from collections.abc import Callable
from urllib.error import HTTPError, URLError
from urllib.request import urlopen


class CepErro(ValueError):
    """Erro de validação ou consulta de CEP."""


def resolver_cidade_rows(rows: list[dict]) -> dict[str, int | str | None]:
    exatas_ibge = [row for row in rows if int(row.get('exata_ibge') or 0) == 1]
    candidatas = exatas_ibge or rows
    if len(candidatas) != 1:
        return {'cd_cidade': None, 'correspondencia_cidade': 'pendente'}
    return {
        'cd_cidade': int(candidatas[0]['cd_cidade']),
        'correspondencia_cidade': 'exata',
    }


def consultar_viacep(
    cep: str,
    *,
    opener: Callable = urlopen,
) -> dict[str, str]:
    digitos = re.sub(r'\D', '', cep or '')
    if len(digitos) != 8:
        raise CepErro('CEP deve conter oito dígitos.')

    try:
        with opener(
            f'https://viacep.com.br/ws/{digitos}/json/', timeout=4
        ) as response:
            payload = json.loads(response.read().decode('utf-8'))
    except (HTTPError, URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        raise CepErro('Serviço de CEP indisponível. Preencha manualmente.') from exc

    if payload.get('erro'):
        raise CepErro('CEP não encontrado. Confira ou preencha manualmente.')

    return {
        'cep': digitos,
        'logradouro': str(payload.get('logradouro') or '').strip(),
        'bairro': str(payload.get('bairro') or '').strip(),
        'cidade': str(payload.get('localidade') or '').strip(),
        'uf': str(payload.get('uf') or '').strip().upper(),
        'codigo_ibge': str(payload.get('ibge') or '').strip(),
    }
