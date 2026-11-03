# ruff: noqa: PLR2004
# Literal expected values make these portal fixtures explicit.
from decimal import Decimal
from types import SimpleNamespace
from urllib.parse import parse_qs

import dlt
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from lxml import etree as ET
from pydantic import SecretStr
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app_prontocardio.models import table_registry
from app_prontocardio.petrobras_models import (
    PetrobrasExecucao,
    PetrobrasGlosa,
    PetrobrasProtocolo,
)
from app_prontocardio.routers import petrobras
from app_prontocardio.services import petrobras_portal as portal
from app_prontocardio.services.petrobras_config import PetrobrasConfig
from app_prontocardio.services.petrobras_portal_pipeline import (
    detail_guides,
    make_pipeline,
    pending_lots,
    stored_xml,
)
from app_prontocardio.services.petrobras_sync import import_rows
from app_prontocardio.services.petrobras_tiss import NS, TissError, element

LOTE = '35810370'


@pytest.fixture
def config():
    return PetrobrasConfig(
        enabled=True,
        login=SecretStr('synthetic'),
        password=SecretStr('synthetic-only'),
        prestador_cnpj='00000000000000',
        portal_login='00000000000000',
        portal_password=SecretStr('portal-synthetic'),
    )


@pytest.fixture
def db():
    engine = create_engine(
        'sqlite://',
        poolclass=StaticPool,
        connect_args={'check_same_thread': False},
        execution_options={'schema_translate_map': {'api_prontocardio': None}},
    )
    tables = [
        t
        for t in table_registry.metadata.sorted_tables
        if t.name.startswith('petrobras_')
    ]
    table_registry.metadata.create_all(engine, tables=tables)
    with Session(engine) as session:
        yield session
    engine.dispose()


@pytest.fixture
def destination(tmp_path):
    return dlt.destinations.sqlalchemy(f'sqlite:///{tmp_path / "dlt.db"}')


def portal_xml(protocolo='123', login='00000000000000'):
    """Portal DEMONSTRATIVO_ANALISE_CONTA with the embedded login."""
    root = ET.Element('{%s}mensagemTISS' % NS, nsmap={'ans': NS})
    head = element(root, 'cabecalho')
    element(
        element(head, 'identificacaoTransacao'),
        'tipoTransacao',
        'DEMONSTRATIVO_ANALISE_CONTA',
    )
    secret = element(head, 'loginSenhaPrestador')
    element(secret, 'loginPrestador', login)
    element(secret, 'senhaPrestador', 'portal-synthetic')
    demo = element(
        element(
            element(root, 'operadoraParaPrestador'), 'demonstrativosRetorno'
        ),
        'demonstrativoAnaliseConta',
    )
    h = element(demo, 'cabecalhoDemonstrativo')
    element(h, 'registroANS', '422631')
    element(h, 'numeroDemonstrativo', '1544637')
    element(h, 'dataEmissao', '2026-09-28')
    p = element(element(demo, 'dadosConta'), 'dadosProtocolo')
    element(p, 'numeroLotePrestador', LOTE)
    element(p, 'numeroProtocolo', protocolo)
    g = element(p, 'relacaoGuias')
    element(g, 'numeroGuiaPrestador', '0000000000000B392968')
    element(g, 'numeroGuiaOperadora', '122750063')
    element(g, 'senha', 'B392968')
    element(g, 'numeroCarteira', '070000654002')
    for seq, code, glosa in [
        ('1', '10101039', '74.00'),
        ('2', '70905096', '6.61'),
        ('3', '70223777', None),
    ]:
        d = element(g, 'detalhesGuia')
        element(d, 'sequencialItem', seq)
        element(d, 'dataRealizacao', '2026-07-09')
        proc = element(d, 'procedimento')
        element(proc, 'codigoTabela', '0')
        element(proc, 'codigoProcedimento', code)
        element(proc, 'descricaoProcedimento', f'ITEM {code}')
        element(d, 'qtdExecutada', '1')
        element(d, 'valorInformado', '10.00')
        if glosa:
            r = element(d, 'relacaoGlosa')
            element(r, 'tipoGlosa', '3253')
            element(r, 'valorGlosa', glosa)
    element(g, 'valorGlosaGuia', '80.61')
    return ET.tostring(root, xml_declaration=True, encoding='iso-8859-1')


