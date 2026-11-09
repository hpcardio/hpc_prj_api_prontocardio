"""Appeal through the payer's own screen when the webservice fails.

Mirrors Extrato > Visualizar > Detalhar > lote > Aceitar Selecionados >
Salvar Recurso > Efetivar. Pages live on the portal; the grids and writes on
the payer's REST host. `accept` and `save` are reversible in the portal;
`efetivar` is not and marks every unappealed item of the whole lot as accepted,
so it only runs at the user's explicit request. No request or response logging.
"""

import html
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
# Columns of the screen's account grid (Contas Médicas), in its order.
# Aceitar/Salvar post the selected accounts with these, as text, plus the
# constant ContaInteira the grid sets; the screen never posts procedure rows
# when an account is selected (recorded on the portal on 2026-11-09).
ACCOUNT_COLUMNS = (
    'numeroCapaLotePrestador',
    'numeroCapaLote',
    'numeroContaPrestador',
    'numeroContaOperadora',
    'numeroConta',
    'seqConta',
    'dataRealizacaoFmt',
    'carteirinhaBeneficiario',
    'nomeBeneficiario',
    'somaValorGlosadoItensConta',
    'quantidadeItensConta',
    'quantidadeItensRecursado',
    'quantidadeItensRecursadoAceito',
    'quantidadeItensFlagAcerto',
    'quantidadeItensFlagDeny',
    'quantidadeItensDispSalvarRecurso',
    'quantidadeItensSalvoRecursado',
    'quantidadeItensSalvoAceito',
    'flagDenyConta',
    'flagAcertoConta',
    'flagDenyGlosa',
    'qtdItemEfetivado',
    'qtdItemDispEfetivar',
)
WHOLE_ACCOUNT = 'ContaInteira'
CURRENCY_COLUMNS = {'somaValorGlosadoItensConta'}
# Columns of the screen's procedures grid (Procedimentos), in its order, read
# from dadosLotesRecursoAberto_gridProcedimentosContas.js on 2026-11-09.
# Selecting procedures instead of an account posts each selected row with
# these, so one item of a guide can be accepted or appealed alone.
PROCEDURE_COLUMNS = (
    'statusItem',
    'itemRecursado',
    'imgSituacaoRecursadoAceito',
    'situacaoRecursadoAceito',
    'imgAglutinador',
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
    'codigoItemDet',
    'codigoItem',
    'descricaoItem',
    'qtdItem',
    'qtdItemAglutinadosFmt',
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
    'valorAcerto',
    'valorRecursado',
    'flagAcertoItem',
    'flagDenyItem',
    'valorRecursados',
    'imgDetalhesRecursoGlosa',
    'imgJustificativaRecurso',
    'protocoloRecurso',
    'efetivado',
    'listaDetalheRecursoGlosa',
    'stringArrayDetalhesRecursoGlosa',
    'detalhesRecursoGlosa',
    'tipoRecurso_ContaInteira_Procedimento',
    'flagDenyGlosa',
    'flagDenyPCMSO',
)
PROCEDURES = 'Procedimentos'
# Icons and HTML summaries the grid draws; no data of their own.
PROCEDURE_DISPLAY = {
    'statusItem',
    'imgSituacaoRecursadoAceito',
    'imgAglutinador',
    'qtdItemAglutinadosFmt',
    'valorRecursados',
    'imgDetalhesRecursoGlosa',
    'imgJustificativaRecurso',
    'stringArrayDetalhesRecursoGlosa',
    'detalhesRecursoGlosa',
}
# jqGrid currency cells, read back as plain decimals.
PROCEDURE_CURRENCY = {'valorDeclarado', 'valorPago', 'valorGlosado'}
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


def account_row(account):
    """The account as the screen reads it from its grid: text, blank null."""
    row = {}
    for column in ACCOUNT_COLUMNS:
        value = account.get(column)
        if value is None:
            row[column] = ''
        elif column in CURRENCY_COLUMNS:
            row[column] = f'{Decimal(str(value)):.2f}'
        else:
            row[column] = str(value)
    row['tipoRecurso_ContaInteira_Procedimento'] = WHOLE_ACCOUNT
    return row


def js_number(value):
    """A number as JavaScript prints it: 0 not 0.0, 1078.18 as is."""
    number = Decimal(str(value or 0))
    return (
        int(number) if number == number.to_integral_value() else float(number)
    )


def js_text(value):
    """A grid cell as text: blank for null, integers without '.0'."""
    if value is None:
        return ''
    if isinstance(value, float):
        return str(js_number(value))
    return str(value)


