"""SOAP 1.1 client for the Petrobras 4.01.00 published contracts.

No request/response logging, no redirects, no automatic write retries.
"""

import base64
import hashlib
import hmac
import json
from copy import deepcopy
from datetime import datetime
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import uuid4
from zoneinfo import ZoneInfo

from lxml import etree as ET

NS = 'http://www.ans.gov.br/padroes/tiss/schemas'
SOAP = 'http://schemas.xmlsoap.org/soap/envelope/'
CONTRACTS = Path(__file__).with_name('petrobras_contracts')
MAX_PROTOCOLS = 30
PROTOCOL_LENGTH = 12
COMPETENCE_LENGTH = 6
MONTHS_PER_YEAR = 12
MAX_RESPONSE = 20 * 1024 * 1024
OPERATIONS = {
    'consulta': (
        'tissSolicitacaoDemonstrativoRetorno',
        'solicitacaoDemonstrativoRetornoWS',
        'SOLIC_DEMONSTRATIVO_RETORNO',
        'demonstrativoRetornoWS',
    ),
    'recurso': (
        'tissRecursoGlosa',
        'loteRecursoGlosaWS',
        'RECURSO_GLOSA',
        'protocoloRecebimentoRecursoWS',
    ),
    'status': (
        'tissSolicitacaoStatusRecursoGlosa',
        'solicitacaoStatusRecursoGlosaWS',
        'SOLIC_STATUS_RECURSO_GLOSA',
        'situacaoProtocoloRecursoWS',
    ),
    'documento': (
        'tissEnvioDocumentos',
        'envioDocumentoWS',
        'ENVIO_DOCUMENTO',
        'reciboDocumentosWS',
    ),
}


class TissError(ValueError):
    """Safe message only; never include an HTTP body or credentials."""


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: PLR0913 - urllib override signature
        raise TissError('Redirecionamento do serviço recusado.')


def element(parent, name, value=None):
    child = ET.SubElement(parent, '{%s}%s' % (NS, name))
    if value is not None:
        child.text = str(value)
    return child


def value(node, path):
    found = node.find(
        '/'.join('{%s}%s' % (NS, part) for part in path.split('/'))
    )
    return found.text.strip() if found is not None and found.text else None


def children(node, name):
    return node.findall('{%s}%s' % (NS, name))


def parse_xml(raw):
    if (
        len(raw) > MAX_RESPONSE
        or b'<!DOCTYPE' in raw.upper()
        or b'<!ENTITY' in raw.upper()
    ):
        raise TissError(
            'XML excede o limite ou contém declarações não permitidas.'
        )
    try:
        return ET.fromstring(
            raw,
            parser=ET.XMLParser(
                resolve_entities=False, no_network=True, load_dtd=False
            ),
        )
    except ET.XMLSyntaxError:
        raise TissError('Resposta XML inválida.') from None


@lru_cache(maxsize=1)
def schema():
    parser = ET.XMLParser(resolve_entities=False, no_network=True)
    return ET.XMLSchema(
        ET.parse(str(CONTRACTS / 'tissWebServicesV4_01_00.xsd'), parser)
    )


XSD = 'http://www.w3.org/2001/XMLSchema'


class ReceiptResolver(ET.Resolver):
    """Receive numeric denial reasons; keep the send schema unchanged."""

    def resolve(self, url, public_id, context):
        if not url.endswith('tissSimpleTypesV4_01_00.xsd'):
            return None
        document = ET.parse(str(CONTRACTS / 'tissSimpleTypesV4_01_00.xsd'))
        restriction = document.find(
            f'{{{XSD}}}simpleType[@name="dm_tipoGlosa"]/{{{XSD}}}restriction'
        )
        for child in list(restriction):
            restriction.remove(child)
        ET.SubElement(restriction, f'{{{XSD}}}length', value='4')
        ET.SubElement(restriction, f'{{{XSD}}}pattern', value='[0-9]{4}')
        return self.resolve_string(ET.tostring(document), context)


@lru_cache(maxsize=1)
def receipt_schema():
    parser = ET.XMLParser(resolve_entities=False, no_network=True)
    parser.resolvers.add(ReceiptResolver())
    return ET.XMLSchema(
        ET.parse(str(CONTRACTS / 'tissWebServicesV4_01_00.xsd'), parser)
    )


