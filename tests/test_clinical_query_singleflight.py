import threading
import time

from app_prontocardio.routers import painel_leitos_soulmv as painel


def test_four_identical_clinical_queries_execute_loader_once(monkeypatch):
    cache_name = f"singleflight-test-{time.monotonic_ns()}"
    calls = 0
    calls_lock = threading.Lock()
    start = threading.Barrier(4)
    results = []
    errors = []

    def fake_fetcher(sql, binds):
        nonlocal calls
        with calls_lock:
            calls += 1
        time.sleep(0.2)
        return [{"CONTEUDO": "resultado"}]

    monkeypatch.setattr(painel, "fetch_all_readonly", fake_fetcher)

    def request():
        try:
            start.wait(timeout=2)
            results.append(
                painel._cached_clinical_rows(
                    cache_name,
                    123,
                    "select 1 from dual",
                    readonly=True,
                )
            )
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=request) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=3)

    assert not errors
    assert len(results) == 4
    assert calls == 1
    assert all(result == [{"CONTEUDO": "resultado"}] for result in results)
