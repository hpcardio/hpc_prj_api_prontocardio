"""Appeal through the payer's own screen when the webservice fails.

Mirrors Extrato > Visualizar > Detalhar > lote > Salvar Recurso > Efetivar.
Pages live on the portal; the grids and writes on the payer's REST host. Only
two calls write: `save` is reversible in the portal, `efetivar` is not and
marks every unappealed item of the whole lot as accepted, so the caller must
check the lot first. No request or response logging.
"""

import json
import re
from decimal import Decimal
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, build_opener
from uuid import uuid4

from app_prontocardio.services.petrobras_portal import (
    PRESTADOR,
    PortalClient,
)
from app_prontocardio.services.petrobras_tiss import NoRedirect, TissError

REST = (
    'https://wwwt.connectmed.com.br/Connectmed/extratoItensRecursoGlosaLote/'
)
EXTRATO = PRESTADOR + 'extrato/'
PERIOD = '360'  # Longest finite period offered by the screen.
MAX_ROWS = 500
MAX_RESPONSE = 5 * 1024 * 1024
MIN_JUSTIFICATION = 20  # Enforced by the portal screen.
DOCUMENT_TYPES = {'application/pdf', 'image/jpeg', 'image/bmp'}
DEFAULTS_ID = 'dadosLotesRecursoAberto_defaultValues'
APPEALED = 'R'
DENIED = 'S'


def form_fields(html):
    """name -> value; some portal inputs repeat value="" after the real one."""
    fields = {}
    for tag in re.findall(r'<input[^>]*>', html):
        name = re.search(r'name=["\']([^"\']+)', tag)
        if name:
            values = re.findall(r'value=["\']([^"\']*)', tag)
            fields[name.group(1)] = next((v for v in values if v), '')
    return fields


def lot_forms(html):
    """{capa de lote: fields of its detail form}."""
    forms = {}
    for capa in re.findall(r'id="formularioBuscarLote_(\d+)"', html):
        start = html.index(f'id="formularioBuscarLote_{capa}"')
        forms[capa] = form_fields(html[start : html.index('</form>', start)])
    return forms


def multipart(fields, files):
    boundary = uuid4().hex
    body = bytearray()
    for name, value in fields:
        body += (
            f'--{boundary}\r\nContent-Disposition: form-data; '
            f'name="{name}"\r\n\r\n{value}\r\n'
        ).encode()
    for name, filename, mime, content in files:
        safe = re.sub(r'[^\w.-]', '_', filename, flags=re.ASCII)
        body += (
            f'--{boundary}\r\nContent-Disposition: form-data; '
            f'name="{name}"; filename="{safe}"\r\n'
            f'Content-Type: {mime}\r\n\r\n'
        ).encode()
        body += bytes(content) + b'\r\n'
    body += f'--{boundary}--\r\n'.encode()
    return bytes(body), f'multipart/form-data; boundary={boundary}'


