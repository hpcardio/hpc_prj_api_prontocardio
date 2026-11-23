# ruff: noqa: E501
"""Leitura de extratos bancários (fase 5 de Contas a Pagar).

O financeiro envia os arquivos que já baixa todo dia dos bancos (PDF; XLSX no
Santander). Cada leitor devolve as mesmas informações: banco, agência, conta,
período e lançamentos ``{data, histórico, documento, valor, saldo}``, com
débito negativo.

Conferência: quando o extrato traz os saldos, a soma dos lançamentos precisa
fechar com saldo final - saldo inicial (ou com o saldo de cada linha). Um
extrato que não fecha é recusado inteiro; nunca é aceito pela metade. Alguns
PDFs impressos pelo navegador saem com a coluna de saldo cortada (BB diário):
nesse caso não há como conferir e o extrato entra com um aviso.

Nenhuma informação vem de layout fixo por coordenada: cada linha é
reconhecida pela data no início e pelos valores no fim.
"""

import re
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from io import BytesIO
from xml.etree import ElementTree

BANCO_BB = 1
BANCO_BNB = 4
BANCO_SANTANDER = 33
BANCO_CEF = 104
BANCO_UNICRED = 136
BANCO_BRADESCO = 237

NOMES_BANCO = {
    BANCO_BB: 'Banco do Brasil',
    BANCO_BNB: 'Banco do Nordeste',
    BANCO_SANTANDER: 'Santander',
    BANCO_CEF: 'Caixa',
    BANCO_UNICRED: 'Unicred',
    BANCO_BRADESCO: 'Bradesco',
}

CENTAVO = Decimal('0.01')
MAX_LINHAS = 20000

_DATA = r'\d{2}/\d{2}/\d{4}'
_VALOR = r'\d{1,3}(?:\.\d{3})*,\d{2}'


class ExtratoInvalido(ValueError):
    """Arquivo que não é um extrato reconhecido ou que não fecha."""


@dataclass
class LinhaExtrato:
    data: date
    historico: str
    documento: str | None
    valor: Decimal
    saldo: Decimal | None = None
    # Descrição da 1ª linha do lançamento, sem a continuação; é ela que
    # identifica o lançamento entre arquivos que se sobrepõem.
    descricao: str = ''

    def __post_init__(self):
        self.descricao = self.descricao or self.historico

    def as_dict(self) -> dict:
        return {
            'data': self.data,
            'historico': self.historico,
            'documento': self.documento,
            'valor': self.valor,
            'saldo': self.saldo,
        }


@dataclass
class Extrato:
    banco: int
    agencia: str
    conta: str
    conta_dv: str | None = None
    titular: str | None = None
    periodo_inicio: date | None = None
    periodo_fim: date | None = None
    saldo_inicial: Decimal | None = None
    saldo_final: Decimal | None = None
    linhas: list[LinhaExtrato] = field(default_factory=list)
    avisos: list[str] = field(default_factory=list)
    conferido: bool = False

    @property
    def nome_banco(self) -> str:
        return NOMES_BANCO.get(self.banco, str(self.banco))


# ---------------------------------------------------------------- utilidades


def _valor_br(texto: str) -> Decimal:
    limpo = texto.replace('R$', '').replace(' ', '').replace('.', '')
    try:
        return Decimal(limpo.replace(',', '.')).quantize(CENTAVO)
    except InvalidOperation as exc:
        raise ExtratoInvalido(f'Valor inválido no extrato: {texto!r}') from exc


def _data_br(texto: str) -> date:
    return datetime.strptime(texto.replace(' ', ''), '%d/%m/%Y').date()


def _sem_espacos(texto: str) -> str:
    return re.sub(r'\s+', '', texto)


def _so_digitos(texto: str | None) -> str:
    return re.sub(r'\D', '', texto or '')


def _sem_zeros(texto: str) -> str:
    return texto.lstrip('0') or '0'


