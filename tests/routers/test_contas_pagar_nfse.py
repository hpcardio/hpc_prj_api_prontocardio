# ruff: noqa: PLR2004
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app_prontocardio.routers import contas_pagar_nfse as rota


class FakeResult:
    def __init__(self, rows):
        self.rows = rows

    def mappings(self):
        return self

    def all(self):
        return self.rows

    def first(self):
        return self.rows[0] if self.rows else None

    def scalars(self):
        return iter(self.rows)

    def scalar_one(self):
        return self.rows[0]

    def scalar_one_or_none(self):
        return self.rows[0] if self.rows else None


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.executions = []
        self.commits = 0

    def execute(self, statement, params=None):
        self.executions.append((str(statement), params))
        return FakeResult(self.responses.pop(0) if self.responses else [])

    def commit(self):
        self.commits += 1


def _usuario(perfil='usuario', telas=(), uid=7):
    return SimpleNamespace(id=uid, nome='Ana', perfil=perfil, telas_permitidas=list(telas))


HOSPITAL = {'cd_multi_empresa': 1, 'ds_multi_empresa': 'HOSPITAL', 'cnpj': '08711085000128'}
NOTA_TI = {
    'prestador_cnpj': '11111111000111', 'prestador_razao_social': 'Software SA',
    'numero_nfse': '0000123', 'codigo_verificacao_nfse': 'ABC',
    'data_hora': '2026-11-10T09:00:00', 'competencia_nfse': '2026-11-01',
    'valor_servicos': '1500.50', 'valor_liquido_nfse': '1400.00',
    'tomador_cnpj': '08.711.085/0001-28', 'tomador_razao_social': 'Hospital',
    'discriminacao': 'Licença',
}
NOTA_NOVA = {**NOTA_TI, 'prestador_cnpj': '22222222000122', 'numero_nfse': '9'}
NOTA_LANCADA = {**NOTA_TI, 'prestador_cnpj': '33333333000133', 'numero_nfse': '55'}
REGRAS = [
    {'cnpj': '11111111000111', 'cd_multi_empresa': 1, 'cd_fornecedor': 1, 'nm_fornecedor': 'Software SA',
     'cd_oficina': 2, 'ds_oficina': 'TECNOLOGIA DA INFORMACAO', 'status': 'definida', 'origem': 'historico',
     'qtd_entradas': 9, 'participacao': Decimal('1'), 'motivo': '9 de 9', 'usuario_id': 1, 'atualizado_em': None},
    {'cnpj': '33333333000133', 'cd_multi_empresa': 1, 'cd_fornecedor': 3, 'nm_fornecedor': 'Lançada',
     'cd_oficina': 2, 'ds_oficina': 'TECNOLOGIA DA INFORMACAO', 'status': 'definida', 'origem': 'historico',
     'qtd_entradas': 2, 'participacao': Decimal('1'), 'motivo': '2 de 2', 'usuario_id': 1, 'atualizado_em': None},
]
LANCADAS_MV = [
    {'cnpj': '33333333000133', 'nr_documento': '055', 'cd_ent_serv': 900, 'cd_con_pag': None, 'origem': 'ENT_SERV'},
]


def _sessoes(permitidas=None):
    postgres = [[True], [NOTA_TI, NOTA_NOVA, NOTA_LANCADA], REGRAS]
    if permitidas is not None:
        postgres.append(permitidas)
    return FakeSession(postgres), FakeSession([LANCADAS_MV, [HOSPITAL]])


def test_acesso_fila_exige_tela_ou_gestao():
    with pytest.raises(HTTPException):
        rota.acesso_fila(_usuario())
    assert rota.acesso_fila(_usuario(telas=[rota.TELA_SETOR]))
    assert rota.acesso_fila(_usuario(perfil='ti'))


def test_acesso_regras_so_para_financeiro():
    with pytest.raises(HTTPException):
        rota.acesso_regras(_usuario(telas=[rota.TELA_SETOR]))
    assert rota.acesso_regras(_usuario(telas=[rota.TELA_REGRAS]))


def test_recebidas_sem_tabela_retorna_aviso():
    postgres = FakeSession([[False]])
    resposta = rota.listar_recebidas(_usuario(perfil='ti'), postgres, FakeSession([]))

    assert resposta['itens'] == []
    assert 'ainda não carregou' in resposta['aviso']


def test_gestor_ve_pendentes_a_classificar_e_lancadas():
    postgres, oracle = _sessoes()
    resposta = rota.listar_recebidas(
        _usuario(perfil='ti'), postgres, oracle, situacao='todas', desde=date(2026, 11, 1)
    )

    por_chave = {i['chave']: i for i in resposta['itens']}
    assert por_chave['11111111000111-0000123']['situacao'] == 'pendente'
    assert por_chave['11111111000111-0000123']['cd_oficina'] == 2
    assert por_chave['11111111000111-0000123']['cd_multi_empresa'] == 1
    assert por_chave['11111111000111-0000123']['valor_servicos'] == Decimal('1500.50')
    assert por_chave['22222222000122-9']['situacao'] == 'a_classificar'
    assert 'sem entradas' in por_chave['22222222000122-9']['motivo']
    # nr_documento '055' no MV casa com a NFS-e '55' (zeros à esquerda).
    assert por_chave['33333333000133-55']['situacao'] == 'lancada'
    assert por_chave['33333333000133-55']['mv']['cd_ent_serv'] == 900
    assert postgres.executions[1][1] == {'desde': '2026-11-01'}


