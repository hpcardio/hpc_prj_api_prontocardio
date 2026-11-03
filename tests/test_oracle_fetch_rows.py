# ruff: noqa: PLR2004
# Literal batch sizes make the opt-in contract explicit.
from types import SimpleNamespace

from app_prontocardio import database
from app_prontocardio.routers import contas_pagar


def run(options):
    cursor = SimpleNamespace(arraysize=100)
    database.configurar_lote_prontoglosa(
        None,
        cursor,
        'SELECT 1',
        {},
        SimpleNamespace(execution_options=options),
        False,
    )
    return cursor.arraysize


def test_only_marked_statements_get_larger_fetch_batch():
    assert run({database.FETCH_ROWS_OPTION: 1000}) == 1000
    assert run({}) == 100
    assert run({database.FETCH_ROWS_OPTION: 50}) == 100


def test_titles_query_opts_into_fetch_batch():
    source = open(contas_pagar.__file__, encoding='utf-8').read()
    assert 'ORACLE_TITULOS_QUERY.execution_options(' in source
    marked = contas_pagar.ORACLE_TITULOS_QUERY.execution_options(
        prontoglosa_fetch_rows=1000
    )
    assert marked.get_execution_options()[database.FETCH_ROWS_OPTION] == 1000
    # The shared statement itself stays unmarked for other callers.
    assert database.FETCH_ROWS_OPTION not in (
        contas_pagar.ORACLE_TITULOS_QUERY.get_execution_options()
    )