def _linhas_pdf(conteudo: bytes) -> list[str]:
    from pypdf import PdfReader  # noqa: PLC0415 - dependência só deste fluxo

    try:
        leitor = PdfReader(BytesIO(conteudo))
        paginas = [
            pagina.extract_text(extraction_mode='layout') or ''
            for pagina in leitor.pages
        ]
    except Exception as exc:  # noqa: BLE001 - qualquer PDF ilegível
        raise ExtratoInvalido('Não foi possível ler o PDF.') from exc
    return [linha.rstrip() for linha in '\n'.join(paginas).splitlines()]


def _conferir(extrato: Extrato) -> Extrato:
    """Confere a soma dos lançamentos com os saldos que o extrato traz."""
    if len(extrato.linhas) > MAX_LINHAS:
        raise ExtratoInvalido('Extrato com lançamentos demais.')
    soma = sum((linha.valor for linha in extrato.linhas), Decimal('0'))
    if not extrato.linhas and extrato.saldo_inicial is not None:
        extrato.saldo_final = (
            extrato.saldo_final if extrato.saldo_final is not None
            else extrato.saldo_inicial
        )
    if extrato.saldo_inicial is not None and extrato.saldo_final is not None:
        esperado = (extrato.saldo_final - extrato.saldo_inicial).quantize(CENTAVO)
        if soma.quantize(CENTAVO) != esperado:
            raise ExtratoInvalido(
                'O extrato não fecha: a soma dos lançamentos '
                f'({soma:.2f}) difere de saldo final - saldo inicial '
                f'({esperado:.2f}).'
            )
        extrato.conferido = True
    elif extrato.saldo_inicial is not None and any(
        linha.saldo is not None for linha in extrato.linhas
    ):
        corrente = extrato.saldo_inicial
        for linha in extrato.linhas:
            corrente += linha.valor
            if linha.saldo is not None and corrente != linha.saldo:
                raise ExtratoInvalido(
                    f'O extrato não fecha no lançamento de {linha.data:%d/%m/%Y} '
                    f'({linha.historico[:40]}): saldo calculado {corrente:.2f}, '
                    f'impresso {linha.saldo:.2f}.'
                )
        extrato.saldo_final = corrente
        extrato.conferido = True
    else:
        extrato.avisos.append(
            'O extrato não traz os saldos (coluna cortada na impressão): '
            'os lançamentos não puderam ser conferidos com o saldo.'
        )
    if extrato.linhas:
        datas = [linha.data for linha in extrato.linhas]
        extrato.periodo_inicio = extrato.periodo_inicio or min(datas)
        extrato.periodo_fim = extrato.periodo_fim or max(datas)
    return extrato


def _acrescentar_historico(linhas: list[LinhaExtrato], texto: str) -> None:
    texto = re.sub(r'\s+', ' ', texto).strip()
    if linhas and texto:
        linhas[-1].historico = f'{linhas[-1].historico} {texto}'.strip()[:300]


# ---------------------------------------------------------------- Banco do Brasil

_BB_RESUMO_SALDO = re.compile(rf'^\s*Saldo\s+(?P<valor>{_VALOR})\s(?P<dc>[CD])\s*$')
_BB_LINHA = re.compile(
    rf'^\s*(?P<data>{_DATA})\s+(?:{_DATA}\s+)?(?P<ag>\d{{4}})\s+(?P<lote>\d{{5}})\s+'
    rf'(?P<hist>.+?)\s{{2,}}(?:(?P<doc>[\d.]+)\s+)?(?P<valor>{_VALOR})\s(?P<dc>[CD])'
    rf'(?:\s+(?P<saldo>{_VALOR})\s(?P<sdc>[CD]))?\s*$'
)
_BB_SALDO = re.compile(
    rf'^\s*(?P<data>{_DATA})\s+(?:{_DATA}\s+)?\d{{4}}\s+00000\s+(?:\d{{3}}\s+)?'
    rf'(?P<hist>Saldo Anterior|S A L D O)\s*(?:(?P<valor>{_VALOR})\s(?P<dc>[CD]))?'
    rf'(?:\s+(?P<saldo>{_VALOR})\s(?P<sdc>[CD]))?\s*$'
)