def procedure_row(item, situation):
    """A selected procedure as the screen reads it from its grid.

    Mirrors getRowDataProcedimentosSelecionados: text cells, valorAcerto as
    a number, valorRecursado '0.0' to accept or the whole denied value to
    appeal, and no detail list.
    """
    row = {}
    for column in PROCEDURE_COLUMNS:
        value = item.get(column)
        if column in PROCEDURE_DISPLAY:
            row[column] = ''
        elif column in PROCEDURE_CURRENCY:
            row[column] = f'{Decimal(str(value or 0)):.2f}'
        else:
            row[column] = js_text(value)
    code = html.escape(js_text(item.get('codigoItem')))
    title = html.escape(
        f' {js_text(item.get("codigoItem"))} - '
        f'{js_text(item.get("descricaoItem"))}'
    )
    row['codigoItemDet'] = f'<div title="{title}">{code}</div>'
    row['listaDetalheRecursoGlosa'] = None
    row['valorAcerto'] = js_number(item.get('valorAcerto'))
    row['valorRecursado'] = (
        '0.0' if situation == ACCEPTED else js_number(item.get('valorGlosado'))
    )
    row['tipoRecurso_ContaInteira_Procedimento'] = PROCEDURES
    return row


def selected_codes(items):
    """codigosSelecionados: the grid's row ids (numeroItem), ';' after each."""
    return ''.join(f'{js_text(i.get("numeroItem"))};' for i in items)


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


def multipart(parts):
    """Body in the given order; a part is (name, value) or a file 4-tuple."""
    boundary = uuid4().hex
    body = bytearray()
    for part in parts:
        if len(part) == 2:  # noqa: PLR2004 - (name, value)
            name, value = part
            body += (
                f'--{boundary}\r\nContent-Disposition: form-data; '
                f'name="{name}"\r\n\r\n{value}\r\n'
            ).encode()
            continue
        name, filename, mime, content = part
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

    def _respond(  # noqa: PLR0913, PLR0917 - one screen action
        self, lot, rows, situation, justification, attachments, codes=''
    ):
        """Post selected grid rows as the screen's responderRecursoComAnexo.

        Order and shape follow the portal: form fields, the file inputs, the
        hidden defaults, then the selected rows as compact JSON objects
        joined by commas (FormData of a JavaScript array). `codes` is the
        procedures grid selection; blank when whole accounts are selected.
        """
        hidden = {
            'mensagemRespostaPrestador': justification,
            'codigosSelecionados': codes,
            **lot,
        }
        records = []
        for row in rows:
            record = dict(row)
            record.update({
                'mensagemRespostaPrestador': justification,
                'codigosSelecionados': codes,
                'situacaoRecursadoAceito': situation,
                **lot,
            })
            records.append(
                json.dumps(record, ensure_ascii=False, separators=(',', ':'))
            )
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
        body, content_type = multipart([
            ('acceptTypePersonalizado', ''),
            ('tipoPermitidoPersonalizado', ''),
            ('pathSaveFileDefault', 'a'),
            ('origem', 'a'),
            *files,
            *hidden.items(),
            ('listaItensSelecionadosRecursoGlosa', ','.join(records)),
        ])
        self._rest(
            'criarRecursoGlosaLoteProvisorio',
            data=body,
            content_type=content_type,
        )

    @staticmethod
    def _account_rows(accounts, situation):
        rows = []
        for account in accounts:
            row = account_row(account)
            if situation == ACCEPTED:
                # Appeals carry no value: the account grid has no such
                # column and the portal appeals the whole denied amount.
                row['valorRecursado'] = '0.0'
            rows.append(row)
        return rows

    def accept_accounts(self, lot, accounts):
        """Aceitar Selecionados of whole accounts; undone until Efetivar."""
        self._respond(
            lot, self._account_rows(accounts, ACCEPTED), ACCEPTED, '', []
        )

    def save_accounts(self, lot, accounts, justification, attachments):
        """Salvar Recurso dos Itens Selecionados for whole accounts."""
        self._respond(
            lot,
            self._account_rows(accounts, APPEALED),
            APPEALED,
            justification,
            attachments,
        )

    def accept_items(self, lot, items):
        """Aceitar Selecionados of some procedures of one account."""
        self._respond(
            lot,
            [procedure_row(i, ACCEPTED) for i in items],
            ACCEPTED,
            '',
            [],
            selected_codes(items),
        )

    def save_items(self, lot, items, justification, attachments):
        """Salvar Recurso of some procedures of one account."""
        self._respond(
            lot,
            [procedure_row(i, APPEALED) for i in items],
            APPEALED,
            justification,
            attachments,
            selected_codes(items),
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


def is_accepted(item):
    return item.get('situacaoRecursadoAceito') == ACCEPTED
