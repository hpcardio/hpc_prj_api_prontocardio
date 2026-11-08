# ruff: noqa: PLR2004
from types import SimpleNamespace

import pytest
from sqlalchemy.exc import DBAPIError

from app_prontocardio.services import payables_mv


class Result:
    def __init__(self, rows):
        self.rows = rows

    def mappings(self):
        return self

    def all(self):
        return self.rows


class Session:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0
        self.invalidated = 0

    def execute(self, _statement, _params):
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return Result(outcome)

    def invalidate(self):
        self.invalidated += 1


def lost(invalidated=True):
    error = DBAPIError('SELECT 1', {}, Exception('DPY-4011'))
    error.connection_invalidated = invalidated
    return error


def test_select_is_retried_once_after_a_lost_connection():
    session = Session([lost(), [{'a': 1}]])
    assert payables_mv.read_payables_rows(session, 'q', {}) == [{'a': 1}]
    assert (session.calls, session.invalidated) == (2, 1)


def test_other_database_errors_are_not_retried():
    session = Session([lost(invalidated=False)])
    with pytest.raises(DBAPIError):
        payables_mv.read_payables_rows(session, 'q', {})
    assert session.calls == 1


def test_second_failure_is_raised():
    session = Session([lost(), lost()])
    with pytest.raises(DBAPIError):
        payables_mv.read_payables_rows(session, 'q', {})
    assert session.calls == 2


def test_connect_timeout_is_off_without_configuration(monkeypatch):
    monkeypatch.delenv('PAYABLES_MV_CONNECT_TIMEOUT_SECONDS', raising=False)
    params = {'dsn': 'host:1521/service'}
    payables_mv.bound_connect_timeout(params)
    assert params == {'dsn': 'host:1521/service'}


def test_connect_timeout_is_applied_when_configured(monkeypatch):
    monkeypatch.setenv('PAYABLES_MV_CONNECT_TIMEOUT_SECONDS', '3')
    params = {'dsn': 'dbhost:1521/service'}
    payables_mv.bound_connect_timeout(params)
    assert 'dbhost' in params['dsn']
    assert params['dsn'] != 'dbhost:1521/service'
    assert SimpleNamespace(dsn=params['dsn'])