def _bb_valor(valor: str, dc: str) -> Decimal:
    numero = _valor_br(valor)
    return -numero if dc == 'D' else numero


def ler_bb(linhas: list[str]) -> Extrato:  # noqa: PLR0912 - um caso por tipo de linha do BB
    texto = '\n'.join(linhas)
    agencia = re.search(r'Ag[eê]ncia\s+(\d+)-?([\dXx])?', texto)
    conta = re.search(r'Conta corrente\s+(\d+)-?([\dXx])?', texto)
    if not agencia or not conta:
        raise ExtratoInvalido('Extrato do BB sem agência ou conta.')
    extrato = Extrato(
        banco=BANCO_BB, agencia=_sem_zeros(agencia.group(1)),
        conta=_sem_zeros(conta.group(1)), conta_dv=(conta.group(2) or '').upper() or None,
    )
    periodo = re.search(
        r'de\s+(\d{2}\s*/\s*\d{2}\s*/\s*\d{4})\s+até\s+(\d{2}\s*/\s*\d{2}\s*/\s*\d{4})', texto
    )
    if periodo:
        extrato.periodo_inicio = _data_br(periodo.group(1))
        extrato.periodo_fim = _data_br(periodo.group(2))
    dentro = False
    resumo_saldo = None
    for linha in linhas:
        resumo = _BB_RESUMO_SALDO.match(linha)
        if resumo:
            resumo_saldo = _bb_valor(resumo.group('valor'), resumo.group('dc'))
            continue
        saldo = _BB_SALDO.match(linha)
        if saldo:
            dentro = True
            numero = saldo.group('saldo') or saldo.group('valor')
            if numero:
                dc = saldo.group('sdc') or saldo.group('dc')
                if saldo.group('hist') == 'Saldo Anterior':
                    extrato.saldo_inicial = _bb_valor(numero, dc)
                else:
                    extrato.saldo_final = _bb_valor(numero, dc)
            if saldo.group('hist') == 'S A L D O':
                dentro = False
            continue
        lancamento = _BB_LINHA.match(linha)
        if lancamento:
            dentro = True
            extrato.linhas.append(LinhaExtrato(
                data=_data_br(lancamento.group('data')),
                historico=re.sub(r'\s+', ' ', lancamento.group('hist')).strip(),
                documento=lancamento.group('doc'),
                valor=_bb_valor(lancamento.group('valor'), lancamento.group('dc')),
                saldo=(_bb_valor(lancamento.group('saldo'), lancamento.group('sdc'))
                       if lancamento.group('saldo') else None),
            ))
            continue
        if dentro and re.match(r'^\s{20,}\S', linha) and not re.search(r'https?\s*:|\d{1,2}:\d{2}\s*[AP]M', linha):
            _acrescentar_historico(extrato.linhas, linha)
    # Saldos de linhas intermediárias (saldo do dia) não entram na conferência
    # corrente; só o saldo final impresso (linha "S A L D O" ou, se ela saiu
    # cortada, o quadro "Saldo" do fim do extrato).
    for linha_extrato in extrato.linhas:
        linha_extrato.saldo = None
    if extrato.saldo_final is None and extrato.saldo_inicial is not None:
        extrato.saldo_final = resumo_saldo
    return _conferir(extrato)


# ---------------------------------------------------------------- Bradesco

_BRADESCO_LINHA = re.compile(
    rf'^\s*(?:(?P<data>{_DATA})\s+)?(?P<hist>\S.*?)\s{{2,}}(?:(?P<doc>\d+)\s+)?'
    rf'(?P<valor>-?{_VALOR})\s+(?P<saldo>-?{_VALOR})\s*$'
)


