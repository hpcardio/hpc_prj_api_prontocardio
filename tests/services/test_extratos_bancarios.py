# ruff: noqa: PLR2004, E501
"""Leitores de extrato com textos sintéticos no layout de cada banco.

Nenhum extrato real vai para o repositório: as linhas abaixo reproduzem o
formato (colunas, sinais, quebras) com valores inventados.
"""

import zipfile
from datetime import date
from decimal import Decimal
from io import BytesIO

import pytest

from app_prontocardio.services import extratos_bancarios as eb

BB = """
       Cliente - Conta atual
       Agência             2917-3
       Conta corrente      211433-X   EMPRESA TESTE
       Período do          de 18 / 11 / 2026 até 19 / 11 / 2026
       Lançamentos
        Dt. balancete      Dt.       Ag. origem       Lote    Histórico                    Documento                Valor R$              Sald
        17/11/2026                     0000         00000     Saldo Anterior                                          100,00 C              100,00 C
        18/11/2026                     0000         14175     TED-Crédito em Conta                100.622.754        1.000,00 C
                                                              341 0262 FORNECEDOR CREDITO
        18/11/2026                     0000         13113     Pagamento de Boleto                 811.000.123          250,50 D
                                                              FORNECEDOR XYZ
        18/11/2026                     0000         13113     Tar DOC/TED Eletrônico                                    13,60 D
https  ://autoatendimento.bb.c  om.br/index.html
11/19/26, 8:06 AM                                       Banco do Brasil
        19/11/2026                     0000         00000     S A L D O                                                                  835,90 C
"""


def _linhas(texto):
    return texto.strip('\n').splitlines()


def test_bb_le_lancamentos_continuacao_e_confere_saldo():
    extrato = eb.ler_bb(_linhas(BB))

    assert (extrato.banco, extrato.agencia, extrato.conta, extrato.conta_dv) == (1, '2917', '211433', 'X')
    assert (extrato.periodo_inicio, extrato.periodo_fim) == (date(2026, 11, 18), date(2026, 11, 19))
    assert extrato.saldo_inicial == Decimal('100.00')
    assert extrato.saldo_final == Decimal('835.90')
    assert extrato.conferido is True
    assert [linha.valor for linha in extrato.linhas] == [Decimal('1000.00'), Decimal('-250.50'), Decimal('-13.60')]
    assert extrato.linhas[1].documento == '811.000.123'
    assert extrato.linhas[1].historico == 'Pagamento de Boleto FORNECEDOR XYZ'
    # Cabeçalho/rodapé da impressão não entram no histórico.
    assert 'http' not in extrato.linhas[2].historico


def test_bb_saldo_anterior_com_prefixo_e_conta_sem_movimento():
    texto = """
       Agência              2917-3
       Conta corrente       59138-6   EMPRESA
       Período do           de 18 / 11 / 2026 até 19 / 11 / 2026
        17/11/2026                         0000               00000       000 Saldo Anterior                                   263,29 C
        Saldo                                                                                                                263,29 C
                     *** A CONTA NAO FOI MOVIMENTADA ***
"""
    extrato = eb.ler_bb(_linhas(texto))

    assert extrato.linhas == []
    assert extrato.saldo_inicial == extrato.saldo_final == Decimal('263.29')
    assert extrato.conferido is True


def test_bb_com_saldo_cortado_entra_com_aviso():
    texto = BB.replace('835,90 C', '')
    texto = texto.replace('100,00 C              100,00 C', '')
    extrato = eb.ler_bb(_linhas(texto))

    assert extrato.conferido is False
    assert 'não traz os saldos' in extrato.avisos[0]
    assert len(extrato.linhas) == 3


def test_extrato_que_nao_fecha_e_recusado():
    with pytest.raises(eb.ExtratoInvalido, match='não fecha'):
        eb.ler_bb(_linhas(BB.replace('835,90 C', '835,91 C')))