@lru_cache(maxsize=1)
def known_denial_codes():
    document = ET.parse(str(CONTRACTS / 'tissSimpleTypesV4_01_00.xsd'))
    return frozenset(
        document.xpath(
            'xs:simpleType[@name="dm_tipoGlosa"]/xs:restriction/'
            'xs:enumeration/@value',
            namespaces={'xs': XSD},
        )
    )


def validate(root):
    if not schema().validate(root):
        raise TissError('Mensagem incompatível com o contrato TISS 4.01.00.')


def text_hash(root):
    # TISS concatenates text values in document order (ISO-8859-1).
    contents = ''.join(n.text or '' for n in root.iter() if len(n) == 0)
    try:
        return hashlib.md5(
            contents.encode('iso-8859-1'), usedforsecurity=False
        ).hexdigest()
    except UnicodeEncodeError:
        raise TissError(
            'Texto contém caracteres incompatíveis com TISS.'
        ) from None


def message(config, operation):
    config.require_connection()
    _, root_name, kind, _ = OPERATIONS[operation]
    root = ET.Element('{%s}%s' % (NS, root_name), nsmap={'ans': NS})
    head = element(root, 'cabecalho')
    trans = element(head, 'identificacaoTransacao')
    now = datetime.now(ZoneInfo('America/Sao_Paulo'))
    element(trans, 'tipoTransacao', kind)
    element(trans, 'sequencialTransacao', str(uuid4().int % 10**12).zfill(12))
    element(trans, 'dataRegistroTransacao', now.date().isoformat())
    element(trans, 'horaRegistroTransacao', now.strftime('%H:%M:%S'))
    element(
        element(element(head, 'origem'), 'identificacaoPrestador'),
        'codigoPrestadorNaOperadora',
        config.identificador_prestador,
    )
    element(element(head, 'destino'), 'registroANS', config.registro_ans)
    element(head, 'Padrao', '4.01.00')
    login = element(head, 'loginSenhaPrestador')
    element(login, 'loginPrestador', config.login.get_secret_value())
    element(
        login,
        'senhaPrestador',
        hashlib.md5(
            config.password.get_secret_value().encode('iso-8859-1'),
            usedforsecurity=False,
        ).hexdigest(),
    )
    return root


def contracted(parent, config):
    element(
        parent, 'codigoPrestadorNaOperadora', config.identificador_prestador
    )