def ler_bradesco(linhas: list[str]) -> Extrato:
    texto = '\n'.join(linhas)
    cabecalho = re.search(r'Ag:\s*(\d+)\s*\|\s*CC:\s*(\d+)-?(\d)?', texto)
    if not cabecalho:
        raise ExtratoInvalido('Extrato do Bradesco sem agência ou conta.')
    extrato = Extrato(
        banco=BANCO_BRADESCO, agencia=_sem_zeros(cabecalho.group(1)),
        conta=_sem_zeros(cabecalho.group(2)), conta_dv=cabecalho.group(3),
    )
    data_atual = None
    dentro = False
    for linha in linhas:
        if re.match(r'^\s*Data\s+Lançamento', linha):
            dentro = True
            continue
        if not dentro:
            continue
        if re.match(r'^\s*Total\b', linha) or 'Os dados acima' in linha:
            break
        anterior = re.match(rf'^\s*({_DATA})\s+SALDO ANTERIOR\s+(-?{_VALOR})\s*$', linha)
        if anterior:
            extrato.saldo_inicial = _valor_br(anterior.group(2))
            data_atual = _data_br(anterior.group(1))
            continue
        lancamento = _BRADESCO_LINHA.match(linha)
        if lancamento:
            if lancamento.group('data'):
                data_atual = _data_br(lancamento.group('data'))
            if data_atual is None:
                raise ExtratoInvalido('Lançamento do Bradesco sem data.')
            extrato.linhas.append(LinhaExtrato(
                data=data_atual,
                historico=re.sub(r'\s+', ' ', lancamento.group('hist')).strip(),
                documento=lancamento.group('doc'),
                valor=_valor_br(lancamento.group('valor')),
                saldo=_valor_br(lancamento.group('saldo')),
            ))
            continue
        data_solta = re.match(rf'^\s*({_DATA})\s*$', linha)
        if data_solta:
            data_atual = _data_br(data_solta.group(1))
            continue
        if linha.strip():
            _acrescentar_historico(extrato.linhas, linha)
    if extrato.saldo_inicial is None:
        raise ExtratoInvalido('Extrato do Bradesco sem saldo anterior.')
    return _conferir(extrato)


# ---------------------------------------------------------------- Banco do Nordeste

_BNB_LINHA = re.compile(
    rf'^\s*(?P<data>{_DATA})\s+(?P<hist>\S.*?)\s{{2,}}(?P<doc>\d+)\s+'
    rf'(?P<valor>-?\s?{_VALOR})\s+(?P<saldo>-?\s?{_VALOR})\s*$'
)


def ler_bnb(linhas: list[str]) -> Extrato:
    texto = '\n'.join(linhas)
    cabecalho = re.search(r'Ag[eê]ncia/Conta Corrente:\s*(\d+)\s*-\s*[^/]*/(\d+)-?(\d)?', texto)
    if not cabecalho:
        raise ExtratoInvalido('Extrato do BNB sem agência ou conta.')
    extrato = Extrato(
        banco=BANCO_BNB, agencia=_sem_zeros(cabecalho.group(1)),
        conta=_sem_zeros(cabecalho.group(2)), conta_dv=cabecalho.group(3),
    )
    titular = re.search(r'Titular:\s*(.+)', texto)
    if titular:
        extrato.titular = titular.group(1).strip()[:120]
    anterior = re.search(rf'Saldo Anterior:\s*R\$\s*(-?\s?{_VALOR})', texto)
    if anterior:
        extrato.saldo_inicial = _valor_br(anterior.group(1))
    periodo = re.search(rf'Período:\s*({_DATA})\s+até\s+({_DATA})', texto)
    if periodo:
        extrato.periodo_inicio = _data_br(periodo.group(1))
        extrato.periodo_fim = _data_br(periodo.group(2))
    dentro = False
    for linha in linhas:
        if 'Detalhamento do Extrato' in linha:
            dentro = True
            continue
        if 'Detalhamento do Saldo' in linha:
            break
        if not dentro or re.match(r'^\s*Data\s+Hist', linha):
            continue
        lancamento = _BNB_LINHA.match(linha)
        if lancamento:
            extrato.linhas.append(LinhaExtrato(
                data=_data_br(lancamento.group('data')),
                historico=re.sub(r'\s+', ' ', lancamento.group('hist')).strip(),
                documento=lancamento.group('doc'),
                valor=_valor_br(lancamento.group('valor')),
                saldo=_valor_br(lancamento.group('saldo')),
            ))
        elif linha.strip():
            _acrescentar_historico(extrato.linhas, linha)
    if extrato.saldo_inicial is None:
        raise ExtratoInvalido('Extrato do BNB sem saldo anterior.')
    return _conferir(extrato)


