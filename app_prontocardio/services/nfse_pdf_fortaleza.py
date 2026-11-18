# ruff: noqa: E501
"""PDF oficial (DANFSe) da NFS-e de Fortaleza, pela página pública de consulta do ISS.

A página `pagesPublic/consultarNota.seam` é pública (sem login) e recebe o
número da NFS-e, o código de verificação e a inscrição municipal do
prestador; o PDF vem embutido nela (<object data=...>), num recurso da mesma
sessão HTTP. É o mesmo documento que o portal abre em "Visualizar".
"""

import re
from html import unescape
from urllib.parse import urljoin

import httpx

URL_CONSULTA = 'https://iss.fortaleza.ce.gov.br/grpfor/pagesPublic/consultarNota.seam'
TAMANHO_MAXIMO = 10 * 1024 * 1024
RE_PDF_EMBUTIDO = (
    re.compile(r"""<object[^>]+data=["']([^"']+)["']""", re.I),
    re.compile(r"""<(?:embed|iframe)[^>]+src=["']([^"']+)["']""", re.I),
)
RE_ALFANUMERICO = re.compile(r'^[0-9A-Za-z]{1,30}$')


class PdfIndisponivel(RuntimeError):
    """O portal do ISS não devolveu o PDF da nota."""


def url_publica(numero: str, codigo_verificacao: str, inscricao_prestador: str) -> str:
    return str(httpx.URL(URL_CONSULTA, params={
        'codigo': codigo_verificacao, 'numero': numero, 'consultarNota': 'true', 'chave': inscricao_prestador}))


def baixar_pdf_oficial(numero: str, codigo_verificacao: str, inscricao_prestador: str, *, timeout: float = 20) -> bytes:
    for valor in (numero, codigo_verificacao, inscricao_prestador):
        if not RE_ALFANUMERICO.match(str(valor or '')):
            raise PdfIndisponivel('A NFS-e não tem número, código de verificação ou inscrição do prestador válidos.')
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True, headers={'User-Agent': 'Mozilla/5.0'}) as cliente:
            pagina = cliente.get(url_publica(numero, codigo_verificacao, inscricao_prestador))
            pagina.raise_for_status()
            html = unescape(pagina.text)
            link = next((m.group(1) for r in RE_PDF_EMBUTIDO if (m := r.search(html))), None)
            if link is None:
                raise PdfIndisponivel('O portal do ISS não encontrou a NFS-e com esses dados.')
            resposta = cliente.get(urljoin(str(pagina.url), link))
            resposta.raise_for_status()
    except httpx.HTTPError as exc:
        raise PdfIndisponivel(f'O portal do ISS Fortaleza não respondeu ({type(exc).__name__}).') from exc
    conteudo = resposta.content
    if not conteudo.startswith(b'%PDF') or len(conteudo) > TAMANHO_MAXIMO:
        raise PdfIndisponivel('O portal do ISS não devolveu um PDF válido.')
    return conteudo