class PetrobrasClient:
    def __init__(self, config, opener=None, dry_run=False):
        self.config = config
        self.response_warnings = []
        self.dry_run = dry_run
        self.opener = opener or build_opener(NoRedirect())

    def call(self, operation, root):  # noqa: PLR0912 - validate all SOAP trust boundaries
        if (
            operation in {'recurso', 'documento'}
            and not self.config.transmission_enabled
        ):
            raise TissError(
                (
                    'Transmissão Petrobras ainda não '
                    'habilitada para este ambiente.'
                )
            )
        element(root, 'hash', text_hash(root))
        validate(root)
        if self.dry_run:
            return root
        envelope = ET.Element('{%s}Envelope' % SOAP, nsmap={'soap': SOAP})
        ET.SubElement(envelope, '{%s}Body' % SOAP).append(root)
        service, _, _, expected = OPERATIONS[operation]
        # Published WSDL declares HTTP:80. Only the same service over TLS is
        # allowed.
        url = 'https://webservices.crc.com.br/tiss/ws/40100/' + service
        request = Request(
            url,
            ET.tostring(envelope, encoding='iso-8859-1', xml_declaration=True),
            headers={
                'Content-Type': 'text/xml; charset=iso-8859-1',
                'SOAPAction': '""',
            },
            method='POST',
        )
        try:
            with self.opener.open(
                request, timeout=self.config.timeout_seconds
            ) as response:
                raw = response.read(MAX_RESPONSE + 1)
        except HTTPError as exc:
            # SOAP faults can use HTTP 500. Expose only known safe fault codes.
            try:
                fault = parse_xml(exc.read(32768))
                code = next(
                    (
                        n.text
                        for n in fault.iter()
                        if n.tag.split('}')[-1] == 'faultstring'
                    ),
                    '',
                )
            except TissError:
                code = ''
            if code in {
                'DESTINATARIO_INVALIDO',
                'LOGIN_INVALIDO',
                'SENHA_INVALIDA',
                'HASH_INVALIDO',
            }:
                raise TissError('Petrobras: ' + code) from None
            raise TissError(
                (
                    f'Serviço Petrobras retornou HTTP '
                    f'{exc.code}; dados anteriores preservados.'
                )
            ) from None
        except (URLError, TimeoutError, OSError):
            raise TissError(
                (
                    'Falha de comunicação Petrobras; '
                    'confirme a situação antes de reenviar.'
                )
            ) from None
        document = parse_xml(raw)
        body = document.find('{%s}Body' % SOAP)
        if (
            body is None
            or len(body) != 1
            or body[0].tag != '{%s}%s' % (NS, expected)
        ):
            raise TissError(
                'Resposta SOAP inesperada ou falha informada pela operadora.'
            )
        result = body[0]
        error = result.find('.//{%s}mensagemErro' % NS)
        if (
            error is not None
            and 'NENHUMA INFORMA'
            in (value(error, 'descricaoGlosa') or '').upper()
        ):
            raise TissError(
                (
                    'Nenhuma informação de pagamento encontrada '
                    'para os parâmetros consultados.'
                )
            )
        if error is not None or value(result, 'cabecalho/falhaNegocio'):
            raise TissError(
                (
                    'Operadora recusou a consulta/transação; '
                    'verificar cadastro, credenciais e protocolo.'
                )
            )
        received_hash = value(result, 'hash')

        checked = deepcopy(result)
        for child in list(checked):
            if child.tag.split('}')[-1] in {'hash', 'Signature', 'Signatures'}:
                checked.remove(child)

        if not received_hash or not hmac.compare_digest(
            received_hash.lower(), text_hash(checked)
        ):
            raise TissError(
                (
                    'Integridade do retorno TISS não '
                    'confirmada; dados anteriores preservados.'
                )
            )
        validate_response_identity(result, self.config)
        self.response_warnings = validate_response_schema(result, self.config)
        return result

    def demonstrativos(self, protocols):
        if not 1 <= len(protocols) <= MAX_PROTOCOLS or any(
            not p or len(p) > PROTOCOL_LENGTH for p in protocols
        ):
            raise TissError(
                'Informe de 1 a 30 protocolos, com até 12 caracteres.'
            )
        root = message(self.config, 'consulta')
        analysis = element(
            element(root, 'solicitacaoDemonstrativoRetorno'),
            'demonstrativoAnalise',
        )
        contracted(element(analysis, 'dadosPrestador'), self.config)
        element(
            analysis,
            'dataSolicitacao',
            datetime.now(ZoneInfo('America/Sao_Paulo')).date().isoformat(),
        )
        group = element(analysis, 'protocolos')
        for protocol in protocols:
            element(group, 'numeroProtocolo', protocol)
        return self.call('consulta', root)

    def pagamentos(self, competencia):
        if (
            len(competencia) != COMPETENCE_LENGTH
            or not competencia.isdigit()
            or not 1 <= int(competencia[4:]) <= MONTHS_PER_YEAR
        ):
            raise TissError('Competência inválida; informe AAAAMM.')
        root = message(self.config, 'consulta')
        body = element(
            element(root, 'solicitacaoDemonstrativoRetorno'),
            'demonstrativoPagamento',
        )
        contracted(element(body, 'dadosPrestador'), self.config)
        element(
            body,
            'dataSolicitacao',
            datetime.now(ZoneInfo('America/Sao_Paulo')).date().isoformat(),
        )
        element(body, 'tipoDemonstrativo', '1')
        element(element(body, 'periodo'), 'competencia', competencia)
        return self.call('consulta', root)

    def status(self, protocol):
        root = message(self.config, 'status')
        body = element(root, 'solicitacaoStatusProtocoloRecurso')
        contracted(element(body, 'dadosPrestador'), self.config)
        element(body, 'numeroProtocolo', protocol)
        return self.call('status', root)

    def appeal(self, source, justification, appeal_id):
        if source.get('divergencia_totais'):
            raise TissError(
                'Totais divergentes na fonte; revisar o demonstrativo '
                'antes de transmitir recurso.'
            )
        if source.get('escopo') != 'item' or not source.get('motivos'):
            raise TissError(
                (
                    'Recurso automático disponível '
                    'apenas para itens discriminados.'
                )
            )
        root = message(self.config, 'recurso')
        guide = element(element(root, 'loteRecurso'), 'guiaRecursoGlosa')
        element(guide, 'registroANS', self.config.registro_ans)
        element(guide, 'numeroGuiaRecGlosaPrestador', appeal_id)
        element(guide, 'nomeOperadora', 'SAUDE PETROBRAS')
        element(guide, 'objetoRecurso', '2')
        contracted(element(guide, 'dadosContratado'), self.config)
        element(guide, 'numeroLote', source['lote'])
        element(guide, 'numeroProtocolo', source['protocolo'])
        origin = element(element(guide, 'opcaoRecurso'), 'recursoGuia')
        element(origin, 'numeroGuiaOrigem', source['guia_prestador'])
        if source.get('guia_operadora'):
            element(origin, 'numeroGuiaOperadora', source['guia_operadora'])
        items = element(origin, 'opcaoRecursoGuia')
        for reason in source['motivos']:
            item = element(items, 'itensGuia')
            element(item, 'sequencialItem', source['sequencial'])
            element(item, 'dataInicio', source['data_realizacao'])
            proc = element(item, 'procRecurso')
            element(proc, 'codigoTabela', source['tabela'])
            element(proc, 'codigoProcedimento', source['tuss'])
            element(proc, 'descricaoProcedimento', source['descricao'])
            element(item, 'codGlosaItem', reason['codigo'])
            element(item, 'valorRecursado', reason['valor'])
            element(item, 'justificativaItem', justification)
        element(guide, 'valorTotalRecursado', source['valor_glosa'])
        element(
            guide,
            'dataRecurso',
            datetime.now(ZoneInfo('America/Sao_Paulo')).date().isoformat(),
        )
        return self.call('recurso', root)

    def document(self, source, attachment):
        root = message(self.config, 'documento')
        body = element(
            root, 'envioDOcumento'
        )  # Exact spelling from published XSD.
        for key, tag in [
            ('lote', 'numeroLote'),
            ('protocolo', 'numeroProtocolo'),
            ('guia_prestador', 'numeroGuiaPrestador'),
            ('guia_operadora', 'numeroGuiaOperadora'),
        ]:
            if source.get(key):
                element(body, tag, source[key])
        element(body, 'numeroDocumento', str(attachment.id))
        element(body, 'naturezaGuia', attachment.natureza_guia)
        element(body, 'formatoDocumento', attachment.formato)
        if source.get('sequencial'):
            element(body, 'seqReferenciaItem', source['sequencial'])
        element(
            body,
            'documento',
            base64.b64encode(attachment.conteudo).decode('ascii'),
        )
        element(body, 'tipoDocumento', attachment.tipo_documento)
        return self.call('documento', root)


