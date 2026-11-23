import os
import re
from collections.abc import Callable, Mapping

from sqlalchemy import text

from app_prontocardio.database import oracle_engine
from evolucao_sadt_backend.prontorede_context import VerifiedMvContext


class MvContextAuthorizationError(PermissionError):
    '''Raised when the MV context cannot be authorized for ProntoRede.'''


OracleConnectionFactory = Callable[[], object]


_AUTHORIZED_CONTEXT_QUERY = text(
    """
    SELECT A.CD_ATENDIMENTO AS cd_atendimento,
           ID.CD_USUARIO AS cd_usuario,
           ID.CD_PRESTADOR AS cd_prestador,
           CO.DS_CONSELHO AS ds_conselho,
           ID.DS_CODIGO_CONSELHO AS ds_codigo_conselho,
           ID.CD_UF_ORGAO_EMISSOR AS cd_uf_orgao_emissor,
           COUNT(*) OVER () AS match_count
      FROM DBAMV.ATENDIME A
      JOIN (
            SELECT U.CD_USUARIO,
                   P.CD_PRESTADOR,
                   P.CD_CONSELHO,
                   P.DS_CODIGO_CONSELHO,
                   P.CD_UF_ORGAO_EMISSOR
              FROM DBASGU.USUARIOS U
              JOIN DBAMV.PRESTADOR P
                ON P.CD_PRESTADOR = U.CD_PRESTADOR
             WHERE (
                    UPPER(U.CD_USUARIO) = :identity
                    OR UPPER(P.NM_PRESTADOR) = :identity
                   )
               AND NVL(P.TP_SITUACAO, 'A') = 'A'
           ) ID ON 1 = 1
      LEFT JOIN DBAMV.CONSELHO CO ON CO.CD_CONSELHO = ID.CD_CONSELHO
     WHERE A.CD_ATENDIMENTO = :cd_atendimento
       AND A.DT_ALTA IS NULL
    """
)


def resolve_authorized_mv_context(
    cd_atendimento: int,
    cd_usuario: str,
    *,
    connection_factory: OracleConnectionFactory = oracle_engine.connect,
) -> VerifiedMvContext:
    '''Revalidate an authorized CRM provider through a read-only query.'''
    attendance = _normalize_attendance(cd_atendimento)
    identity = _normalize_identity(cd_usuario)
    if not _allowed_users():
        raise MvContextAuthorizationError('Contexto MV não autorizado.')

    with connection_factory() as connection:
        row = connection.execute(
            _AUTHORIZED_CONTEXT_QUERY,
            {
                'cd_atendimento': attendance,
                'identity': identity,
            },
        ).mappings().first()

    context = _verified_context(row, attendance)
    if not _is_user_allowed(context.cd_usuario):
        raise MvContextAuthorizationError('Contexto MV não autorizado.')
    return context


def _normalize_attendance(value: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise MvContextAuthorizationError('Contexto MV não autorizado.')
    return value


def _normalize_identity(value: str) -> str:
    if not isinstance(value, str):
        raise MvContextAuthorizationError('Contexto MV não autorizado.')
    normalized = value.strip().upper()
    if not re.fullmatch(r"[A-ZÀ-ÖØ-Ý0-9_. '\-]{2,120}", normalized):
        raise MvContextAuthorizationError('Contexto MV não autorizado.')
    return normalized


def _allowed_users() -> set[str]:
    raw_users = os.getenv('PRONTOREDE_MV_ALLOWED_USERS', '')
    return {
        user.strip().upper()
        for user in raw_users.split(',')
        if user.strip()
    }


def _is_user_allowed(user: str) -> bool:
    allowed_users = _allowed_users()
    return '*' in allowed_users or user in allowed_users


def _verified_context(
    row: Mapping[str, object] | None,
    attendance: int,
) -> VerifiedMvContext:
    if not isinstance(row, Mapping):
        raise MvContextAuthorizationError('Contexto MV não autorizado.')
    if row.get('cd_atendimento') != attendance:
        raise MvContextAuthorizationError('Contexto MV não autorizado.')
    if row.get('match_count') != 1:
        raise MvContextAuthorizationError('Contexto MV não autorizado.')

    user = _normalize_row_user(row.get('cd_usuario'))

    provider = row.get('cd_prestador')
    if (
        not isinstance(provider, int)
        or isinstance(provider, bool)
        or provider <= 0
    ):
        raise MvContextAuthorizationError('Contexto MV não autorizado.')

    council = _normalized_row_text(row.get('ds_conselho'))
    number = _normalized_row_text(row.get('ds_codigo_conselho'))
    state = _normalized_row_text(row.get('cd_uf_orgao_emissor'))
    if council != 'CRM' or not number:
        raise MvContextAuthorizationError('Contexto MV não autorizado.')

    crm = f'CRM-{state} {number}' if state else f'CRM {number}'

    return VerifiedMvContext(
        cd_atendimento=attendance,
        cd_usuario=user,
        cd_prestador=provider,
        crm=crm,
    )


def _normalize_row_user(value: object) -> str:
    if not isinstance(value, str):
        raise MvContextAuthorizationError('Contexto MV não autorizado.')
    return value.strip().upper()


def _normalized_row_text(value: object) -> str:
    return value.strip().upper() if isinstance(value, str) else ''
