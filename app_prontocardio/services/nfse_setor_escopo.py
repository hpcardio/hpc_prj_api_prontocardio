"""Escopo da fila "NFS-e do setor" pela regra usuário -> oficina do MV.

Quem tem oficina na regra do MV (USUARIOS_OFICINA_FUNCAO) vê só as NFS-e
das oficinas dele, qualquer que seja o perfil. Um fornecedor é da oficina do
usuário quando:

1. a regra do fornecedor (calculada pelo histórico de entradas) aponta para
   uma oficina do usuário; ou
2. a regra do fornecedor aponta para uma oficina que hoje não tem ninguém
   na regra de usuários (reorganização) — ou o fornecedor não tem regra — e
   o lançador mais recente desse fornecedor que tem oficina na regra atual
   é da oficina do usuário.

Uma oficina com usuários na regra atual nunca perde fornecedores para outra.
A oficina exibida é sempre a da regra do usuário, mesmo que o lançamento
antigo mencione outra.
"""

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field


@dataclass(frozen=True)
class EscopoFila:
    oficinas: frozenset[int]
    # Regra do MV de todos os usuários: login -> oficinas.
    oficinas_por_login: dict[str, frozenset[int]] = field(default_factory=dict)

    @property
    def oficinas_ativas(self) -> frozenset[int]:
        """Oficinas com pelo menos um usuário na regra atual."""
        return frozenset().union(*self.oficinas_por_login.values())

    def oficina_do_fornecedor(
        self, oficina_regra: int | None, lancadores: Iterable[str] = ()
    ) -> int | None:
        if oficina_regra is not None and oficina_regra in self.oficinas:
            return oficina_regra
        if oficina_regra is not None and oficina_regra in self.oficinas_ativas:
            return None  # é de outra oficina que tem gente na regra
        for login in lancadores:
            do_lancador = self.oficinas_por_login.get(
                (login or '').strip().upper()
            )
            if do_lancador:
                comum = do_lancador & self.oficinas
                return min(comum) if comum else None
        return None


def oficinas_por_login(linhas) -> dict[str, frozenset[int]]:
    """``linhas``: (cd_usuario, cd_oficina) da regra do MV."""
    por_login: dict[str, set[int]] = defaultdict(set)
    for linha in linhas:
        login = str(linha['cd_usuario']).strip().upper()
        por_login[login].add(int(linha['cd_oficina']))
    return {login: frozenset(ofs) for login, ofs in por_login.items()}


def lancadores_por_fornecedor(lancamentos) -> dict[str, list[str]]:
    """CNPJ -> logins que lançaram, do mais recente para o mais antigo."""
    ordenados = sorted(
        lancamentos, key=lambda linha: linha['dt_entrada'] or 0, reverse=True
    )
    resultado: dict[str, list[str]] = defaultdict(list)
    for linha in ordenados:
        login = (linha['cd_usuario'] or '').strip().upper()
        if login and login not in resultado[linha['cnpj']]:
            resultado[linha['cnpj']].append(login)
    return resultado