def parse_demonstrativos(root, prestador, registro_ans):  # noqa: PLR0912, PLR0915 - preserve all source balance levels
    """Import explicit details; retain wider totals without allocating them."""
    rows, scopes = [], set()
    for demo in root.findall('.//{%s}demonstrativoAnaliseConta' % NS):
        if value(demo, 'cabecalhoDemonstrativo/registroANS') != registro_ans:
            raise TissError('Demonstrativo pertence a outra operadora.')
        cnpj = value(demo, 'dadosPrestador/dadosContratado/cnpjContratado')
        if cnpj and cnpj != prestador:
            raise TissError('Demonstrativo de outro prestador recusado.')
        demo_id = value(demo, 'cabecalhoDemonstrativo/numeroDemonstrativo')
        issued = value(demo, 'cabecalhoDemonstrativo/dataEmissao')
        demo_start = len(rows)
        demo_warnings = []
        for protocol in demo.findall('.//{%s}dadosProtocolo' % NS):
            number = value(protocol, 'numeroProtocolo')
            if not number or set(number) == {'0'}:
                raise TissError(
                    'Análise sem protocolo válido; preservar o lote para '
                    'revisão. Não é possível vincular '
                    'pela consulta de protocolo.'
                )
            scopes.add((number, demo_id))
            protocol_start = len(rows)
            protocol_warnings = []
            common = {
                'prestador': prestador,
                'demonstrativo': demo_id,
                'protocolo': number,
                'lote': value(protocol, 'numeroLotePrestador'),
                'data_emissao': issued,
                'total_glosa_demonstrativo': value(demo, 'valorGlosaGeral'),
                'total_glosa_protocolo': value(
                    protocol, 'valorGlosaProtocolo'
                ),
                'cobertura': 'parcial_nao_confirmada',
            }
            guide_total = Decimal(0)
            for guide in children(protocol, 'relacaoGuias'):
                info = {
                    **common,
                    'guia_prestador': value(guide, 'numeroGuiaPrestador'),
                    'guia_operadora': value(guide, 'numeroGuiaOperadora'),
                    'carteira': value(guide, 'numeroCarteira'),
                    'total_glosa_guia': value(guide, 'valorGlosaGuia'),
                }
                guide_start = len(rows)
                item_total = Decimal(0)
                seen = set()
                for detail in children(guide, 'detalhesGuia'):
                    seq = value(detail, 'sequencialItem')
                    if seq in seen:
                        raise TissError(
                            (
                                'Sequência de item duplicada na '
                                'guia; importação interrompida.'
                            )
                        )
                    seen.add(seq)
                    reasons = [
                        {
                            'codigo': value(g, 'tipoGlosa'),
                            'valor': value(g, 'valorGlosa'),
                        }
                        for g in children(detail, 'relacaoGlosa')
                    ]
                    total = sum(
                        (Decimal(r['valor']) for r in reasons), Decimal(0)
                    )
                    item_total += total
                    row = {
                        **info,
                        'escopo': 'item',
                        'sequencial': seq,
                        'tuss': value(
                            detail, 'procedimento/codigoProcedimento'
                        ),
                        'tabela': value(detail, 'procedimento/codigoTabela'),
                        'descricao': value(
                            detail, 'procedimento/descricaoProcedimento'
                        ),
                        'data_realizacao': value(detail, 'dataRealizacao'),
                        'quantidade': value(detail, 'qtdExecutada'),
                        'valor_informado': value(detail, 'valorInformado'),
                        'valor_liberado': value(detail, 'valorLiberado'),
                        'motivos': reasons,
                        'valor_glosa': str(total),
                    }
                    unknown = sorted({
                        r['codigo']
                        for r in reasons
                        if r['codigo'] not in known_denial_codes()
                    })
                    if unknown:
                        row.setdefault('avisos', []).append(
                            'Código de glosa fora do domínio TISS 4.01: '
                            + ', '.join(unknown)
                            + '. Revisão necessária.'
                        )
                    rows.append(row)
                declared = value(guide, 'valorGlosaGuia')
                total_guide = (
                    Decimal(declared) if declared is not None else item_total
                )
                if total_guide < item_total:
                    for row in rows[guide_start:]:
                        row.setdefault('avisos', []).append(
                            'Total da guia inferior à soma dos itens; '
                            'valores originais preservados para revisão.'
                        )
                        row['divergencia_totais'] = True
                guide_total += total_guide
                if total_guide > item_total or children(
                    guide, 'motivoGlosaGuia'
                ):
                    rows.append({
                        **info,
                        'escopo': 'guia',
                        'sequencial': '',
                        'tuss': None,
                        'valor_glosa': str(
                            max(total_guide - item_total, Decimal(0))
                        ),
                        'motivos': [
                            {
                                'codigo': value(m, 'codigoGlosa'),
                                'descricao': value(m, 'descricaoGlosa'),
                            }
                            for m in children(guide, 'motivoGlosaGuia')
                        ],
                    })
            declared = value(protocol, 'valorGlosaProtocolo')
            protocol_total = (
                Decimal(declared) if declared is not None else guide_total
            )
            if protocol_total < guide_total:
                protocol_warnings.append(
                    'Total de glosa do protocolo inferior às guias retornadas.'
                )
            for row in rows[protocol_start:]:
                if protocol_warnings:
                    row.setdefault('avisos', []).extend(protocol_warnings)
                    row['divergencia_totais'] = True
        declared_total = value(demo, 'valorGlosaGeral')
        calculated = sum(
            (Decimal(row['valor_glosa']) for row in rows[demo_start:]),
            Decimal(0),
        )
        if declared_total is not None and Decimal(declared_total) < calculated:
            demo_warnings.append(
                'Total geral da glosa inferior aos detalhes retornados.'
            )
        for row in rows[demo_start:]:
            if demo_warnings:
                row.setdefault('avisos', []).extend(demo_warnings)
                row['divergencia_totais'] = True
    if not scopes:
        raise TissError(
            'Demonstrativo ainda não disponível; dados anteriores preservados.'
        )
    for row in rows:
        identity = [
            row.get(k)
            for k in (
                'prestador',
                'demonstrativo',
                'protocolo',
                'guia_prestador',
                'escopo',
                'sequencial',
            )
        ]
        row['chave'] = hashlib.sha256(
            json.dumps(identity, ensure_ascii=True).encode()
        ).hexdigest()
    if len({row['chave'] for row in rows}) != len(rows):
        raise TissError(
            'Identidade duplicada no demonstrativo; revisão necessária.'
        )
    return rows, scopes


