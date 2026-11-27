"""Senhas das guias de cada remessa do MV (GUIA.CD_SENHA).

Usadas para casar a remessa com o processo do portal ISSEC pelas guias.
A consulta no MV de todas as remessas do ISSEC leva cerca de um minuto, então
as senhas ficam em memória e são carregadas em segundo plano, em lotes,
desde a subida da API. Uma remessa fechada não muda de guias: depois da
primeira carga só as remessas novas vão ao MV. Enquanto uma remessa não foi
carregada, ela não tem senhas e o fluxo usa os outros critérios (mês de
produção e valor); a resposta da tela informa que as guias estão carregando.
"""

import logging
import threading
import time
from collections.abc import Callable, Iterable

# Logger do uvicorn: aparece no log do container (docker logs).
logger = logging.getLogger('uvicorn.error')

# O payables corta consultas ao MV em 20 s (PAYABLES_MV_CALL_TIMEOUT_MS): um
# lote de 200 remessas leva poucos segundos.
TAMANHO_LOTE = 200
TENTATIVAS_POR_LOTE = 3
CarregarLote = Callable[[list[int]], dict[int, set[str]]]


class SenhasDasRemessas:
    def __init__(
        self,
        carregar_lote: CarregarLote,
        *,
        em_segundo_plano: bool = True,
        espera_entre_tentativas: float = 5.0,
    ):
        self._carregar_lote = carregar_lote
        self._em_segundo_plano = em_segundo_plano
        self._espera = espera_entre_tentativas
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

    def faltam(self, cd_remessas: Iterable[int]) -> int:
        """Quantas remessas ainda não têm as senhas carregadas."""
        with self._lock:
            return sum(1 for cd in cd_remessas if cd not in self._senhas)

    def _carregar_com_tentativas(self, lote: list[int]):
        for tentativa in range(1, TENTATIVAS_POR_LOTE + 1):
            try:
                return self._carregar_lote(lote)
            except Exception:  # noqa: BLE001 - registrado e tentado de novo
                logger.warning(
                    'Senhas das guias: lote de %s remessas falhou '
                    '(tentativa %s de %s).',
                    len(lote),
                    tentativa,
                    TENTATIVAS_POR_LOTE,
                    exc_info=True,
                )
                if tentativa < TENTATIVAS_POR_LOTE:
                    time.sleep(self._espera)
        return None

    def _carregar(self, cd_remessas: list[int]) -> None:
        inicio = time.monotonic()
        carregadas = falhas = 0
        logger.warning(
            'Senhas das guias: carregando %s remessas do MV.', len(cd_remessas)
        )
        try:
            for i in range(0, len(cd_remessas), TAMANHO_LOTE):
                lote = cd_remessas[i:i + TAMANHO_LOTE]
                senhas = self._carregar_com_tentativas(lote)
                if senhas is None:
                    falhas += len(lote)
                    continue
                with self._lock:
                    for cd in lote:
                        self._senhas[cd] = frozenset(senhas.get(cd, ()))
                carregadas += len(lote)
        except Exception:  # noqa: BLE001 - não pode derrubar a API
            logger.exception('Senhas das guias: falha inesperada na carga.')
        finally:
            with self._lock:
                self._carregando = False
            logger.warning(
                'Senhas das guias: %s remessas carregadas, %s com falha, '
                'em %.0f s.',
                carregadas,
                falhas,
                time.monotonic() - inicio,
            )
