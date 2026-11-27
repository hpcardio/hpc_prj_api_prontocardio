"""Senhas das guias de cada remessa do MV (GUIA.CD_SENHA).

Usadas para casar a remessa com o processo do portal ISSEC pelas guias.
A consulta no MV de todas as remessas do ISSEC leva cerca de um minuto, então
as senhas ficam em memória e são carregadas em segundo plano, em lotes. Uma
remessa fechada não muda de guias: depois da primeira carga só as remessas
novas vão ao MV. Enquanto uma remessa não foi carregada, ela não tem senhas e
o fluxo usa os outros critérios (mês de produção e valor).
"""

import logging
import threading
from collections.abc import Callable, Iterable

logger = logging.getLogger(__name__)

TAMANHO_LOTE = 500
CarregarLote = Callable[[list[int]], dict[int, set[str]]]


class SenhasDasRemessas:
    def __init__(
        self, carregar_lote: CarregarLote, *, em_segundo_plano: bool = True
    ):
        self._carregar_lote = carregar_lote
        self._em_segundo_plano = em_segundo_plano
        self._senhas: dict[int, frozenset[str]] = {}
        self._lock = threading.Lock()
        self._carregando = False

    def obter(self, cd_remessas: Iterable[int]) -> dict[int, frozenset[str]]:
        """Senhas já conhecidas; dispara a carga das remessas que faltam."""
        cd_remessas = list(cd_remessas)
        with self._lock:
            faltam = [cd for cd in cd_remessas if cd not in self._senhas]
            iniciar = bool(faltam) and not self._carregando
            if iniciar:
                self._carregando = True
        if iniciar:
            if self._em_segundo_plano:
                threading.Thread(
                    target=self._carregar,
                    args=(faltam,),
                    daemon=True,
                    name='senhas-remessas',
                ).start()
            else:
                self._carregar(faltam)
        with self._lock:
            ja = self._senhas
            return {cd: ja[cd] for cd in cd_remessas if cd in ja}

    def _carregar(self, cd_remessas: list[int]) -> None:
        try:
            for inicio in range(0, len(cd_remessas), TAMANHO_LOTE):
                lote = cd_remessas[inicio:inicio + TAMANHO_LOTE]
                senhas = self._carregar_lote(lote)
                with self._lock:
                    for cd in lote:
                        self._senhas[cd] = frozenset(senhas.get(cd, ()))
        except Exception:
            logger.exception(
                'Falha ao carregar as senhas das guias das remessas no MV.'
            )
        finally:
            with self._lock:
                self._carregando = False