BRADESCO = """
                                       Extrato (Últimos Lançamentos)
    Extrato de: Ag: 00564 | CC: 0030612-6
     Data              Lançamento                                         Dcto.               Crédito (R$)                Débito (R$)                  Saldo (R$)
    17/11/2026        SALDO ANTERIOR                                                                                                                       1.000,00
    19/11/2026        PGTO.SINISTRO AP/CERTIF.                           4375253                      500,00                                             1.500,00
                      SEGURADORA TESTE
                      TARIFA BANCARIA                                    250926                                                     -9,80             1.490,20
                      TRANSF PGTO PIX
    Total                                                                                           500,00                         -9,80                1.490,20
"""


def test_bradesco_herda_data_e_confere_linha_a_linha():
    extrato = eb.ler_bradesco(_linhas(BRADESCO))

    assert (extrato.banco, extrato.agencia, extrato.conta, extrato.conta_dv) == (237, '564', '30612', '6')
    assert [(linha.data, linha.valor) for linha in extrato.linhas] == [
        (date(2026, 11, 19), Decimal('500.00')),
        (date(2026, 11, 19), Decimal('-9.80')),
    ]
    assert extrato.linhas[1].historico == 'TARIFA BANCARIA TRANSF PGTO PIX'
    assert extrato.saldo_final == Decimal('1490.20')
    assert extrato.conferido is True


def test_bradesco_com_saldo_impresso_errado_e_recusado():
    with pytest.raises(eb.ExtratoInvalido, match='não fecha no lançamento'):
        eb.ler_bradesco(_linhas(BRADESCO.replace('1.490,20\n', '1.490,21\n', 1)))


BNB = """
      Titular: EMPRESA TESTE LTDA
                                    Agência/Conta Corrente: 265 - MARANGUAPE/000028564-0
      Saldo Anterior: R$ 0,00                                 Período: 14/11/2026 até 19/11/2026
                                         Detalhamento do Extrato
             Data                  Histórico            Documento         Valor R$         Saldo R$
        18/11/2026     RESGATE FUNDOS               52647            6.269,72        6.269,72
                       AUTOMATICO
        19/11/2026     PAGAMENTO ENCARGOS           43808216         - 6.269,72           0,00
                                          Detalhamento do Saldo
"""


def test_bnb_le_valores_negativos_com_espaco():
    extrato = eb.ler_bnb(_linhas(BNB))

    assert (extrato.banco, extrato.agencia, extrato.conta) == (4, '265', '28564')
    assert [linha.valor for linha in extrato.linhas] == [Decimal('6269.72'), Decimal('-6269.72')]
    assert extrato.linhas[0].historico == 'RESGATE FUNDOS AUTOMATICO'
    assert extrato.conferido is True


CEF = """
        Conta:     4249 | 1292 | 000577494098-1
        Data Mov.         Nr. Doc.           Histórico                                Valor                               Saldo
                          000000             SALDO ANTERIOR                            0,00                              100,00 C
        11/11/2026        111252             CRED TEV                              1.000,00 C                          1.100,00 C
        11/11/2026        369639             DB CX CAP                               418,57 D                            681,43 C
        11/11/2026        000000             SALDO DIA                                 0,00 C                            681,43 C
"""


def test_cef_ignora_saldo_dia_e_confere():
    extrato = eb.ler_cef(_linhas(CEF))

    assert (extrato.banco, extrato.agencia, extrato.conta, extrato.conta_dv) == (104, '4249', '577494098', '1')
    assert [linha.valor for linha in extrato.linhas] == [Decimal('1000.00'), Decimal('-418.57')]
    assert extrato.saldo_inicial == Decimal('100.00')
    assert extrato.conferido is True


UNICRED = """
    Período de 12/11/2026 a 19/11/2026                         Coop: 60 - AG: 6010 - Conta: 225363
    Saldo em 11/11/2026: R$ 46.988,05
    Data              Lançamentos                                     Valor (R$)                     Saldo (R$)
    13/11/2026        PAGAMENTO TITULO                                -R$ 1.000,00                   -R$ 47.988,05
    Saldo no final do período                                                                         -R$ 47.988,05
"""


