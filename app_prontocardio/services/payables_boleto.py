# ruff: noqa: E501, PLR2004
"""Leitura de boletos (PDF) para gerar a duplicata de uma NFS-e recebida.

A fonte principal é a **linha digitável** (padrão FEBRABAN), que não depende
do layout do banco:

- boleto bancário: 47 dígitos (código de barras de 44), com banco, valor e
  fator de vencimento;
- arrecadação/convênio (concessionárias, tributos): 48 dígitos, começa com 8,
  traz valor mas não vencimento.

Os dígitos verificadores são conferidos; uma linha inválida nunca vira
duplicata. Do texto do PDF também saem, quando presentes, o CNPJ do
beneficiário e o nosso número, usados só para conferência com a NFS-e.
"""

import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from io import BytesIO

# Fator de vencimento: dias desde 07/10/1997. Ao chegar a 9999 (21/02/2025)
# o fator reiniciou em 1000 (FEBRABAN). Fatores >= 1000 são interpretados na
# janela que contém a data de referência.
BASE_FATOR = date(1997, 10, 7)
CICLO_FATOR = 9000

RE_LINHA_BANCARIA = re.compile(
    r'(\d{5})[.\s]?(\d{5})\s*(\d{5})[.\s]?(\d{6})\s*(\d{5})[.\s]?(\d{6})\s*(\d)\s*(\d{14})'
)
RE_LINHA_ARRECADACAO = re.compile(
    r'(8\d{10})[-\s]?(\d)\s*(\d{11})[-\s]?(\d)\s*(\d{11})[-\s]?(\d)\s*(\d{11})[-\s]?(\d)'
)
RE_CNPJ = re.compile(r'\b\d{2}\.?\d{3}\.?\d{3}/?\d{4}-?\d{2}\b')
RE_VALOR_DOCUMENTO = re.compile(r'Valor\s+(?:do\s+)?Documento\s*[:\n ]\s*(?:R\$\s*)?(\d+(?:\.\d{3})*,\d{2})', re.I)
RE_NOSSO_NUMERO = re.compile(r'Nosso\s+N[uú]mero\s*[:\n ]\s*([0-9./\- ]{5,30})', re.I)


class BoletoInvalido(ValueError):
    """Linha digitável ausente ou com dígito verificador errado."""


@dataclass
class BoletoLido:
    linha_digitavel: str
    codigo_barras: str
    tipo: str  # 'bancario' | 'arrecadacao'
    banco: str | None
    valor: Decimal | None
    vencimento: date | None
    cnpjs_no_texto: list[str] = field(default_factory=list)
    nosso_numero: str | None = None
    valor_impresso: Decimal | None = None

    def as_dict(self) -> dict:
        return {
            'linha_digitavel': self.linha_digitavel,
            'codigo_barras': self.codigo_barras,
            'tipo': self.tipo,
            'banco': self.banco,
            'valor': self.valor,
            'vencimento': self.vencimento,
            'cnpjs_no_texto': self.cnpjs_no_texto,
            'nosso_numero': self.nosso_numero,
            'valor_impresso': self.valor_impresso,
        }


def _digitos(valor) -> str:
    return re.sub(r'\D', '', str(valor or ''))


def modulo10(numero: str) -> int:
    soma = 0
    for posicao, algarismo in enumerate(reversed(numero)):
        produto = int(algarismo) * (2 if posicao % 2 == 0 else 1)
        soma += produto // 10 + produto % 10
    return (10 - soma % 10) % 10


def modulo11_bancario(numero: str) -> int:
    soma = sum(int(a) * (2 + i % 8) for i, a in enumerate(reversed(numero)))
    resto = 11 - soma % 11
    return 1 if resto in (0, 10, 11) else resto


def modulo11_arrecadacao(numero: str) -> int:
    soma = sum(int(a) * (2 + i % 8) for i, a in enumerate(reversed(numero)))
    resto = soma % 11
    return 0 if resto in (0, 1) else 11 - resto


def vencimento_do_fator(fator: int, referencia: date) -> date | None:
    """Converte o fator considerando o reinício de 22/02/2025 (fator 1000)."""
    if fator == 0:
        return None
    candidatos = [BASE_FATOR + timedelta(days=fator + CICLO_FATOR * ciclo) for ciclo in range(3)]
    candidatos = [c for c in candidatos if c >= BASE_FATOR + timedelta(days=1000)]
    return min(candidatos, key=lambda d: abs((d - referencia).days))