# ---------------------------------------------------------------- Caixa

_CEF_LINHA = re.compile(
    rf'^\s*(?P<data>{_DATA})\s+(?P<doc>\d+)\s+(?P<hist>\S.*?)\s{{2,}}'
    rf'(?P<valor>{_VALOR})\s(?P<dc>[CD])\s+(?P<saldo>{_VALOR})\s(?P<sdc>[CD])\s*$'
)


def ler_cef(linhas: list[str]) -> Extrato:
    texto = '\n'.join(linhas)
    cabecalho = re.search(r'Conta:\s*(\d+)\s*\|\s*(\d+)\s*\|\s*(\d+)-?(\d)?', texto)
    if not cabecalho:
        raise ExtratoInvalido('Extrato da Caixa sem agência ou conta.')
    extrato = Extrato(
        banco=BANCO_CEF, agencia=_sem_zeros(cabecalho.group(1)),
        conta=_sem_zeros(cabecalho.group(3)), conta_dv=cabecalho.group(4),
    )
    anterior = re.search(rf'SALDO ANTERIOR\s+({_VALOR})\s+({_VALOR})\s([CD])', texto)
    if anterior:
        extrato.saldo_inicial = _bb_valor(anterior.group(2), anterior.group(3))
    for linha in linhas:
        lancamento = _CEF_LINHA.match(linha)
        if not lancamento:
            continue
        valor = _bb_valor(lancamento.group('valor'), lancamento.group('dc'))
        saldo = _bb_valor(lancamento.group('saldo'), lancamento.group('sdc'))
        if lancamento.group('hist').strip() == 'SALDO DIA':
            if valor != 0:
                raise ExtratoInvalido('Linha "SALDO DIA" da Caixa com valor.')
            continue
        extrato.linhas.append(LinhaExtrato(
            data=_data_br(lancamento.group('data')),
            historico=re.sub(r'\s+', ' ', lancamento.group('hist')).strip(),
            documento=lancamento.group('doc'),
            valor=valor,
            saldo=saldo,
        ))
    if extrato.saldo_inicial is None:
        raise ExtratoInvalido('Extrato da Caixa sem saldo anterior.')
    return _conferir(extrato)


# ---------------------------------------------------------------- Unicred

_UNICRED_VALOR = rf'-?\s?R\$\s*{_VALOR}'
_UNICRED_LINHA = re.compile(
    rf'^\s*(?P<data>{_DATA})\s+(?P<hist>\S.*?)\s{{2,}}(?P<valor>{_UNICRED_VALOR})'
    rf'\s+(?P<saldo>{_UNICRED_VALOR})\s*$'
)