class PortalAppeal:
    def __init__(self, config, portal=None, opener=None):
        config.require_portal()
        self.config = config
        self.portal = portal or PortalClient(config)
        self.opener = opener or build_opener(NoRedirect())

    def close(self):
        self.portal.logout()

    def _rest(self, path, query=None, data=None, content_type=None):
        url = REST + path + (f'?{urlencode(query)}' if query else '')
        headers = {'User-Agent': 'Mozilla/5.0', 'Accept': 'application/json'}
        if content_type:
            headers['Content-Type'] = content_type
        try:
            with self.opener.open(
                Request(url, data, headers),
                timeout=self.config.timeout_seconds,
            ) as response:
                raw = response.read(MAX_RESPONSE + 1)
        except HTTPError as exc:
            raise TissError(f'Portal Petrobras: HTTP {exc.code}.') from None
        except (URLError, TimeoutError, OSError):
            raise TissError('Falha de comunicação com o portal.') from None
        return raw

    def _list(self, path, lot, **extra):
        raw = self._rest(
            path,
            {
                **lot,
                'statusRecurso': '',
                '_search': 'false',
                'rows': str(MAX_ROWS),
                'page': '1',
                'sidx': '',
                'sord': 'asc',
                **extra,
            },
        )
        try:
            data = json.loads(raw)
            rows = data['rows']
        except (ValueError, KeyError, TypeError):
            raise TissError('Resposta inesperada do portal.') from None
        if int(data.get('records') or 0) > len(rows):
            raise TissError('Lote grande demais para o recurso automático.')
        return rows

    def lots(self, demonstrativo):
        """Yield the default values of each lot of a demonstrativo."""
        if not self.portal.logged:
            self.portal.login()
        self.portal._open(
            EXTRATO + 'extratoPorPeriodo.htm', {'periodo': PERIOD}
        )
        _, page = self.portal._open(
            EXTRATO + 'detalhePagamento.htm',
            {'controle': demonstrativo, 'periodo': PERIOD},
        )
        millis = re.search(
            r"dataPagamentoMillis' value='\" \+ (\d+)", page.decode('latin-1')
        )
        _, page = self.portal._open(
            EXTRATO + 'listarLotesPagamento.htm',
            {
                'codigoContrato': self.config.prestador_cnpj,
                'codigoEmpresa': '1',
                'controle': demonstrativo,
                'periodo': PERIOD,
            },
        )
        for fields in lot_forms(page.decode('latin-1')).values():
            if millis:
                fields['dataPagamentoMillis'] = millis.group(1)
            _, detail = self.portal._open(
                EXTRATO + 'buscarLoteNovo.htm', fields
            )
            html = detail.decode('latin-1')
            start = html.find(f'id="{DEFAULTS_ID}"')
            if start < 0:
                raise TissError('Tela do lote do portal não reconhecida.')
            yield form_fields(html[start : html.index('</div>', start)])

    def accounts(self, lot):
        return self._list('listarContasLoteRecurso/', lot)

    def items(self, lot, account):
        return self._list(
            'listarItensContaRecurso/',
            lot,
            numeroCapaLote=account['numeroCapaLote'],
            numeroConta=account['numeroConta'],
        )

    def save(self, lot, item, justification, attachments):
        """Salvar Recurso of one item, as the screen posts it."""
        code = f'{item["numeroItem"]};'
        complement = {
            'mensagemRespostaPrestador': justification,
            'codigosSelecionados': code,
            'situacaoRecursadoAceito': APPEALED,
            **lot,
        }
        row = {
            **item,
            'listaDetalheRecursoGlosa': None,
            'valorAcerto': item.get('valorAcerto') or 0,
            'valorRecursado': item['valorGlosado'],
            **complement,
        }
        fields = [
            ('acceptTypePersonalizado', ''),
            ('tipoPermitidoPersonalizado', ''),
            ('pathSaveFileDefault', 'a'),
            ('origem', 'a'),
            *((k, v) for k, v in complement.items()),
            ('listaItensSelecionadosRecursoGlosa', json.dumps(row)),
        ]
        files = [
            (f'listaDocumentoAnexo[{i}].file', a.nome, a.mime, a.conteudo)
            for i, a in enumerate(attachments)
        ]
        body, content_type = multipart(fields, files)
        self._rest(
            'criarRecursoGlosaLoteProvisorio',
            data=body,
            content_type=content_type,
        )

    def efetivar(self, lot):
        """Irreversible: every unappealed item of the lot becomes accepted."""
        raw = self._rest(
            'efetivarItensSalvos',
            data=json.dumps({'jsonParam': json.dumps(lot)}).encode(),
            content_type='application/json; charset=utf-8',
        )
        try:
            return json.loads(raw)
        except ValueError:
            return {}


def same_value(item, value):
    return Decimal(str(item.get('valorGlosado') or 0)) == Decimal(
        str(value or 0)
    )


def is_denied(item):
    return item.get('flagDenyItem') == DENIED


def is_saved(item):
    return item.get('situacaoRecursadoAceito') == APPEALED
