"""Appeal through the payer's own screen when the webservice fails.

Mirrors Extrato > Visualizar > Detalhar > lote > Aceitar Selecionados >
Salvar Recurso > Efetivar. Pages live on the portal; the grids and writes on
the payer's REST host. `accept` and `save` are reversible in the portal;
`efetivar` is not and marks every unappealed item of the whole lot as accepted,
so it only runs at the user's explicit request. No request or response logging.
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
ACCEPTED = 'A'
ERROR_TEXT_LIMIT = 200
# Columns of the screen's procedure grid that come straight from the JSON.
# getRowData posts them as text, and only these, whatever the JSON carries.
GRID_COLUMNS = (
    'statusItem',
    'itemRecursado',
    'aglutinador',
    'idRecursoGlosaProvisorio',
    'numeroConta',
    'numeroContaPrestador',
    'numeroContaOperadora',
    'controle',
    'numeroCapaLote',
    'numeroCapaLotePrestador',
    'valorTotalProcessado',
    'valorTotalPago',
    'valorTotalGlosado',
    'valorTotalAcerto',
    'numeroItemPrincipal',
    'numeroItem',
    'codigoItem',
    'descricaoItem',
    'qtdItem',
    'qtdItemAglutinados',
    'qtdItemAglutinadosGlosa',
    'qtdItemAglutinadoSalvoR',
    'qtdItemAglutinadoSalvoA',
    'qtdItemAglutinadoDispSalvar',
    'dataCronogramaLote',
    'dataRealizacao',
    'dataRealizacaoStr',
    'codigoBeneficiario',
    'carteirinhaBeneficiario',
    'nomeBeneficiario',
    'valorDeclarado',
    'valorPago',
    'valorGlosado',
    'flagAcertoItem',
    'flagDenyItem',
    'protocoloRecurso',
    'efetivado',
    'flagDenyGlosa',
    'flagDenyPCMSO',
)
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


def grid_row(item):
    """The row as the screen reads it from its grid: text, blank for null."""
    return {
        column: '' if item[column] is None else str(item[column])
        for column in GRID_COLUMNS
        if column in item
    }


def error_text(exc):
    """Short reason from an error body, kept out of request logging."""
    try:
        raw = exc.read(MAX_RESPONSE).decode('utf-8', 'replace')
    except (OSError, ValueError):
        return ''
    try:
        data = json.loads(raw)
    except ValueError:
        data = None
    if isinstance(data, dict):
        for key in (
            'mensagemRetorno',
            'errorMessageClean',
            'errorMessage',
            'message',
        ):
            if data.get(key):
                return str(data[key])[:ERROR_TEXT_LIMIT]
    text = re.sub(r'<(script|style).*?</\1>|<[^>]+>', ' ', raw, flags=re.S)
    return ' '.join(text.split())[:ERROR_TEXT_LIMIT]


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
            detail = error_text(exc)
            raise TissError(
                f'Portal Petrobras: HTTP {exc.code}'
                + (f' ({detail}).' if detail else '.')
            ) from None
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

    def _respond(self, lot, item, situation, justification, attachments):  # noqa: PLR0913, PLR0917 - one screen action
        """Post one item as the screen's responderRecursoComAnexo does."""
        hidden = {
            'mensagemRespostaPrestador': justification,
            'codigosSelecionados': f'{item["numeroItem"]};',
            **lot,
        }
        row = {
            **grid_row(item),
            'listaDetalheRecursoGlosa': None,
            'valorAcerto': item.get('valorAcerto') or 0,
            'valorRecursado': (
                item['valorGlosado'] if situation == APPEALED else '0.0'
            ),
            'situacaoRecursadoAceito': situation,
            **hidden,
        }
        fields = [
            ('acceptTypePersonalizado', ''),
            ('tipoPermitidoPersonalizado', ''),
            ('pathSaveFileDefault', 'a'),
            ('origem', 'a'),
            *hidden.items(),
            ('listaItensSelecionadosRecursoGlosa', json.dumps(row)),
        ]
        files = [
            (f'listaDocumentoAnexo[{i}].file', a.nome, a.mime, a.conteudo)
            for i, a in enumerate(attachments)
        ] or [
            (
                'listaDocumentoAnexo[0].file',
                '',
                'application/octet-stream',
                b'',
            )
        ]
        body, content_type = multipart(fields, files)
        self._rest(
            'criarRecursoGlosaLoteProvisorio',
            data=body,
            content_type=content_type,
        )

    def accept(self, lot, item):
        """Aceitar Selecionados of one item; reversible until Efetivar."""
        self._respond(lot, item, ACCEPTED, '', [])

    def save(self, lot, item, justification, attachments):
        """Salvar Recurso of one item, as the screen posts it."""
        self._respond(lot, item, APPEALED, justification, attachments)

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


def is_accepted(item):
    return item.get('situacaoRecursadoAceito') == ACCEPTED