def payment_protocols(root, config):
    demo = root.find('.//{%s}demonstrativoPagamento' % NS)
    if demo is None:
        raise TissError('Demonstrativo de pagamento ainda não disponível.')
    if (
        value(demo, 'cabecalhoDemonstrativo/registroANS')
        != config.registro_ans
    ):
        raise TissError('Demonstrativo de outra operadora recusado.')
    validate_provider(demo, 'dadosContratado/dadosPrestador', config)
    numbers = {
        value(node, 'numeroProtocolo')
        for node in demo.findall('.//{%s}relacaoProtocolos' % NS)
    }
    if not numbers or None in numbers:
        raise TissError(
            'Demonstrativo sem protocolos; dados anteriores preservados.'
        )
    return sorted(n for n in numbers if set(n) != {'0'})


def validate_provider(root, path, config):
    code = value(root, path + '/codigoPrestadorNaOperadora')
    cnpj = value(root, path + '/cnpjContratado')
    if code:
        valid = code == config.identificador_prestador
    else:
        valid = cnpj == config.prestador_cnpj
    if not valid:
        raise TissError(
            'Demonstrativo de outro prestador ou sem identificação.'
        )


def validate_response_identity(root, config):
    """Verify both identities, including an echoed request header."""
    ans = value(root, 'cabecalho/origem/registroANS')
    destination = 'cabecalho/destino/identificacaoPrestador'
    if ans is None:
        ans = value(root, 'cabecalho/destino/registroANS')
        destination = 'cabecalho/origem/identificacaoPrestador'
    if ans != config.registro_ans:
        raise TissError('Resposta recebida de outra operadora recusada.')
    code = value(root, destination + '/codigoPrestadorNaOperadora')
    cnpj = value(root, destination + '/CNPJ')
    if not (
        (code and code == config.identificador_prestador)
        or (cnpj and cnpj == config.prestador_cnpj)
    ):
        raise TissError('Resposta de outro prestador ou sem identificação.')


