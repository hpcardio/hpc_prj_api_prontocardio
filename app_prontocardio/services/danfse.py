# ruff: noqa: E501
"""Leitura do DANFSe (documento auxiliar da NFS-e do padrão nacional) a partir do PDF.

O layout do DANFSe é uma grade fixa: cada linha de rótulos é seguida pela
linha de valores, nas mesmas colunas. Com o texto em modo "layout" do pypdf,
cada valor é atribuído ao rótulo cuja coluna começa mais perto dele.

Devolve os dados com os mesmos nomes de coluna de ``nfse_xml_tomador``,
para que fila, boletos, simulação e importação tratem a nota igual às do ISS
Fortaleza. Também confere a nota com a chave de acesso (50 dígitos: município,
ambiente, tipo de inscrição, CNPJ do prestador, número, AAMM, código, DV).
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation

import pypdf

TOLERANCIA = Decimal('0.02')
TEXTO_MINIMO = 50  # menos que isso: PDF escaneado (imagem)
RE_CHAVE = re.compile(r'(?<!\d)(\d{50})(?!\d)')
RE_PEDACO = re.compile(r'\S+(?: \S+)*')  # palavras separadas por um espaço; 2+ espaços separam colunas
RE_DINHEIRO = re.compile(r'-?R\$\s*-?[\d.]+,\d{2}|-?\d{1,3}(?:\.\d{3})*,\d{2}')
RE_DATA_HORA = re.compile(r'(\d{2})/(\d{2})/(\d{4})(?:\s+(\d{2}):(\d{2})(?::(\d{2}))?)?')


class DanfseIlegivel(ValueError):
    """O PDF não tem texto (escaneado) ou não pôde ser lido."""


@dataclass
class LeituraDanfse:
    reconhecido: bool
    dados: dict = field(default_factory=dict)
    avisos: list[str] = field(default_factory=list)
    informativos: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {'reconhecido': self.reconhecido, 'dados': self.dados, 'avisos': self.avisos, 'informativos': self.informativos}


def somente_digitos(valor) -> str:
    return re.sub(r'\D', '', str(valor or ''))


def dinheiro(texto: str | None) -> Decimal | None:
    """'R$ 18.551,40' -> 18551.40; '-' ou vazio -> None."""
    encontrado = RE_DINHEIRO.search(texto or '')
    if not encontrado:
        return None
    bruto = encontrado.group(0).replace('R$', '').replace(' ', '')
    negativo = bruto.startswith('-')
    try:
        valor = Decimal(bruto.lstrip('-').replace('.', '').replace(',', '.'))
    except InvalidOperation:
        return None
    return -valor if negativo else valor


def data_hora(texto: str | None) -> datetime | None:
    encontrado = RE_DATA_HORA.search(texto or '')
    if not encontrado:
        return None
    dia, mes, ano, hora, minuto, segundo = encontrado.groups()
    try:
        return datetime(int(ano), int(mes), int(dia), int(hora or 0), int(minuto or 0), int(segundo or 0))
    except ValueError:
        return None


def _normalizar(texto: str) -> str:
    return re.sub(r'\s+', ' ', texto or '').strip().upper()


class Grade:
    """Texto em modo layout: localiza um rótulo e devolve o valor da linha de baixo, na mesma coluna."""

    def __init__(self, texto: str):
        self.linhas = texto.splitlines()

    def indice(self, rotulo: str, inicio: int = 0, fim: int | None = None) -> int | None:
        alvo = _normalizar(rotulo)
        for i in range(inicio, len(self.linhas) if fim is None else min(fim, len(self.linhas))):
            if alvo in _normalizar(self.linhas[i]):
                return i
        return None

    def valor(self, rotulo: str, inicio: int = 0, fim: int | None = None) -> str | None:
        """Valor da coluna do rótulo na primeira linha não vazia abaixo dele."""
        i = self.indice(rotulo, inicio, fim)
        if i is None:
            return None
        colunas = sorted((m.start(), _normalizar(m.group(0))) for m in RE_PEDACO.finditer(self.linhas[i]))
        alvo = _normalizar(rotulo)
        coluna = next((pos for pos, txt in colunas if alvo in txt), None)
        if coluna is None:
            return None
        j = i + 1
        while j < len(self.linhas) and not self.linhas[j].strip():
            j += 1
        if j >= len(self.linhas):
            return None
        escolhidos = []
        for m in RE_PEDACO.finditer(self.linhas[j]):
            # Dono: a coluna de rótulo mais à direita que começa até 6 caracteres depois do valor.
            candidatos = [pos for pos, _ in colunas if pos <= m.start() + 6]
            dono = max(candidatos) if candidatos else colunas[0][0]
            if dono == coluna:
                escolhidos.append(m.group(0))
        valor = ' '.join(escolhidos).strip()
        return None if valor in ('', '-', '--') else valor

    def bloco_apos(self, rotulo: str, ate: tuple[str, ...]) -> str | None:
        i = self.indice(rotulo)
        if i is None:
            return None
        partes = []
        for linha in self.linhas[i + 1:]:
            if any(_normalizar(p) in _normalizar(linha) for p in ate):
                break
            if linha.strip():
                partes.append(linha.strip())
        return ' '.join(partes).strip() or None


def _texto_layout(conteudo: bytes) -> str:
    try:
        leitor = pypdf.PdfReader(io.BytesIO(conteudo))
        return '\n'.join((p.extract_text(extraction_mode='layout') or '') for p in leitor.pages[:3])
    except Exception as exc:  # noqa: BLE001 - qualquer falha do pypdf vira PDF ilegível
        raise DanfseIlegivel('Não foi possível ler o PDF.') from exc


def ler_danfse_texto(texto: str) -> LeituraDanfse:
    if 'DANFSE' not in texto.upper() and not RE_CHAVE.search(texto):
        return LeituraDanfse(False, avisos=['O PDF não está no padrão nacional (DANFSe). Preencha os dados da nota.'])
    g = Grade(texto)
    leitura = LeituraDanfse(True)
    d = leitura.dados

    chave = RE_CHAVE.search(texto)
    d['chave_acesso'] = chave.group(1) if chave else None
    municipio = re.search(r'Munic[ií]pio:\s*([^\n]+?)(?:\s{2,}|$)', texto)
    leitura.informativos['municipio_emissor'] = municipio.group(1).strip() if municipio else None
    d['numero_nfse'] = somente_digitos(g.valor('NÚMERO DA NFS-E')) or None
    competencia = data_hora(g.valor('COMPETÊNCIA DA NFS-E'))
    d['competencia_nfse'] = competencia.date().isoformat() if competencia else None
    emissao = data_hora(g.valor('DATA E HORA DA EMISSÃO DA NFS-E'))
    d['data_emissao'] = emissao.isoformat() if emissao else None

    _ler_participantes(g, d)
    _ler_valores(g, leitura)
    return leitura


def _ler_participantes(g: Grade, d: dict) -> None:
    ini_prest = g.indice('PRESTADOR/FORNECEDOR')
    ini_tom = g.indice('TOMADOR/ADQUIRENTE')
    fim_tom = g.indice('SERVIÇO PRESTADO') or g.indice('INTERMEDIÁRIO DA OPERAÇÃO')
    if ini_prest is not None:
        d['prestador_cnpj'] = somente_digitos(g.valor('CNPJ/CPF/NIF', ini_prest, ini_tom)) or None
        d['prestador_inscricao_municipal'] = g.valor('Indicador Municipal', ini_prest, ini_tom)
        d['prestador_telefone'] = g.valor('Telefone', ini_prest, ini_tom)
        d['prestador_razao_social'] = g.valor('Nome/Nome Empresarial', ini_prest, ini_tom)
        d['prestador_municipio'] = g.valor('Município/Sigla UF', ini_prest, ini_tom)
        ibge = g.valor('Código IBGE/CEP', ini_prest, ini_tom) or ''
        d['prestador_codigo_municipio'] = ibge.split('/')[0].strip() or None
        d['prestador_cep'] = somente_digitos(ibge.split('/')[1]) if '/' in ibge else None
        d['prestador_endereco'] = g.valor('Endereço', ini_prest, ini_tom)
        d['prestador_email'] = g.valor('E-mail', ini_prest, ini_tom)
    if ini_tom is not None:
        d['tomador_cnpj'] = somente_digitos(g.valor('CNPJ/CPF/NIF', ini_tom, fim_tom)) or None
        d['tomador_razao_social'] = g.valor('Nome/Nome Empresarial', ini_tom, fim_tom)

    d['codigo_tributacao'] = g.valor('Código de Tributação')
    d['codigo_municipio_servico'] = g.valor('Local da Prestação')
    d['discriminacao'] = g.bloco_apos('Descrição do Serviço', ('TRIBUTAÇÃO MUNICIPAL', 'TRIBUTAÇÃO FEDERAL'))


def _ler_valores(g: Grade, leitura: LeituraDanfse) -> None:
    d = leitura.dados
    retencao_iss = _normalizar(g.valor('Retenção do ISSQN') or '')
    iss = dinheiro(g.valor('ISSQN Apurado'))
    retido = 'RETIDO' in retencao_iss and 'NÃO' not in retencao_iss and 'NAO' not in retencao_iss
    d['iss_retido'] = '1' if retido else '2'
    d['valor_iss'] = iss
    d['valor_iss_retido'] = iss if retido else Decimal('0')
    d['base_calculo'] = dinheiro(g.valor('BC ISSQN'))
    aliquota = g.valor('Alíquota Aplicada')
    d['aliquota'] = aliquota.replace(',', '.') if aliquota and re.fullmatch(r'[\d.,]+', aliquota) else None
    d['valor_ir'] = dinheiro(g.valor('IRRF')) or Decimal('0')
    d['valor_inss'] = dinheiro(g.valor('Contribuição Previdenciária - Retida')) or Decimal('0')
    # O DANFSe soma PIS/COFINS/CSLL retidos em "Contribuições Sociais - Retidas" (a CSRF);
    # fica no campo da CSLL, como o ISS Fortaleza faz no XML.
    d['valor_csll'] = dinheiro(g.valor('Contribuições Sociais - Retidas')) or Decimal('0')
    d['valor_pis'] = Decimal('0')
    d['valor_cofins'] = Decimal('0')
    d['outras_retencoes'] = Decimal('0')
    leitura.informativos['pis_apuracao_propria'] = dinheiro(g.valor('PIS - Débito Apura'))
    leitura.informativos['cofins_apuracao_propria'] = dinheiro(g.valor('COFINS - Débito Apura'))

    d['valor_servicos'] = dinheiro(g.valor('VALOR DA OPERAÇÃO/SERVIÇO'))
    ini_totais = g.indice('VALOR DA OPERAÇÃO/SERVIÇO') or 0
    d['desconto_incondicionado'] = dinheiro(g.valor('Desconto Incondicionado', ini_totais)) or Decimal('0')
    d['desconto_condicionado'] = dinheiro(g.valor('Desconto Condicionado', ini_totais)) or Decimal('0')
    d['valor_deducoes'] = Decimal('0')
    total_retencoes = dinheiro(g.valor('Total das Retenções')) or Decimal('0')
    d['valor_liquido_nfse'] = dinheiro(g.valor('VALOR LÍQUIDO DA NFS-e', ini_totais))
    leitura.informativos['total_retencoes'] = total_retencoes
    _conferir(leitura, total_retencoes)


def _conferir(leitura: LeituraDanfse, total_retencoes: Decimal) -> None:
    d, avisos = leitura.dados, leitura.avisos
    faltando = [nome for campo, nome in (
        ('numero_nfse', 'número'), ('data_emissao', 'data de emissão'), ('prestador_cnpj', 'CNPJ do prestador'),
        ('tomador_cnpj', 'CNPJ do tomador'), ('valor_servicos', 'valor do serviço'), ('valor_liquido_nfse', 'valor líquido'),
    ) if not d.get(campo)]
    if faltando:
        avisos.append('Não foi possível ler: ' + ', '.join(faltando) + '. Confira e preencha.')
    chave = d.get('chave_acesso')
    if chave:
        if d.get('prestador_cnpj') and chave[9:23] != d['prestador_cnpj']:
            avisos.append('O CNPJ do prestador não confere com a chave de acesso.')
        if d.get('numero_nfse') and int(chave[23:36]) != int(d['numero_nfse']):
            avisos.append('O número da NFS-e não confere com a chave de acesso.')
    else:
        avisos.append('Chave de acesso não encontrada no PDF.')
    servicos, liquido = d.get('valor_servicos'), d.get('valor_liquido_nfse')
    if servicos is not None and liquido is not None:
        retencoes = sum((d.get(k) or Decimal('0')) for k in ('valor_iss_retido', 'valor_ir', 'valor_inss', 'valor_csll'))
        if abs(retencoes - total_retencoes) > TOLERANCIA:
            avisos.append(f'As retenções lidas (R$ {retencoes:.2f}) não fecham com o total do DANFSe (R$ {total_retencoes:.2f}).')
        esperado = servicos - (d.get('desconto_incondicionado') or 0) - (d.get('desconto_condicionado') or 0) - total_retencoes
        if abs(esperado - liquido) > TOLERANCIA:
            avisos.append(f'O valor líquido (R$ {liquido:.2f}) não fecha com serviços − descontos − retenções (R$ {esperado:.2f}).')


def ler_danfse(conteudo: bytes) -> LeituraDanfse:
    if not conteudo.startswith(b'%PDF'):
        raise DanfseIlegivel('Envie a NFS-e em PDF.')
    texto = _texto_layout(conteudo)
    if len(texto.strip()) < TEXTO_MINIMO:
        return LeituraDanfse(False, avisos=['O PDF é uma imagem (escaneado); não há texto para ler. Preencha os dados da nota.'])
    return ler_danfse_texto(texto)
