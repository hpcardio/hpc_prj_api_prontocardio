import logging
from collections.abc import Mapping
from typing import Any

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

PANEL_ORACLE_CALL_TIMEOUT_MS = 4_000


def executar_consulta_painel(
    session: Session,
    statement: Any,
    params: Mapping[str, Any],
) -> list[Any]:
    """Executa e consome uma consulta curta de painel com limite no driver."""
    driver_connection = session.connection().connection.driver_connection
    timeout_anterior = driver_connection.call_timeout
    driver_connection.call_timeout = PANEL_ORACLE_CALL_TIMEOUT_MS
    try:
        return session.execute(statement, params).mappings().all()
    except SQLAlchemyError:
        try:
            session.invalidate()
        except Exception:
            logger.exception(
                'Falha ao invalidar uma conexão Oracle do painel.'
            )
        raise
    finally:
        try:
            driver_connection.call_timeout = timeout_anterior
        except Exception:
            logger.debug(
                'Conexão Oracle do painel já estava indisponível '
                'ao restaurar timeout.',
                exc_info=True,
            )