def validate_response_schema(root, config):
    """Validate a copy for the observed CRC header defect."""
    checked = deepcopy(root)
    warnings = []
    for demo in checked.findall('.//{%s}demonstrativoAnaliseConta' % NS):
        validate_provider(demo, 'dadosPrestador/dadosContratado', config)
    for demo in checked.findall('.//{%s}demonstrativoPagamento' % NS):
        validate_provider(demo, 'dadosContratado/dadosPrestador', config)
        node = demo.find(
            '{%s}cabecalhoDemonstrativo/{%s}numeroCNPJ' % (NS, NS)
        )
        if (
            node is not None
            and config.prestador_cnpj.startswith('0')
            and node.text == str(int(config.prestador_cnpj))
        ):
            # CRC returns the provider CNPJ without leading zero in this
            # operator metadata field. Never use it to identify the operator.
            node.text = config.prestador_cnpj
            warnings.append(
                'CNPJ do cabeçalho devolvido incorretamente pelo serviço; '
                'operadora conferida pelo registro ANS. '
                'XML original preservado.'
            )
    unknown = sorted({
        node.text
        for node in checked.findall('.//{%s}tipoGlosa' % NS)
        if node.text not in known_denial_codes()
    })
    try:
        validate(checked)
    except TissError:
        if (
            not unknown
            or checked.tag != '{%s}demonstrativoRetornoWS' % NS
            or not receipt_schema().validate(checked)
        ):
            raise
        warnings.append(
            'Códigos de glosa fora do domínio TISS 4.01 publicado: '
            + ', '.join(unknown)
            + '. Preservados para revisão; envio permanece validado '
            'pelo contrato original.'
        )
    return warnings
