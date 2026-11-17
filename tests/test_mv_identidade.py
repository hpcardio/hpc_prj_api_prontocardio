# ruff: noqa: PLR2004, E501
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app_prontocardio.routers import mv_identidade as rota
from app_prontocardio.services import mv_identidade as mv

SUCESSO = """<cas:serviceResponse xmlns:cas='http://www.yale.edu/tp/cas'>
  <cas:authenticationSuccess><cas:user>rh000969</cas:user></cas:authenticationSuccess>
</cas:serviceResponse>"""
FALHA = """<cas:serviceResponse xmlns:cas='http://www.yale.edu/tp/cas'>
  <cas:authenticationFailure code='INVALID_TICKET'>ticket 'ST-1' não reconhecido</cas:authenticationFailure>
</cas:serviceResponse>"""
SERVICE = 'https://receitacerta.hospitalprontocardio.com.br/mv/login/retorno/'


class FakeResult:
    def __init__(self, rows):
        self.rows = rows

    def mappings(self):
        return self

    def first(self):
        return self.rows[0] if self.rows else None

    def scalar(self):
        return self.rows[0] if self.rows else None

    def scalar_one_or_none(self):
        return self.rows[0] if self.rows else None


class FakeSession:
    def __init__(self, responses=()):
        self.responses = list(responses)
        self.executions = []
        self.commits = 0

    def execute(self, statement, params=None):
        self.executions.append((str(statement), params))
        return FakeResult(self.responses.pop(0) if self.responses else [])

    def commit(self):
        self.commits += 1


def _config(**kw):
    base = {'MV_CAS_SERVICE_URL': SERVICE, 'MV_CAS_BASE_URL': 'http://cas.test/mvautenticador-cas',
            'MV_CAS_TIMEOUT_SECONDS': 5, 'MV_PROXY_USER': 'RC_PROXY', 'MV_PROXY_PASSWORD': 'segredo',
            'ORACLE_DATABASE_URL': 'oracle+oracledb://dbamv:x@db.test:1521/?service_name=PRD2361'}
    base.update(kw)
    return SimpleNamespace(**base)


def _usuario(uid=7):
    return SimpleNamespace(id=uid, nome='Wiara', perfil='usuario', telas_permitidas=[])


# ------------------------------------------------------------- serviço


def test_resposta_cas_com_sucesso_devolve_login_em_maiusculas():
    assert mv.interpretar_resposta_cas(SUCESSO) == 'RH000969'


def test_resposta_cas_com_falha():
    with pytest.raises(mv.CasInvalido, match='INVALID_TICKET'):
        mv.interpretar_resposta_cas(FALHA)


def test_resposta_cas_ilegivel():
    with pytest.raises(mv.CasInvalido):
        mv.interpretar_resposta_cas('<html>erro</html')


@pytest.mark.parametrize('login', ['RH000969"; DROP USER X', 'rc proxy', '', '1ABC', 'A' * 31])
def test_login_fora_do_formato_e_rejeitado(login):
    with pytest.raises(mv.LoginMvInvalido):
        mv.normalizar_login(login)


def test_ticket_em_formato_invalido_nem_consulta_o_cas(monkeypatch):
    chamado = []
    monkeypatch.setattr(mv.httpx, 'get', lambda *a, **k: chamado.append(1))
    with pytest.raises(mv.CasInvalido):
        mv.validar_ticket_cas('http://cas', 'nao-e-ticket', SERVICE)
    assert chamado == []


def test_validar_ticket_envia_ticket_e_service(monkeypatch):
    chamadas = []

    def fake_get(url, params, timeout):
        chamadas.append((url, params))
        return SimpleNamespace(status_code=200, text=SUCESSO)

    monkeypatch.setattr(mv.httpx, 'get', fake_get)
    assert mv.validar_ticket_cas('http://cas/mvautenticador-cas/', 'ST-123-abc', SERVICE) == 'RH000969'
    assert chamadas == [('http://cas/mvautenticador-cas/serviceValidate', {'ticket': 'ST-123-abc', 'service': SERVICE})]


def test_conceder_proxy_executa_ddl_quando_ainda_nao_existe():
    oracle = FakeSession([[0]])
    assert mv.conceder_proxy(oracle, 'rh000969', 'rc_proxy') is True
    assert oracle.executions[1][0] == 'ALTER USER "RH000969" GRANT CONNECT THROUGH "RC_PROXY"'


def test_conceder_proxy_e_idempotente():
    oracle = FakeSession([[1]])
    assert mv.conceder_proxy(oracle, 'RH000969', 'RC_PROXY') is False
    assert len(oracle.executions) == 1


def test_dsn_vem_da_url_da_api():
    assert mv.dsn_do_oracle('oracle+oracledb://dbamv:x@2361db.cloudmv.com.br:1521/?service_name=PRD2361.db') == '2361db.cloudmv.com.br:1521/PRD2361.db'


# ------------------------------------------------------------- endpoint


@pytest.fixture
def cas_ok(monkeypatch):
    monkeypatch.setattr(rota.mv, 'validar_ticket_cas', lambda *a: 'RH000969')


