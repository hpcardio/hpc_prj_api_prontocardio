# ruff: noqa: PLR2004
from decimal import Decimal

from app_prontocardio.services.payables_oficinas import (
    STATUS_A_CLASSIFICAR,
    STATUS_DEFINIDA,
    situacao_da_nota,
    sugerir_regras,
)

CNPJ = '12.345.678/0001-90'


def _linha(cd_oficina, ds_oficina, qtd, empresa=1, cnpj=CNPJ, cd_fornecedor=10):
    return {
        'cnpj': cnpj,
        'cd_fornecedor': cd_fornecedor,
        'nm_fornecedor': 'Fornecedor X',
        'cd_multi_empresa': empresa,
        'cd_oficina': cd_oficina,
        'ds_oficina': ds_oficina,
        'qtd': qtd,
    }


def test_oficina_dominante_vira_regra_definida():
    regras = sugerir_regras([_linha(2, 'TI', 9), _linha(5, 'CENTRAL', 1)])

    assert len(regras) == 1
    regra = regras[0]
    assert regra.status == STATUS_DEFINIDA
    assert regra.cnpj == '12345678000190'
    assert regra.cd_oficina == 2
    assert regra.qtd_entradas == 10
    assert regra.participacao == Decimal('0.9')
    assert '9 de 10 entradas (90%)' in regra.motivo


def test_historico_dividido_fica_a_classificar_com_motivo():
    regras = sugerir_regras([_linha(7, 'MARKETING', 5), _linha(8, 'CAFA', 5)])

    regra = regras[0]
    assert regra.status == STATUS_A_CLASSIFICAR
    assert regra.cd_oficina is None
    assert 'CAFA 50%' in regra.motivo
    assert 'MARKETING 50%' in regra.motivo


def test_abaixo_do_limiar_fica_a_classificar():
    regras = sugerir_regras([_linha(1, 'MANUTENCAO', 7), _linha(4, 'OBRAS', 3)])

    assert regras[0].status == STATUS_A_CLASSIFICAR


def test_oficinas_homonimas_da_mesma_empresa_sao_um_setor():
    # Empresa 2 tem três "CENTRAL DE RECEBIMENTO FISCAL" (12, 22 e 23).
    regras = sugerir_regras([
        _linha(12, 'CENTRAL DE RECEBIMENTO FISCAL', 5, empresa=2),
        _linha(22, 'CENTRAL DE RECEBIMENTO FISCAL', 4, empresa=2),
        _linha(23, 'Central de  Recebimento Fiscal', 1, empresa=2),
    ])

    regra = regras[0]
    assert regra.status == STATUS_DEFINIDA
    assert regra.cd_oficina == 12  # o código mais usado entre os homônimos
    assert regra.participacao == Decimal('1')


def test_regra_e_separada_por_empresa():
    regras = sugerir_regras([
        _linha(2, 'TI', 3, empresa=1),
        _linha(12, 'CENTRAL', 3, empresa=2),
    ])

    assert {(r.cd_multi_empresa, r.cd_oficina) for r in regras} == {(1, 2), (2, 12)}


def test_cadastros_duplicados_do_mesmo_cnpj_sao_somados():
    regras = sugerir_regras([
        _linha(2, 'TI', 2, cd_fornecedor=10),
        _linha(2, 'TI', 5, cd_fornecedor=11, cnpj='12345678000190'),
    ])

    assert len(regras) == 1
    assert regras[0].qtd_entradas == 7
    assert regras[0].cd_fornecedor == 11  # cadastro com mais entradas


def test_ignora_cpf_e_linhas_sem_oficina():
    regras = sugerir_regras([
        _linha(2, 'TI', 3, cnpj='123.456.789-00'),
        {**_linha(2, 'TI', 3), 'cd_oficina': None},
    ])

    assert regras == []


def test_situacao_da_nota():
    definida = {'status': STATUS_DEFINIDA, 'cd_oficina': 2, 'motivo': 'ok'}
    pendente = {'status': STATUS_A_CLASSIFICAR, 'cd_oficina': None, 'motivo': 'dividido'}

    assert situacao_da_nota(definida, 1) == (STATUS_DEFINIDA, 'ok')
    assert situacao_da_nota(pendente, 1) == (STATUS_A_CLASSIFICAR, 'dividido')
    assert situacao_da_nota(None, 1)[0] == STATUS_A_CLASSIFICAR
    assert 'não é uma empresa' in situacao_da_nota(definida, None)[1]