def guide_row(db, config, lote=LOTE):
    protocol = PetrobrasProtocolo(
        prestador=config.prestador_cnpj, numero='123'
    )
    db.add(protocol)
    db.commit()
    source = {
        'chave': 'guide-key',
        'prestador': config.prestador_cnpj,
        'demonstrativo': '1544637',
        'protocolo': '123',
        'lote': lote,
        'escopo': 'guia_pagamento',
        'sequencial': '',
        'guia_prestador': '0000000000000B392968',
        'senha': 'B392968',
        'tuss': None,
        'valor_glosa': '80.61',
        'motivos': [],
    }
    import_rows(db, protocol, [source], set(), [], config)
    db.commit()
    return protocol


class FakePortal:
    def __init__(self, xml=None, error=None):
        self.xml, self.error, self.calls, self.closed = xml, error, [], False

    def analysis(self, lote):
        self.calls.append(lote)
        if self.error:
            raise TissError(self.error)
        return portal.sanitize(self.xml)

    def logout(self):
        self.closed = True


def test_sanitize_drops_embedded_credentials_and_checks_transaction():
    root = portal.sanitize(portal_xml())
    text = ET.tostring(root, encoding='unicode')
    assert 'portal-synthetic' not in text
    assert 'loginSenhaPrestador' not in text
    assert 'B392968' in text
    other = portal_xml().replace(
        b'DEMONSTRATIVO_ANALISE_CONTA', b'DEMONSTRATIVO_PAGAMENTO'
    )
    with pytest.raises(TissError, match='demonstrativo de análise'):
        portal.sanitize(other)


class FakeResponse:
    def __init__(self, url, body):
        self.url, self.body = url, body

    def geturl(self):
        return self.url

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeOpener:
    def __init__(self, analysis=None, login_ok=True):
        self.analysis, self.login_ok, self.requests = analysis, login_ok, []

    def open(self, request, timeout):
        url = request.full_url
        body = parse_qs(request.data.decode()) if request.data else {}
        self.requests.append((url, body))
        if url.endswith(portal.LOGIN):
            page = (
                b'<form id="formPrestador" name="formPrestador" '
                b'action="/saudeweb/seguranca/login;jsessionid=X" '
                b'method="post">'
                b'<input type="hidden" name="lt" value="LT-1" />'
            )
            return FakeResponse(url, page)
        if 'jsessionid' in url:
            landing = (
                portal.PORTAL + '/conectividade/prestador/home.htm'
                if self.login_ok
                else portal.PORTAL + portal.LOGIN
            )
            return FakeResponse(landing, b'<html/>')
        if url.endswith(portal.ANALYSIS):
            return FakeResponse(url, self.analysis)
        return FakeResponse(url, b'<html/>')


def test_client_logs_in_selects_operator_and_downloads_lot(config):
    opener = FakeOpener(portal_xml())
    client = portal.PortalClient(config, opener=opener)
    root = client.analysis(LOTE)
    assert root.find('.//{%s}loginSenhaPrestador' % NS) is None
    urls = [u for u, _ in opener.requests]
    assert any(
        'trocaroperadora.htm' in u and 'codigoOperadora=44' in u for u in urls
    )
    login = next(b for u, b in opener.requests if 'jsessionid' in u)
    assert login['username'] == ['00000000000000']
    assert login['password'] == ['portal-synthetic']
    download = opener.requests[-1][1]
    assert download['numeroProtocolo'] == [f'{LOTE}L']
    assert download['codigoEmpresa'] == ['00000000000000']
    # Only the portal host is ever contacted.
    assert all(u.startswith(portal.PORTAL + '/') for u in urls)
    client.logout()
    assert opener.requests[-1][0].endswith(portal.LOGOUT)


def test_client_rejects_refused_login_html_and_invalid_lot(config):
    with pytest.raises(TissError, match='Login'):
        portal.PortalClient(config, opener=FakeOpener(login_ok=False)).login()
    client = portal.PortalClient(config, opener=FakeOpener(b'<html>home'))
    with pytest.raises(TissError, match='sem demonstrativo'):
        client.analysis(LOTE)
    with pytest.raises(TissError, match='inválido'):
        client.analysis('1 OR 1=1')


