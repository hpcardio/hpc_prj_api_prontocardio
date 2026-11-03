"""Read-only Saúde Petrobras portal: account analysis XML of a lot.

The TISS webservice answers "Nenhuma informação" to the account analysis of
SADT/urgency lots that the portal details. The portal builds the same TISS
4.01 DEMONSTRATIVO_ANALISE_CONTA XML on demand for protocol "<lote>L". Only
that download is used: nothing is sent to the payer here. No request or
response logging; the portal password never leaves this module.
"""

import re
from datetime import datetime
from http.cookiejar import CookieJar
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import (
    HTTPCookieProcessor,
    HTTPRedirectHandler,
    Request,
    build_opener,
)
from zoneinfo import ZoneInfo

from app_prontocardio.services.petrobras_tiss import NS, TissError, parse_xml

PORTAL = 'https://portaltiss.saudepetrobras.com.br'
HOST = urlsplit(PORTAL).hostname
LOGIN = '/saudeweb/seguranca/login'
OPERADORA = '44'  # Saúde Petrobras in the portal operator list.
PRESTADOR = '/conectividade/prestador/'
ANALYSIS = PRESTADOR + 'demonstrativo/downloadXMLDemonstrativoAnaliseConta.htm'
LOGOUT = '/conectividade/j_spring_cas_security_logout'
ANALYSIS_TYPE = 'DEMONSTRATIVO_ANALISE_CONTA'
# Credentials the portal embeds in the XML; dropped before anything is kept.
SECRET_TAGS = ('loginSenhaPrestador',)
LOT = re.compile(r'^\d{1,11}$')


class SameHost(HTTPRedirectHandler):
    """Login redirects are allowed only inside the portal over TLS."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: PLR0913 - urllib override signature
        target = urlsplit(newurl)
        if target.scheme != 'https' or target.hostname != HOST:
            raise TissError('Redirecionamento do portal recusado.')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def sanitize(raw):
    """Parse safely, check the transaction and drop embedded credentials."""
    root = parse_xml(raw)
    kind = root.find('.//{%s}tipoTransacao' % NS)
    if root.tag != '{%s}mensagemTISS' % NS or (
        kind is None or kind.text != ANALYSIS_TYPE
    ):
        raise TissError('Portal não retornou o demonstrativo de análise.')
    for tag in SECRET_TAGS:
        for node in list(root.iter('{%s}%s' % (NS, tag))):
            node.getparent().remove(node)
    return root


class PortalClient:
    def __init__(self, config, opener=None):
        config.require_portal()
        self.config = config
        self.opener = opener or build_opener(
            HTTPCookieProcessor(CookieJar()), SameHost()
        )
        self.logged = False

    def _open(self, path, data=None):
        request = Request(
            PORTAL + path,
            urlencode(data).encode() if data is not None else None,
            headers={'User-Agent': 'Mozilla/5.0'},
        )
        try:
            with self.opener.open(
                request, timeout=self.config.timeout_seconds
            ) as response:
                return response.geturl(), response.read()
        except HTTPError as exc:
            raise TissError(
                f'Portal Petrobras retornou HTTP {exc.code}.'
            ) from None
        except (URLError, TimeoutError, OSError):
            raise TissError('Falha de comunicação com o portal.') from None

    def login(self):
        _, page = self._open(LOGIN)
        page = page.decode('latin-1')
        action = re.search(r'id="formPrestador"[^>]*action="([^"]+)"', page)
        ticket = re.search(r'name="lt" value="([^"]+)"', page)
        if not action or not ticket or not action.group(1).startswith('/'):
            raise TissError('Tela de login do portal não reconhecida.')
        url, _ = self._open(
            action.group(1),
            {
                'origemCas': 'Petrobras',
                'lt': ticket.group(1),
                '_eventId': 'submit',
                'username': self.config.portal_login,
                'password': self.config.portal_password.get_secret_value(),
            },
        )
        if PRESTADOR not in urlsplit(url).path:
            raise TissError('Login no portal Petrobras recusado.')
        self._open(
            PRESTADOR
            + 'trocaroperadora.htm?'
            + urlencode({'codigoOperadora': OPERADORA, 'contr': ''})
        )
        self.logged = True

    def analysis(self, lote):
        """Sanitized analysis XML of one lot (portal protocol "<lote>L")."""
        if not LOT.match(str(lote)):
            raise TissError('Número de lote inválido para o portal.')
        if not self.logged:
            self.login()
        today = datetime.now(ZoneInfo('America/Sao_Paulo'))
        _, raw = self._open(
            ANALYSIS,
            {
                'periodo': '90',
                'codigoEmpresa': self.config.prestador_cnpj,
                'dataTransacao': today.strftime('%d/%m/%Y'),
                'numeroProtocolo': f'{lote}L',
            },
        )
        if not raw.lstrip().startswith(b'<?xml'):
            # An unknown lot comes back as the portal home page.
            raise TissError('Portal sem demonstrativo de análise do lote.')
        return sanitize(raw)

    def logout(self):
        if self.logged:
            try:
                self._open(LOGOUT)
            except TissError:
                pass
            self.logged = False
