"""Recover a lost MV connection while reading accounts-payable rows."""

import logging
import os

import oracledb
from sqlalchemy.exc import DBAPIError

logger = logging.getLogger(__name__)


def bound_connect_timeout(connect_params):
    timeout_seconds = float(
        os.getenv('PAYABLES_MV_CONNECT_TIMEOUT_SECONDS', '0')
    )
    if timeout_seconds <= 0:
        return
    params = oracledb.ConnectParams()
    params.parse_connect_string(connect_params['dsn'])
    params.set(tcp_connect_timeout=timeout_seconds, retry_count=0)
    connect_params['dsn'] = params.get_connect_string()


def read_payables_rows(session, statement, params):
    try:
        return session.execute(statement, params).mappings().all()
    except DBAPIError as exc:
        if not exc.connection_invalidated:
            raise
        # These call sites execute only SELECTs. Never replay a request
        # handler, a PostgreSQL transaction, a snapshot, or a payment
        # operation.
        session.invalidate()
        logger.warning('MV read disconnected; retrying the SELECT once')
    return session.execute(statement, params).mappings().all()