def test_redirect_outside_portal_is_refused():
    handler = portal.SameHost()
    request = SimpleNamespace(full_url=portal.PORTAL + '/x')
    with pytest.raises(TissError, match='Redirecionamento'):
        handler.redirect_request(
            request, None, 302, 'Found', {}, 'https://example.com/login'
        )


def test_portal_requires_configured_login(config):
    config.portal_password = SecretStr('')
    with pytest.raises(ValueError, match='portal'):
        portal.PortalClient(config)


def test_detail_guides_loads_xml_with_dlt_and_imports_items(
    db, config, destination
):
    guide_row(db, config)
    assert pending_lots(db, config.prestador_cnpj) == {LOTE: '123'}
    fake = FakePortal(portal_xml())
    result = detail_guides(
        db, config, lambda *a: [], 1, client=fake, destination=destination
    )
    assert fake.calls == [LOTE]
    assert fake.closed
    assert result['situacao'] == 'concluida'
    assert result['lotes_detalhados'] == 1
    items = db.scalars(
        select(PetrobrasGlosa).where(PetrobrasGlosa.tuss.is_not(None))
    ).all()
    # The item without glosa is not imported.
    assert sorted((i.tuss, i.valor_glosa) for i in items) == [
        ('10101039', Decimal('74.00')),
        ('70905096', Decimal('6.61')),
    ]
    assert {i.fonte['origem'] for i in items} == {'portal_analise_conta'}
    assert {i.fonte['lote'] for i in items} == {LOTE}
    assert pending_lots(db, config.prestador_cnpj) == {}
    # dlt keeps the sanitized source; the embedded password never lands.
    stored = stored_xml(make_pipeline(destination, None), {LOTE})
    assert 'B392968' in stored[LOTE]
    assert 'portal-synthetic' not in stored[LOTE]
    run = db.get(PetrobrasExecucao, result['execucao_id'])
    assert run.situacao == 'concluida'
    assert run.quantidade == 2


def test_detail_guides_keeps_guide_on_portal_or_protocol_failure(
    db, config, destination
):
    guide_row(db, config)
    result = detail_guides(
        db,
        config,
        lambda *a: [],
        client=FakePortal(
            error='Portal sem demonstrativo de análise do lote.'
        ),
        destination=destination,
    )
    assert result['situacao'] == 'parcial'
    assert result['falhas'] == {
        LOTE: 'Portal sem demonstrativo de análise do lote.'
    }
    wrong = detail_guides(
        db,
        config,
        lambda *a: [],
        client=FakePortal(portal_xml(protocolo='999')),
        destination=destination,
    )
    assert wrong['falhas'] == {
        LOTE: 'Portal retornou protocolo diferente do lote.'
    }
    assert (
        db.scalar(
            select(PetrobrasGlosa.tuss).where(PetrobrasGlosa.tuss.is_not(None))
        )
        is None
    )
    assert pending_lots(db, config.prestador_cnpj) == {LOTE: '123'}


def test_nothing_pending_skips_portal(db, config, destination):
    fake = FakePortal(portal_xml())
    result = detail_guides(
        db, config, lambda *a: [], client=fake, destination=destination
    )
    assert result['lotes'] == 0
    assert fake.calls == []


def test_portal_endpoint_requires_writer_and_returns_summary(
    db, config, monkeypatch
):
    calls = []
    monkeypatch.setattr(
        petrobras,
        'detail_guides',
        lambda *a: calls.append(a) or {'situacao': 'concluida'},
    )
    app = FastAPI()
    app.include_router(petrobras.router)
    user = SimpleNamespace(
        id=1, perfil='usuario', telas_permitidas=['follow_up_glosas']
    )
    app.dependency_overrides[petrobras.valida_token_usuario_atual] = lambda: (
        user
    )
    app.dependency_overrides[petrobras.get_session_postgres] = lambda: db
    app.dependency_overrides[petrobras.get_config] = lambda: config
    response = TestClient(app).post('/app_glosas/petrobras/portal/detalhar')
    assert response.status_code == 200
    assert response.json() == {'situacao': 'concluida'}
    assert calls[0][3] == 1
