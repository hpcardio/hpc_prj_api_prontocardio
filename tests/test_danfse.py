# ruff: noqa: PLR2004, E501
from decimal import Decimal
from pathlib import Path

import pytest

from app_prontocardio.services.danfse import (
    Grade,
    dinheiro,
    ler_danfse,
    ler_danfse_texto,
)
from app_prontocardio.services.nfse_manuais import (
    FONTE_NFSE_RECEBIDAS,
    validar,
)
from app_prontocardio.services.payables_boleto import cnpj_valido

FIXTURES = Path(__file__).parent / 'fixtures'
SEM_RETENCAO = (FIXTURES / 'danfse_sem_retencao.txt').read_text()  # Odontoart 3199621 (ISS Fortaleza, DANFSe v2.0)
COM_RETENCAO = (FIXTURES / 'danfse_com_retencao.txt').read_text()  # Multimédicos 6631: ISS 3%, IRRF, CSRF retidos


def test_danfse_sem_retencao_bate_com_o_xml_do_iss():
    leitura = ler_danfse_texto(SEM_RETENCAO)
    d = leitura.dados
    assert leitura.reconhecido is True
    assert leitura.avisos == []
    assert d['numero_nfse'] == '3199621'
    assert d['chave_acesso'] == '23044001203187913000157000000319962126110375518921'
    assert d['prestador_cnpj'] == '03187913000157'
    assert d['prestador_razao_social'] == 'ODONTOART PLANOS ODONTOLOGICOS LTDA'
    assert d['prestador_codigo_municipio'] == '2304400'
    assert d['tomador_cnpj'] == '05613278000158'
    assert d['data_emissao'] == '2026-11-16T10:45:47'
    assert d['valor_servicos'] == Decimal('173.42')
    assert d['valor_liquido_nfse'] == Decimal('173.42')
    assert d['iss_retido'] == '2'
    assert (d['valor_ir'], d['valor_csll'], d['valor_inss'], d['valor_iss_retido']) == (0, 0, 0, 0)
    assert d['discriminacao'].startswith('TRATAMENTO ODONTOLOGICO')
    assert leitura.informativos['pis_apuracao_propria'] == Decimal('1.13')  # informativo, não é retenção


def test_danfse_com_retencoes_iss_ir_e_csrf():
    d = ler_danfse_texto(COM_RETENCAO).dados
    assert d['valor_servicos'] == Decimal('18551.40')
    assert d['valor_liquido_nfse'] == Decimal('16853.95')
    assert d['iss_retido'] == '1'
    assert d['valor_iss_retido'] == Decimal('556.54')
    assert d['aliquota'] == '3.00'
    assert d['valor_ir'] == Decimal('278.27')
    assert d['valor_csll'] == Decimal('862.64')  # CSRF (Contribuições Sociais - Retidas), como no XML do ISS
    assert d['valor_inss'] == 0
    assert (d['valor_pis'], d['valor_cofins']) == (0, 0)


def test_coluna_com_traco_nao_puxa_o_valor_da_vizinha():
    g = Grade('IRRF      INSS - Retida      Contribuições Sociais - Retidas\n'
              'R$ 1,00   -                  R$ 9,99\n')
    assert g.valor('INSS - Retida') is None
    assert g.valor('Contribuições Sociais - Retidas') == 'R$ 9,99'


def test_chave_que_nao_confere_gera_aviso():
    texto = SEM_RETENCAO.replace('3199621', '3199999', 1)  # número na grade != número da chave
    assert any('não confere com a chave' in a for a in ler_danfse_texto(texto).avisos)


def test_pdf_fora_do_padrao_nacional_vira_preenchimento_manual():
    leitura = ler_danfse_texto('NOTA FISCAL DE SERVIÇOS ELETRÔNICA - PREFEITURA DE SÃO PAULO\nValor R$ 100,00')
    assert leitura.reconhecido is False
    assert 'Preencha os dados' in leitura.avisos[0]


def test_arquivo_que_nao_e_pdf():
    with pytest.raises(ValueError, match='PDF'):
        ler_danfse(b'GIF89a')


@pytest.mark.parametrize(('texto', 'valor'), [('R$ 18.551,40', Decimal('18551.40')), ('-', None), ('3,00', Decimal('3.00')), ('-R$ 173,42', Decimal('-173.42'))])
def test_dinheiro(texto, valor):
    assert dinheiro(texto) == valor


# ------------------------------------------------------------- validação

BASE = {'numero_nfse': '123', 'data_emissao': '2026-11-10T10:00:00', 'prestador_cnpj': '63554067000198',
        'prestador_razao_social': 'X', 'tomador_cnpj': '05613278000158', 'discriminacao': 'Serviço',
        'valor_servicos': Decimal('1000'), 'valor_liquido_nfse': Decimal('939.50'), 'iss_retido': '1',
        'valor_iss_retido': Decimal('30'), 'valor_ir': Decimal('15'), 'valor_csll': Decimal('15.50')}
GRUPO = {'05613278000158', '08711085000128'}


def test_validacao_aceita_nota_que_fecha():
    assert validar(BASE, cnpjs_do_grupo=GRUPO, cnpj_valido=cnpj_valido) == []


@pytest.mark.parametrize(('troca', 'trecho'), [
    ({'tomador_cnpj': '11222333000181'}, 'tomador não é uma empresa do grupo'),
    ({'prestador_cnpj': '11111111000111'}, 'CNPJ do prestador não é válido'),
    ({'valor_liquido_nfse': Decimal('950')}, 'não fecha'),
    ({'numero_nfse': None}, 'número da NFS-e'),
    ({'iss_retido': '1', 'valor_iss_retido': Decimal('0'), 'valor_liquido_nfse': Decimal('969.50')}, 'ISS marcado como retido'),
])
def test_validacao_recusa(troca, trecho):
    assert any(trecho in e for e in validar({**BASE, **troca}, cnpjs_do_grupo=GRUPO, cnpj_valido=cnpj_valido))


def test_fonte_une_iss_e_cadastradas_por_pdf():
    assert 'FROM api_prontocardio.nfse_xml_tomador UNION ALL' in FONTE_NFSE_RECEBIDAS
    assert "contas_pagar_nfse_manuais WHERE status = 'ativa'" in FONTE_NFSE_RECEBIDAS
    assert "NULL::text AS xml_documento" in FONTE_NFSE_RECEBIDAS