def ler_unicred(linhas: list[str]) -> Extrato:
    texto = '\n'.join(linhas)
    cabecalho = re.search(r'AG:\s*(\d+)\s*-\s*Conta:\s*(\d+)', texto)
    if not cabecalho:
        raise ExtratoInvalido('Extrato da Unicred sem agência ou conta.')
    numero = cabecalho.group(2)
    extrato = Extrato(
        banco=BANCO_UNICRED, agencia=_sem_zeros(cabecalho.group(1)),
        conta=_sem_zeros(numero[:-1]), conta_dv=numero[-1],
    )
    periodo = re.search(rf'Período de\s+({_DATA})\s+a\s+({_DATA})', texto)
    if periodo:
        extrato.periodo_inicio = _data_br(periodo.group(1))
        extrato.periodo_fim = _data_br(periodo.group(2))
    final = re.search(rf'Saldo no final do período\s+({_UNICRED_VALOR})', texto)
    if final:
        extrato.saldo_final = _valor_br(final.group(1))
    for linha in linhas:
        lancamento = _UNICRED_LINHA.match(linha)
        if lancamento:
            extrato.linhas.append(LinhaExtrato(
                data=_data_br(lancamento.group('data')),
                historico=re.sub(r'\s+', ' ', lancamento.group('hist')).strip(),
                documento=None,
                valor=_valor_br(lancamento.group('valor')),
                saldo=_valor_br(lancamento.group('saldo')),
            ))
    if extrato.saldo_final is None:
        raise ExtratoInvalido('Extrato da Unicred sem saldo final.')
    # O saldo inicial impresso ("Saldo em dd/mm") vem sem sinal; ele é
    # deduzido do saldo final e conferido pelo saldo de cada linha.
    extrato.saldo_inicial = extrato.saldo_final - sum(
        (linha.valor for linha in extrato.linhas), Decimal('0')
    )
    if extrato.linhas:
        corrente = extrato.saldo_inicial
        for linha_extrato in extrato.linhas:
            corrente += linha_extrato.valor
            if corrente != linha_extrato.saldo:
                raise ExtratoInvalido('O extrato da Unicred não fecha linha a linha.')
    extrato.conferido = True
    if extrato.linhas:
        datas = [linha.data for linha in extrato.linhas]
        extrato.periodo_inicio = extrato.periodo_inicio or min(datas)
        extrato.periodo_fim = extrato.periodo_fim or max(datas)
    return extrato


# ---------------------------------------------------------------- Santander (XLSX)

_NS = {'m': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}


def _celulas_xlsx(conteudo: bytes) -> list[list[str]]:
    """Lê a primeira planilha de um XLSX só com a biblioteca padrão."""
    try:
        with zipfile.ZipFile(BytesIO(conteudo)) as pacote:
            compartilhadas = []
            if 'xl/sharedStrings.xml' in pacote.namelist():
                raiz = ElementTree.fromstring(pacote.read('xl/sharedStrings.xml'))
                for item in raiz.findall('m:si', _NS):
                    compartilhadas.append(''.join(t.text or '' for t in item.iter(f'{{{_NS["m"]}}}t')))
            planilhas = sorted(n for n in pacote.namelist() if re.match(r'xl/worksheets/sheet\d+\.xml$', n))
            if not planilhas:
                raise ExtratoInvalido('XLSX sem planilha.')
            raiz = ElementTree.fromstring(pacote.read(planilhas[0]))
    except (zipfile.BadZipFile, ElementTree.ParseError, KeyError) as exc:
        raise ExtratoInvalido('Não foi possível ler o XLSX.') from exc
    linhas = []
    for linha in raiz.iter(f'{{{_NS["m"]}}}row'):
        valores = {}
        for celula in linha.findall('m:c', _NS):
            referencia = re.match(r'([A-Z]+)', celula.get('r', 'A'))
            coluna = 0
            for letra in referencia.group(1):
                coluna = coluna * 26 + (ord(letra) - 64)
            tipo = celula.get('t')
            bruto = celula.find('m:v', _NS)
            if tipo == 's' and bruto is not None:
                valor = compartilhadas[int(bruto.text)]
            elif tipo == 'inlineStr':
                valor = ''.join(t.text or '' for t in celula.iter(f'{{{_NS["m"]}}}t'))
            else:
                valor = bruto.text if bruto is not None else ''
            valores[coluna] = (valor or '').strip()
        if valores:
            linhas.append([valores.get(i, '') for i in range(1, max(valores) + 1)])
    return linhas


