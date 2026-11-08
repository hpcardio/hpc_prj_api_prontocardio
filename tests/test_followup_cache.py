# ruff: noqa: PLR2004
# Literal call counts make the cache contract explicit.
import threading
import time
from http import HTTPStatus

import pytest

from app_prontocardio.routers import financeiro
from app_prontocardio.services import followup_cache

URL = '/app_glosas/financeiro/conciliacao-faturamento/glosas-pendentes'
chamadas = []


def pagina(params):
    return {
        'cards': [],
        'total': len(chamadas),
        'quantidade_glosas': 0,
        'valor_total_glosado': '0',
        'valor_total_pendente': '0',
        'valor_total_tratado': '0',
        'valor_total_recursado': '0',
        'valor_total_acatado': '0',
        'limit': params['limit'],
        'offset': params['offset'],
    }


@pytest.fixture(autouse=True)
def isolado(monkeypatch):
    chamadas.clear()
    followup_cache.clear()

    def contar(**params):
        chamadas.append(params)
        return pagina(params)

    monkeypatch.setattr(financeiro, 'consultar_follow_up_glosas', contar)
    yield
    followup_cache.clear()


def esperar(condicao, segundos=5):
    limite = time.monotonic() + segundos
    while not condicao() and time.monotonic() < limite:
        time.sleep(0.01)
    return condicao()


def test_listagem_reusa_resultado_e_ignora_escritas_fora_de_glosas(
    cliente, token_teste
):
    headers = {'Authorization': f'Bearer {token_teste}'}
    params = {'limit': 10, 'offset': 0, 'incluir_detalhes': 'false'}
    primeira = cliente.get(URL, params=params, headers=headers)
    segunda = cliente.get(URL, params=params, headers=headers)
    assert primeira.status_code == HTTPStatus.OK
    assert segunda.json() == primeira.json()
    assert len(chamadas) == 1
    cliente.get(URL, params={**params, 'offset': 10}, headers=headers)
    assert len(chamadas) == 2
    geracao = followup_cache.generation()
    cliente.post(
        '/autenticacao/esqueci-senha', json={'email': 'ninguem@teste.com'}
    )
    assert followup_cache.generation() == geracao
    cliente.get(URL, params=params, headers=headers)
    assert len(chamadas) == 2


def test_somente_gravacao_de_glosa_com_sucesso_invalida():
    glosas = '/app_glosas/registro-glosa'
    assert followup_cache.invalidates('POST', glosas, 201)
    assert followup_cache.invalidates('DELETE', '/app_glosas/x/1', 204)
    assert not followup_cache.invalidates('POST', glosas, 422)
    assert not followup_cache.invalidates('GET', glosas, 200)
    assert not followup_cache.invalidates('POST', '/autenticacao/token', 200)
    assert not followup_cache.invalidates('POST', '/whatsapp/webhook', 200)


def test_detalhes_nunca_usam_cache(cliente, token_teste):
    headers = {'Authorization': f'Bearer {token_teste}'}
    params = {'limit': 10, 'offset': 0, 'incluir_detalhes': 'true'}
    cliente.get(URL, params=params, headers=headers)
    cliente.get(URL, params=params, headers=headers)
    assert len(chamadas) == 2


def carga(contador, valor=None):
    def loader(session, session_oracle):
        contador.append(1)
        return {'valor': valor if valor is not None else len(contador)}

    return loader


PARAMS = {'limit': 10, 'session': None, 'session_oracle': None}


def test_gravacao_nunca_serve_pagina_antiga_e_preaquece():
    contador = []
    assert followup_cache.cached(PARAMS, carga(contador))['valor'] == 1
    followup_cache.bump_generation()
    # The prewarm recomputes the used page in the background (own sessions).
    assert esperar(lambda: len(contador) == 2)
    assert followup_cache.cached(PARAMS, carga(contador))['valor'] == 2
    assert len(contador) == 2


def test_pagina_vencida_sem_gravacao_volta_na_hora_e_atualiza(monkeypatch):
    contador = []
    followup_cache.cached(PARAMS, carga(contador))
    monkeypatch.setattr(followup_cache, 'FRESH_SECONDS', -1)
    liberar = threading.Event()

    def lenta(session, session_oracle):
        liberar.wait(5)
        contador.append(1)
        return {'valor': 'novo'}

    # Expired but same generation: old page at once, refresh in background.
    assert followup_cache.cached(PARAMS, lenta)['valor'] == 1
    liberar.set()
    assert esperar(lambda: len(contador) == 2)
    monkeypatch.setattr(followup_cache, 'FRESH_SECONDS', 300)
    assert esperar(
        lambda: followup_cache.cached(PARAMS, lenta)['valor'] == 'novo'
    )


def test_requisicoes_simultaneas_calculam_uma_vez():
    contador = []
    liberar = threading.Event()

    def lenta(session, session_oracle):
        liberar.wait(5)
        contador.append(1)
        return {'valor': 'unico'}

    resultados = []
    threads = [
        threading.Thread(
            target=lambda: resultados.append(
                followup_cache.cached(PARAMS, lenta)
            )
        )
        for _ in range(4)
    ]
    for thread in threads:
        thread.start()
    time.sleep(0.1)
    liberar.set()
    for thread in threads:
        thread.join(5)
    assert len(contador) == 1
    assert [r['valor'] for r in resultados] == ['unico'] * 4
