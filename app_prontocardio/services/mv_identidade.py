# ruff: noqa: E501
"""Identidade MV do usuário do Receita Certa.

1. O usuário entra no CAS da MV (mvautenticador-cas) por um popup do
   Receita Certa. O CAS devolve um ticket para a URL de retorno, e a API o
   valida em ``serviceValidate``: o login MV vem do próprio CAS, nunca é
   digitado nem enviado pelo navegador.
2. Validado o login, a API concede ``CONNECT THROUGH`` ao proxy
   (``RC_PROXY``). As gravações no MV abrem a sessão como
   ``RC_PROXY[<login MV>]``: para o Oracle o usuário da sessão é o próprio
   login MV, com os papéis dele, e triggers/logs que usam ``USER`` registram
   quem fez a operação.
"""

import re
import xml.etree.ElementTree as ET
from contextlib import contextmanager

import httpx
import oracledb
from sqlalchemy import text
from sqlalchemy.engine import make_url

CAS_NS = {'cas': 'http://www.yale.edu/tp/cas'}
RE_LOGIN_MV = re.compile(r'^[A-Z][A-Z0-9_]{1,29}$')

SQL_USUARIO_MV_ATIVO = text("""
SELECT u.cd_usuario AS cd_usuario, u.nm_usuario AS nm_usuario
  FROM dbasgu.usuarios u
  JOIN dba_users d ON d.username = u.cd_usuario
 WHERE u.cd_usuario = :login
   AND NVL(u.sn_ativo, 'S') = 'S'
   AND d.account_status = 'OPEN'
""")

SQL_PROXY_EXISTE = text("""
SELECT COUNT(*) FROM proxy_users WHERE proxy = :proxy AND client = :login
""")


class CasInvalido(ValueError):
    """Ticket recusado pelo CAS ou resposta inesperada."""


class LoginMvInvalido(ValueError):
    """Login MV inexistente, inativo ou com formato inesperado."""


def normalizar_login(login: str) -> str:
    valor = (login or '').strip().upper()
    if not RE_LOGIN_MV.fullmatch(valor):
        raise LoginMvInvalido(f'Login MV com formato inesperado: {login!r}')
    return valor


def interpretar_resposta_cas(xml_resposta: str) -> str:
    """Extrai o usuário de uma resposta CAS 2.0 de ``serviceValidate``."""
    try:
        raiz = ET.fromstring(xml_resposta.strip())
    except ET.ParseError as exc:
        raise CasInvalido('Resposta do CAS da MV ilegível.') from exc
    falha = raiz.find('cas:authenticationFailure', CAS_NS)
    if falha is not None:
        codigo = falha.get('code') or 'FALHA'
        raise CasInvalido(f'O CAS da MV recusou o login ({codigo}). Entre novamente.')
    usuario = raiz.find('cas:authenticationSuccess/cas:user', CAS_NS)
    if usuario is None or not (usuario.text or '').strip():
        raise CasInvalido('O CAS da MV não informou o usuário.')
    return normalizar_login(usuario.text)


def validar_ticket_cas(base_url: str, ticket: str, service: str, timeout: float = 15) -> str:
    if not re.fullmatch(r'ST-[A-Za-z0-9.\-_]{1,250}', ticket or ''):
        raise CasInvalido('Ticket do CAS em formato inválido.')
    try:
        resposta = httpx.get(
            f'{base_url.rstrip("/")}/serviceValidate',
            params={'ticket': ticket, 'service': service},
            timeout=timeout,
        )
    except httpx.HTTPError as exc:
        raise CasInvalido('Não foi possível falar com o CAS da MV.') from exc
    if resposta.status_code != 200:  # noqa: PLR2004
        raise CasInvalido(f'O CAS da MV respondeu {resposta.status_code}.')
    return interpretar_resposta_cas(resposta.text)


def usuario_mv_ativo(oracle_session, login: str) -> dict | None:
    linha = oracle_session.execute(SQL_USUARIO_MV_ATIVO, {'login': login}).mappings().first()
    return dict(linha) if linha else None


def conceder_proxy(oracle_session, login: str, proxy_user: str) -> bool:
    """Concede CONNECT THROUGH ao proxy (idempotente). Retorna True se concedeu agora."""
    login = normalizar_login(login)
    proxy = normalizar_login(proxy_user)
    if oracle_session.execute(SQL_PROXY_EXISTE, {'proxy': proxy, 'login': login}).scalar():
        return False
    # Identificadores já validados pela regex; DDL não aceita bind.
    oracle_session.execute(text(f'ALTER USER "{login}" GRANT CONNECT THROUGH "{proxy}"'))
    return True


def dsn_do_oracle(oracle_database_url: str) -> str:
    """Monta o DSN (host:porta/serviço) a partir da URL SQLAlchemy da API."""
    url = make_url(oracle_database_url)
    servico = url.query.get('service_name') or url.database
    return f'{url.host}:{url.port or 1521}/{servico}'


@contextmanager
def sessao_mv(login: str, *, proxy_user: str, proxy_password: str, dsn: str, usuario_id: int):
    """Conexão Oracle em nome do usuário MV (RC_PROXY[login]).

    Use só para gravações que precisam registrar o usuário MV. A transação
    fica a cargo de quem chama (commit/rollback explícitos).
    """
    login = normalizar_login(login)
    conexao = oracledb.connect(
        user=f'{normalizar_login(proxy_user)}[{login}]',
        password=proxy_password,
        dsn=dsn,
    )
    try:
        conexao.module = 'RECEITA_CERTA'
        conexao.action = 'CONTAS_A_PAGAR'
        conexao.client_identifier = f'RECEITA_CERTA:{usuario_id}'
        yield conexao
    finally:
        conexao.close()