def ler_santander_xlsx(conteudo: bytes) -> Extrato:
    celulas = _celulas_xlsx(conteudo)
    agencia = conta = None
    inicio = None
    for indice, linha in enumerate(celulas):
        if linha and linha[0].upper() == 'AGENCIA' and len(linha) >= 4:  # noqa: PLR2004
            agencia, conta = _so_digitos(linha[1]), _so_digitos(linha[3])
        if [c.lower() for c in linha[:4]] == ['data', 'histórico', 'documento', 'valor (r$)']:
            inicio = indice + 1
            break
    if not agencia or not conta or inicio is None:
        raise ExtratoInvalido('XLSX do Santander sem agência, conta ou tabela de lançamentos.')
    extrato = Extrato(
        banco=BANCO_SANTANDER, agencia=_sem_zeros(agencia),
        conta=_sem_zeros(conta[:-1]), conta_dv=conta[-1],
    )
    for linha in celulas[inicio:]:
        if len(linha) < 5 or not re.fullmatch(_DATA, linha[0]):  # noqa: PLR2004
            continue
        try:
            valor = Decimal(linha[3]).quantize(CENTAVO)
            saldo = Decimal(linha[4]).quantize(CENTAVO)
        except InvalidOperation as exc:
            raise ExtratoInvalido('Valor inválido no XLSX do Santander.') from exc
        extrato.linhas.append(LinhaExtrato(
            data=_data_br(linha[0]),
            historico=re.sub(r'\s+', ' ', linha[1]).strip()[:300],
            documento=linha[2] or None,
            valor=valor,
            saldo=saldo,
        ))
    # O Santander lista do mais recente para o mais antigo.
    extrato.linhas.reverse()
    if extrato.linhas:
        primeira = extrato.linhas[0]
        extrato.saldo_inicial = primeira.saldo - primeira.valor
    return _conferir(extrato)


# ---------------------------------------------------------------- entrada


def ler_extrato(conteudo: bytes, nome_arquivo: str = '') -> Extrato:
    """Identifica o banco pelo conteúdo e lê o extrato."""
    if re.search(r'aplica', nome_arquivo, re.IGNORECASE):
        raise ExtratoInvalido(
            'Extratos de aplicação não entram na baixa de títulos; '
            'envie o extrato da conta corrente.'
        )
    if conteudo[:2] == b'PK':
        try:
            return ler_santander_xlsx(conteudo)
        except ExtratoInvalido as exc:
            raise ExtratoInvalido(
                'Planilha não reconhecida: o XLSX é aceito só no Santander. '
                'Nos demais bancos, envie o PDF.'
            ) from exc
    if not conteudo.startswith(b'%PDF'):
        raise ExtratoInvalido('Envie o PDF do extrato (ou o XLSX, no Santander).')
    linhas = _linhas_pdf(conteudo)
    compacto = _sem_espacos('\n'.join(linhas[:80])).lower()
    if 'santander' in compacto or 'contamax' in compacto:
        raise ExtratoInvalido(
            'O PDF do Santander sai com a coluna de valores cortada. '
            'Envie o extrato exportado em Excel (XLSX).'
        )
    if 'bancodobrasil' in compacto or 'clientecontaatual' in compacto.replace('-', ''):
        return ler_bb(linhas)
    if 'extrato(últimoslançamentos)' in compacto or 'extratode:ag:' in compacto:
        return ler_bradesco(linhas)
    if 'bankingbnb' in compacto or 'agência/contacorrente:' in compacto:
        return ler_bnb(linhas)
    if 'caixa' in compacto and 'extratoporperíodo' in compacto:
        return ler_cef(linhas)
    if 'coop:' in compacto:
        return ler_unicred(linhas)
    raise ExtratoInvalido(
        f'Banco não reconhecido no arquivo {nome_arquivo!r}. Leitores: '
        'BB, Bradesco, BNB, Caixa, Unicred (PDF) e Santander (XLSX).'
    )