def test_service_fora_da_url_autorizada():
    with pytest.raises(HTTPException) as erro:
        rota.validar_login_cas(rota.TicketCasInput(ticket='ST-1', service='https://evil.test/x'),
                               _usuario(), FakeSession(), FakeSession(), _config())
    assert erro.value.status_code == 400


def test_ticket_recusado(monkeypatch):
    def recusa(*a):
        raise mv.CasInvalido('recusado')
    monkeypatch.setattr(rota.mv, 'validar_ticket_cas', recusa)
    with pytest.raises(HTTPException) as erro:
        rota.validar_login_cas(rota.TicketCasInput(ticket='ST-1', service=SERVICE + '?s=1'),
                               _usuario(), FakeSession(), FakeSession(), _config())
    assert erro.value.status_code == 401


def test_usuario_mv_inativo(cas_ok):
    with pytest.raises(HTTPException) as erro:
        rota.validar_login_cas(rota.TicketCasInput(ticket='ST-1', service=SERVICE),
                               _usuario(), FakeSession(), FakeSession([[]]), _config())
    assert erro.value.status_code == 403


def test_login_mv_de_outro_usuario(cas_ok):
    oracle = FakeSession([[{'cd_usuario': 'RH000969', 'nm_usuario': 'WIARA'}]])
    with pytest.raises(HTTPException) as erro:
        rota.validar_login_cas(rota.TicketCasInput(ticket='ST-1', service=SERVICE),
                               _usuario(), FakeSession([[99]]), oracle, _config())
    assert erro.value.status_code == 409


def test_vincula_e_concede_proxy(cas_ok):
    oracle = FakeSession([[{'cd_usuario': 'RH000969', 'nm_usuario': 'WIARA'}], [0]])
    vinculo = {'usuario_id': 7, 'cd_usuario_mv': 'RH000969', 'nm_usuario_mv': 'WIARA', 'origem': 'cas',
               'validado_cas_em': 'x', 'proxy_concedido_em': 'x', 'vinculado_em': 'x'}
    postgres = FakeSession([[], [], [vinculo]])

    resposta = rota.validar_login_cas(rota.TicketCasInput(ticket='ST-1', service=SERVICE),
                                      _usuario(), postgres, oracle, _config())

    assert any('GRANT CONNECT THROUGH "RC_PROXY"' in sql for sql, _ in oracle.executions)
    assert postgres.executions[1][1] == {'usuario_id': 7, 'login': 'RH000969', 'nome': 'WIARA', 'proxy_concedido': True}
    assert postgres.commits == 1
    assert resposta['pode_gravar_no_mv'] is True


def test_sem_senha_do_proxy_vincula_mas_nao_libera_gravacao(cas_ok):
    oracle = FakeSession([[{'cd_usuario': 'RH000969', 'nm_usuario': 'WIARA'}]])
    vinculo = {'usuario_id': 7, 'cd_usuario_mv': 'RH000969', 'nm_usuario_mv': 'WIARA', 'origem': 'cas',
               'validado_cas_em': 'x', 'proxy_concedido_em': None, 'vinculado_em': 'x'}
    postgres = FakeSession([[], [], [vinculo]])

    resposta = rota.validar_login_cas(rota.TicketCasInput(ticket='ST-1', service=SERVICE),
                                      _usuario(), postgres, oracle, _config(MV_PROXY_PASSWORD=None))

    assert not any('GRANT' in sql for sql, _ in oracle.executions)
    assert postgres.executions[1][1]['proxy_concedido'] is False
    assert resposta['pode_gravar_no_mv'] is False


def test_vinculo_manual_nao_libera_gravacao():
    postgres = FakeSession([[{'usuario_id': 7, 'cd_usuario_mv': 'RH000969', 'origem': 'manual',
                              'proxy_concedido_em': None}]])
    assert rota.meu_vinculo(_usuario(), postgres)['pode_gravar_no_mv'] is False


def test_teste_de_sessao_exige_login_pelo_cas():
    with pytest.raises(HTTPException) as erro:
        rota.testar_sessao_mv(_usuario(), FakeSession([[]]), _config())
    assert erro.value.status_code == 412


def test_teste_de_sessao_confere_usuario_oracle(monkeypatch):
    @contextmanager
    def sessao_fake(login, **kw):
        cursor = SimpleNamespace(execute=lambda sql: None, fetchone=lambda: ('RH000969', 'RC_PROXY'))
        assert kw['proxy_user'] == 'RC_PROXY'
        assert kw['dsn'] == 'db.test:1521/PRD2361'
        yield SimpleNamespace(cursor=lambda: cursor)

    monkeypatch.setattr(rota.mv, 'sessao_mv', sessao_fake)
    postgres = FakeSession([[{'usuario_id': 7, 'cd_usuario_mv': 'RH000969', 'origem': 'cas',
                              'proxy_concedido_em': 'x'}]])

    assert rota.testar_sessao_mv(_usuario(), postgres, _config()) == {
        'usuario_oracle': 'RH000969', 'proxy': 'RC_PROXY', 'login_mv': 'RH000969', 'confere': True}