def ler_linha_bancaria(linha: str, referencia: date) -> BoletoLido:
    d = _digitos(linha)
    if len(d) != 47:
        raise BoletoInvalido('A linha digitável de boleto bancário tem 47 dígitos.')
    campos = ((d[0:9], d[9]), (d[10:20], d[20]), (d[21:31], d[31]))
    for numero, dv in campos:
        if modulo10(numero) != int(dv):
            raise BoletoInvalido('Dígito verificador da linha digitável não confere.')
    codigo = d[0:4] + d[32] + d[33:47] + d[4:9] + d[10:20] + d[21:31]
    if modulo11_bancario(codigo[:4] + codigo[5:]) != int(codigo[4]):
        raise BoletoInvalido('Dígito verificador geral do código de barras não confere.')
    fator = int(d[33:37])
    valor = Decimal(int(d[37:47])) / 100
    return BoletoLido(
        linha_digitavel=d,
        codigo_barras=codigo,
        tipo='bancario',
        banco=d[0:3],
        valor=valor if valor > 0 else None,
        vencimento=vencimento_do_fator(fator, referencia),
    )


def ler_linha_arrecadacao(linha: str) -> BoletoLido:
    d = _digitos(linha)
    if len(d) != 48 or d[0] != '8':
        raise BoletoInvalido('A linha digitável de arrecadação tem 48 dígitos e começa com 8.')
    blocos = [(d[i:i + 11], d[i + 11]) for i in range(0, 48, 12)]
    calc = modulo10 if d[2] in '67' else modulo11_arrecadacao
    for numero, dv in blocos:
        if calc(numero) != int(dv):
            raise BoletoInvalido('Dígito verificador da linha de arrecadação não confere.')
    codigo = ''.join(numero for numero, _ in blocos)
    valor = Decimal(int(codigo[4:15])) / 100 if d[2] in '68' else None
    return BoletoLido(
        linha_digitavel=d,
        codigo_barras=codigo,
        tipo='arrecadacao',
        banco=None,
        valor=valor if valor else None,
        vencimento=None,
    )


def ler_linha_digitavel(linha: str, referencia: date) -> BoletoLido:
    d = _digitos(linha)
    if len(d) == 48:
        return ler_linha_arrecadacao(d)
    return ler_linha_bancaria(d, referencia)


def extrair_texto_pdf(conteudo: bytes) -> str:
    from pypdf import PdfReader  # noqa: PLC0415 - dependência só deste fluxo

    leitor = PdfReader(BytesIO(conteudo))
    return '\n'.join((pagina.extract_text() or '') for pagina in leitor.pages)


def ler_boleto_texto(texto: str, referencia: date) -> BoletoLido:
    """Procura a linha digitável no texto; a primeira válida vence."""
    candidatos = [''.join(m.groups()) for m in RE_LINHA_BANCARIA.finditer(texto)]
    candidatos += [''.join(m.groups()) for m in RE_LINHA_ARRECADACAO.finditer(texto)]
    erros = []
    for candidato in dict.fromkeys(candidatos):
        try:
            lido = ler_linha_digitavel(candidato, referencia)
        except BoletoInvalido as exc:
            erros.append(str(exc))
            continue
        lido.cnpjs_no_texto = list(dict.fromkeys(_digitos(c) for c in RE_CNPJ.findall(texto)))
        nosso = RE_NOSSO_NUMERO.search(texto)
        lido.nosso_numero = nosso.group(1).strip() if nosso else None
        impresso = RE_VALOR_DOCUMENTO.search(texto)
        if impresso:
            lido.valor_impresso = Decimal(impresso.group(1).replace('.', '').replace(',', '.'))
        return lido
    if erros:
        raise BoletoInvalido(erros[0])
    raise BoletoInvalido('Linha digitável não encontrada no PDF. Informe-a manualmente.')


def ler_boleto_pdf(conteudo: bytes, referencia: date) -> BoletoLido:
    if not conteudo.startswith(b'%PDF'):
        raise BoletoInvalido('O arquivo enviado não é um PDF.')
    return ler_boleto_texto(extrair_texto_pdf(conteudo), referencia)
