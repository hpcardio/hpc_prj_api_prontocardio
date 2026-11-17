# ruff: noqa: PLR2004, E501
from datetime import date, timedelta
from decimal import Decimal
from io import BytesIO

import pytest
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

from app_prontocardio.services.payables_boleto import (
    BASE_FATOR,
    BoletoInvalido,
    ler_boleto_pdf,
    ler_boleto_texto,
    ler_linha_digitavel,
    modulo10,
    modulo11_bancario,
    vencimento_do_fator,
)


def gerar_linha_bancaria(banco='341', valor=Decimal('1500.50'), fator=1234, livre='1' * 25) -> str:
    """Monta uma linha digitável válida (47 dígitos) a partir dos dados do código de barras."""
    sem_dv = f'{banco}9{fator:04d}{int(valor * 100):010d}{livre}'
    dv = modulo11_bancario(sem_dv)
    codigo = sem_dv[:4] + str(dv) + sem_dv[4:]
    c1 = codigo[0:4] + codigo[19:24]
    c2 = codigo[24:34]
    c3 = codigo[34:44]
    return f'{c1}{modulo10(c1)}{c2}{modulo10(c2)}{c3}{modulo10(c3)}{codigo[4]}{codigo[5:19]}'


def formatar(linha: str) -> str:
    return f'{linha[0:5]}.{linha[5:10]} {linha[10:15]}.{linha[15:21]} {linha[21:26]}.{linha[26:32]} {linha[32]} {linha[33:]}'


def gerar_linha_arrecadacao(valor=Decimal('250.00')) -> str:
    # 8 = arrecadação, 2 = saneamento, 6 = valor efetivo com DV módulo 10
    sem_dv = f'826{int(valor * 100):011d}' + '0' * 29
    dv = modulo10(sem_dv)
    codigo = sem_dv[:3] + str(dv) + sem_dv[3:]
    blocos = [codigo[i:i + 11] for i in range(0, 44, 11)]
    return ''.join(b + str(modulo10(b)) for b in blocos)


def gerar_pdf(*linhas_texto: str) -> bytes:
    buffer = BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4)
    y = 800
    for texto in linhas_texto:
        pdf.drawString(40, y, texto)
        y -= 18
    pdf.save()
    return buffer.getvalue()


def test_linha_bancaria_valida_traz_banco_valor_e_vencimento():
    linha = gerar_linha_bancaria(fator=2000)
    lido = ler_linha_digitavel(formatar(linha), date(2026, 11, 17))

    assert lido.tipo == 'bancario'
    assert lido.banco == '341'
    assert lido.valor == Decimal('1500.50')
    assert len(lido.codigo_barras) == 44


def test_fator_de_vencimento_considera_o_reinicio_de_2025():
    # Após 21/02/2025 o fator voltou para 1000 (= 22/02/2025).
    assert vencimento_do_fator(1000, date(2026, 11, 17)) == date(2025, 2, 22)
    # Fator 1000 lido em 2000 ainda é o ciclo antigo (03/07/2000).
    assert vencimento_do_fator(1000, date(2000, 7, 1)) == BASE_FATOR + timedelta(days=1000)
    hoje = date(2026, 11, 17)
    fator_hoje = (hoje - date(2025, 2, 22)).days + 1000
    assert vencimento_do_fator(fator_hoje + 10, hoje) == hoje + timedelta(days=10)


# 3, 12 e 25 ficam nos campos protegidos por módulo 10; 32 é o DV geral.
# O campo de valor (33-46) só é protegido pelo DV geral, cujo módulo 11 mapeia
# os restos 0, 10 e 11 para 1: algumas trocas ali são indetectáveis por regra.
@pytest.mark.parametrize('posicao', [3, 12, 25, 32])
def test_digito_errado_e_rejeitado(posicao):
    linha = list(gerar_linha_bancaria())
    linha[posicao] = str((int(linha[posicao]) + 1) % 10)
    with pytest.raises(BoletoInvalido):
        ler_linha_digitavel(''.join(linha), date(2026, 11, 17))


def test_linha_de_arrecadacao_traz_valor_sem_vencimento():
    lido = ler_linha_digitavel(gerar_linha_arrecadacao(), date(2026, 11, 17))

    assert lido.tipo == 'arrecadacao'
    assert lido.valor == Decimal('250.00')
    assert lido.vencimento is None


def test_leitura_do_pdf_encontra_linha_cnpj_nosso_numero_e_valor_impresso():
    hoje = date(2026, 11, 17)
    fator = (hoje - date(2025, 2, 22)).days + 1000 + 15
    linha = gerar_linha_bancaria(fator=fator)
    pdf = gerar_pdf(
        'Beneficiario: SOFTWARE SA  CNPJ: 11.111.111/0001-11',
        'Nosso Numero: 109/00012345-6',
        'Valor do Documento',
        '1500,50',
        formatar(linha),
    )

    lido = ler_boleto_pdf(pdf, hoje)

    assert lido.linha_digitavel == linha
    assert lido.vencimento == hoje + timedelta(days=15)
    assert lido.valor_impresso == Decimal('1500.50')
    assert '11111111000111' in lido.cnpjs_no_texto
    assert lido.nosso_numero.startswith('109/00012345-6')


def test_pdf_sem_linha_pede_digitacao():
    with pytest.raises(BoletoInvalido, match='Informe-a manualmente'):
        ler_boleto_pdf(gerar_pdf('Boleto escaneado sem texto util'), date(2026, 11, 17))


def test_arquivo_que_nao_e_pdf():
    with pytest.raises(BoletoInvalido, match='não é um PDF'):
        ler_boleto_pdf(b'GIF89a...', date(2026, 11, 17))


def test_primeira_linha_valida_vence_quando_ha_lixo_antes():
    linha = gerar_linha_bancaria()
    invalida = list(linha)
    invalida[5] = str((int(invalida[5]) + 3) % 10)
    texto = formatar(''.join(invalida)) + '\n' + formatar(linha)

    assert ler_boleto_texto(texto, date(2026, 11, 17)).linha_digitavel == linha