def test_unicred_deduz_saldo_inicial_e_confere_linha():
    extrato = eb.ler_unicred(_linhas(UNICRED))

    assert (extrato.banco, extrato.agencia, extrato.conta, extrato.conta_dv) == (136, '6010', '22536', '3')
    assert extrato.linhas[0].valor == Decimal('-1000.00')
    assert extrato.saldo_inicial == Decimal('-46988.05')
    assert extrato.conferido is True


def _xlsx(linhas):
    compartilhadas = []

    def celula(coluna, linha, valor):
        ref = f'{chr(64 + coluna)}{linha}'
        if isinstance(valor, str):
            compartilhadas.append(valor)
            return f'<c r="{ref}" t="s"><v>{len(compartilhadas) - 1}</v></c>'
        return f'<c r="{ref}"><v>{valor}</v></c>'

    corpo = ''.join(
        f'<row r="{i}">' + ''.join(celula(j, i, v) for j, v in enumerate(linha, start=1)) + '</row>'
        for i, linha in enumerate(linhas, start=1)
    )
    ns = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
    saida = BytesIO()
    with zipfile.ZipFile(saida, 'w') as pacote:
        pacote.writestr('xl/worksheets/sheet1.xml', f'<worksheet xmlns="{ns}"><sheetData>{corpo}</sheetData></worksheet>')
        pacote.writestr('xl/sharedStrings.xml', f'<sst xmlns="{ns}">' + ''.join(f'<si><t>{s}</t></si>' for s in compartilhadas) + '</sst>')
    return saida.getvalue()


def test_santander_xlsx_inverte_ordem_e_confere():
    conteudo = _xlsx([
        ['AGENCIA', '0700', 'CONTA', '130006564'],
        ['Data', 'Histórico', 'Documento', 'Valor (R$)', 'Saldo (R$)'],
        ['26/10/2026', 'PIX ENVIADO FORNECEDOR', '', -9481.07, 10.45],
        ['16/10/2026', 'PIX RECEBIDO', '04161034', 9589.89, 9491.52],
    ])
    extrato = eb.ler_extrato(conteudo, '05 SANTANDER.xlsx')

    assert (extrato.banco, extrato.agencia, extrato.conta, extrato.conta_dv) == (33, '700', '13000656', '4')
    assert [linha.data for linha in extrato.linhas] == [date(2026, 10, 16), date(2026, 10, 26)]
    assert extrato.saldo_inicial == Decimal('-98.37')
    assert extrato.conferido is True


@pytest.mark.parametrize(
    ('conteudo', 'nome', 'mensagem'),
    [
        (b'%PDF-1.4', '05 BB APLICAÇÃO OUT.pdf', 'aplicação'),
        (b'texto qualquer', 'extrato.txt', 'Envie o PDF'),
        (b'PK\x03\x04lixo', '08 BB.xlsx', 'XLSX é aceito só no Santander'),
    ],
)
def test_arquivos_fora_do_escopo_tem_mensagem_clara(conteudo, nome, mensagem):
    with pytest.raises(eb.ExtratoInvalido, match=mensagem):
        eb.ler_extrato(conteudo, nome)


def test_pdf_do_santander_e_recusado_por_vir_cortado(monkeypatch):
    monkeypatch.setattr(eb, '_linhas_pdf', lambda conteudo: ['  S  a  n  t  a  n  d  e  r Em p  re  s a  ria  l'])
    with pytest.raises(eb.ExtratoInvalido, match='XLSX'):
        eb.ler_extrato(b'%PDF-1.4', '05 SANTANDER.pdf')


def test_banco_detectado_pelo_conteudo(monkeypatch):
    monkeypatch.setattr(eb, '_linhas_pdf', lambda conteudo: _linhas(BRADESCO))
    assert eb.ler_extrato(b'%PDF-1.4', 'qualquer.pdf').banco == eb.BANCO_BRADESCO
