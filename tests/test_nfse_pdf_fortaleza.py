# ruff: noqa: E501, PLR2004, PT018
import httpx
import pytest
from fastapi import HTTPException

from app_prontocardio.routers import contas_pagar_boletos as rota
from app_prontocardio.services import nfse_pdf_fortaleza as svc

PAGINA = '<html><object data="/grpfor/a4j/s/3_3_3.Finalorg.ajax4jsf.resource.UserResource/n/s/-1/DATA/abc" type="application/pdf"></object></html>'


def _transporte(pagina=PAGINA, pdf=b'%PDF-1.4 danfse', status=200, chamadas=None):
    def handler(request: httpx.Request):
        if chamadas is not None:
            chamadas.append(str(request.url))
        if 'consultarNota.seam' in request.url.path:
            return httpx.Response(status, text=pagina)
        return httpx.Response(200, content=pdf, headers={'content-type': 'application/pdf'})
    return httpx.MockTransport(handler)


@pytest.fixture
def portal(monkeypatch):
    estado = {'transporte': _transporte(), 'chamadas': []}
    original = httpx.Client

    def cliente(*a, **kw):
        kw['transport'] = estado['transporte']
        return original(*a, **kw)

    monkeypatch.setattr(svc.httpx, 'Client', cliente)
    return estado


def test_baixa_o_pdf_embutido_na_pagina_publica(portal):
    portal['transporte'] = _transporte(chamadas=portal['chamadas'])
    assert svc.baixar_pdf_oficial('3199621', '375518929', '154046') == b'%PDF-1.4 danfse'
    pagina, pdf = portal['chamadas']
    assert pagina.startswith('https://iss.fortaleza.ce.gov.br/grpfor/pagesPublic/consultarNota.seam?')
    assert 'codigo=375518929' in pagina and 'numero=3199621' in pagina and 'chave=154046' in pagina
    assert pdf == 'https://iss.fortaleza.ce.gov.br/grpfor/a4j/s/3_3_3.Finalorg.ajax4jsf.resource.UserResource/n/s/-1/DATA/abc'


def test_nota_nao_encontrada_no_portal(portal):
    portal['transporte'] = _transporte(pagina='<html>Nota não encontrada</html>')
    with pytest.raises(svc.PdfIndisponivel, match='não encontrou'):
        svc.baixar_pdf_oficial('1', '2', '3')


def test_resposta_que_nao_e_pdf(portal):
    portal['transporte'] = _transporte(pdf=b'<html>erro</html>')
    with pytest.raises(svc.PdfIndisponivel, match='PDF válido'):
        svc.baixar_pdf_oficial('1', '2', '3')


def test_portal_fora_do_ar(portal):
    portal['transporte'] = _transporte(status=503)
    with pytest.raises(svc.PdfIndisponivel, match='não respondeu'):
        svc.baixar_pdf_oficial('1', '2', '3')


@pytest.mark.parametrize('dados', [('', '2', '3'), ('1', None, '3'), ('1', '2', '3&x=1')])
def test_dados_invalidos_nem_chamam_o_portal(portal, dados):
    portal['transporte'] = _transporte(chamadas=portal['chamadas'])
    with pytest.raises(svc.PdfIndisponivel):
        svc.baixar_pdf_oficial(*dados)
    assert portal['chamadas'] == []


# ------------------------------------------------------------- endpoint


class FakeResult:
    def __init__(self, linha):
        self.linha = linha

    def mappings(self):
        return self

    def first(self):
        return self.linha


class FakeSession:
    def __init__(self, guardado=None):
        self.guardado = guardado
        self.executados = []
        self.commits = 0

    def execute(self, sql, params=None):
        texto = str(sql)
        self.executados.append((texto, params))
        if 'contas_pagar_nfse_pdf' in texto and texto.strip().startswith('SELECT'):
            return FakeResult(self.guardado)
        return FakeResult(None)

    def commit(self):
        self.commits += 1


@pytest.fixture
def nota(monkeypatch):
    monkeypatch.setattr(rota, '_nfse_completa', lambda *a: {'codigo_verificacao_nfse': '375518929',
                                                           'prestador_inscricao_municipal': '154046'})


def _usuario():
    from types import SimpleNamespace  # noqa: PLC0415
    return SimpleNamespace(id=7, perfil='usuario', telas_permitidas=[])


def test_endpoint_entrega_o_pdf_do_iss_e_guarda_copia(monkeypatch, nota):
    monkeypatch.setattr(rota, 'baixar_pdf_oficial', lambda *a: b'%PDF-oficial')
    session = FakeSession()
    resposta = rota.baixar_pdf_nfse('03.187.913/0001-57', '3199621', _usuario(), session, None)
    assert resposta.body == b'%PDF-oficial'
    assert resposta.headers['x-nfse-origem'] == 'iss'
    assert 'inline; filename="nfse-03187913000157-3199621.pdf"' in resposta.headers['content-disposition']
    gravado = next(p for s, p in session.executados if 'INSERT INTO api_prontocardio.contas_pagar_nfse_pdf' in s)
    assert gravado['arquivo'] == b'%PDF-oficial'
    assert gravado['cnpj'] == '03187913000157'


def test_portal_fora_usa_a_copia(monkeypatch, nota):
    def falha(*a):
        raise svc.PdfIndisponivel('fora')
    monkeypatch.setattr(rota, 'baixar_pdf_oficial', falha)
    resposta = rota.baixar_pdf_nfse('03187913000157', '3199621', _usuario(),
                                    FakeSession({'arquivo': memoryview(b'%PDF-copia'), 'baixado_em': 'ontem'}), None)
    assert resposta.body == b'%PDF-copia'
    assert resposta.headers['x-nfse-origem'] == 'copia'


def test_portal_fora_sem_copia(monkeypatch, nota):
    def falha(*a):
        raise svc.PdfIndisponivel('O portal do ISS Fortaleza não respondeu (ConnectError).')
    monkeypatch.setattr(rota, 'baixar_pdf_oficial', falha)
    with pytest.raises(HTTPException) as erro:
        rota.baixar_pdf_nfse('03187913000157', '3199621', _usuario(), FakeSession(), None)
    assert erro.value.status_code == 502
    assert 'não respondeu' in erro.value.detail