def test_filtro_padrao_mostra_so_pendentes_agrupadas_por_oficina():
    postgres, oracle = _sessoes()
    resposta = rota.listar_recebidas(_usuario(perfil='ti'), postgres, oracle, situacao='pendente')

    assert [i['chave'] for i in resposta['itens']] == ['11111111000111-0000123']
    assert resposta['oficinas'] == [{
        'cd_oficina': 2, 'ds_oficina': 'TECNOLOGIA DA INFORMACAO',
        'quantidade': 1, 'valor': Decimal('1500.50'),
    }]


def test_setor_ve_so_as_proprias_oficinas_e_nao_ve_a_classificar():
    postgres, oracle = _sessoes(permitidas=[5])
    resposta = rota.listar_recebidas(
        _usuario(telas=[rota.TELA_SETOR]), postgres, oracle, situacao='todas'
    )

    assert resposta['itens'] == []


def test_setor_da_oficina_ve_a_nota():
    postgres, oracle = _sessoes(permitidas=[2])
    resposta = rota.listar_recebidas(
        _usuario(telas=[rota.TELA_SETOR]), postgres, oracle, situacao='pendente'
    )

    assert [i['chave'] for i in resposta['itens']] == ['11111111000111-0000123']


def test_classificacao_manual_valida_empresa_da_oficina():
    oracle = FakeSession([[{'cd_oficina': 12, 'ds_oficina': 'CENTRAL', 'cd_setor': 68, 'cd_multi_empresa': 2}]])
    with pytest.raises(HTTPException) as erro:
        rota.classificar_fornecedor(
            1, '11.111.111/0001-11', rota.ClassificacaoInput(cd_oficina=12),
            _usuario(perfil='ti'), FakeSession([]), oracle,
        )
    assert 'outra empresa' in erro.value.detail


def test_classificacao_manual_grava_regra_manual():
    oracle = FakeSession([[{'cd_oficina': 2, 'ds_oficina': 'TI', 'cd_setor': 1, 'cd_multi_empresa': 1}]])
    postgres = FakeSession([[], REGRAS])
    regra = rota.classificar_fornecedor(
        1, '11.111.111/0001-11', rota.ClassificacaoInput(cd_oficina=2, observacao='contrato'),
        _usuario(perfil='ti'), postgres, oracle,
    )

    sql, params = postgres.executions[0]
    assert "'manual'" in sql
    assert 'ON CONFLICT (cnpj, cd_multi_empresa)' in sql
    assert params['cnpj'] == '11111111000111'
    assert params['cd_multi_empresa'] == 1
    assert params['ds_oficina'] == 'TI'
    assert 'contrato' in params['motivo']
    assert postgres.commits == 1
    assert regra['cd_oficina'] == 2


def test_recalcular_nao_sobrescreve_regras_manuais():
    historico = [{
        'cnpj': '11111111000111', 'cd_fornecedor': 1, 'nm_fornecedor': 'X', 'cd_multi_empresa': 1,
        'cd_oficina': 2, 'ds_oficina': 'TI', 'qtd': 4,
    }]
    postgres = FakeSession([])
    resultado = rota.recalcular_regras(_usuario(perfil='ti'), postgres, FakeSession([historico]))

    sql, params = postgres.executions[0]
    assert "WHERE api_prontocardio.contas_pagar_regras_oficina.origem <> 'manual'" in sql
    assert params['status'] == 'definida'
    assert resultado == {'fornecedores': 1, 'definida': 1, 'a_classificar': 0, 'meses_historico': 24}


def test_vinculo_usuario_mv_rejeita_login_ja_usado():
    oracle = FakeSession([[{'cd_usuario': 'RH000969', 'nm_usuario': 'WIARA', 'sn_ativo': 'S'}]])
    postgres = FakeSession([[99]])
    with pytest.raises(HTTPException) as erro:
        rota.vincular_usuario_mv(7, rota.UsuarioMvInput(cd_usuario_mv='rh000969'),
                                 _usuario(perfil='ti'), postgres, oracle)
    assert erro.value.status_code == 409


def test_vinculo_usuario_mv_grava_login_em_maiusculas():
    oracle = FakeSession([[{'cd_usuario': 'RH000969', 'nm_usuario': 'WIARA', 'sn_ativo': 'S'}]])
    vinculo = {'usuario_id': 7, 'cd_usuario_mv': 'RH000969', 'nm_usuario_mv': 'WIARA',
               'vinculado_por': 1, 'vinculado_em': None}
    postgres = FakeSession([[], [], [vinculo]])
    resultado = rota.vincular_usuario_mv(7, rota.UsuarioMvInput(cd_usuario_mv='rh000969'),
                                         _usuario(perfil='ti', uid=1), postgres, oracle)

    assert oracle.executions[0][1] == {'cd_usuario': 'RH000969'}
    assert postgres.executions[1][1]['cd'] == 'RH000969'
    assert resultado['cd_usuario_mv'] == 'RH000969'


def test_vinculo_usuario_mv_inexistente():
    with pytest.raises(HTTPException) as erro:
        rota.vincular_usuario_mv(7, rota.UsuarioMvInput(cd_usuario_mv='XX1'),
                                 _usuario(perfil='ti'), FakeSession([]), FakeSession([[]]))
    assert 'não existe' in erro.value.detail
