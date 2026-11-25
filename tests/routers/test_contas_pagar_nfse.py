# ruff: noqa: PLR2004, E501
from datetime import date, datetime, timezone
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
    # timestamptz no Postgres: 02:30 UTC de 11/11 ainda é 10/11 no horário do hospital.
    'data_hora': datetime(2026, 11, 11, 2, 30, tzinfo=timezone.utc), 'competencia_nfse': '2026-11-01',
    'valor_servicos': '1500.50', 'valor_liquido_nfse': '1400.00',
    'tomador_cnpj': '08.711.085/0001-28', 'tomador_razao_social': 'Hospital',
    'discriminacao': 'Licença',
    'situacao_xml': None, 'situacao_portal': 'NORMAL', 'situacao_consultada_em': None,
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
    postgres = [[True], [True], [NOTA_TI, NOTA_NOVA, NOTA_LANCADA], REGRAS]
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
    assert por_chave['11111111000111-0000123']['data_emissao'] == '2026-11-10'
    assert por_chave['22222222000122-9']['situacao'] == 'a_classificar'
    assert 'sem entradas' in por_chave['22222222000122-9']['motivo']
    # nr_documento '055' no MV casa com a NFS-e '55' (zeros à esquerda).
    assert por_chave['33333333000133-55']['situacao'] == 'lancada'
    assert por_chave['33333333000133-55']['mv']['cd_ent_serv'] == 900
    assert postgres.executions[2][1] == {'desde': '2026-11-01'}


def test_filtro_padrao_mostra_so_pendentes_agrupadas_por_oficina():
    postgres, oracle = _sessoes()
    resposta = rota.listar_recebidas(_usuario(perfil='ti'), postgres, oracle, situacao='pendente')

    assert [i['chave'] for i in resposta['itens']] == ['11111111000111-0000123']
    # Sem a oficina no MV, o grupo usa o nome da oficina da regra.
    assert resposta['oficinas'] == [{
        'cd_setor': None, 'nm_setor': 'TECNOLOGIA DA INFORMACAO',
        'cd_oficina': 2, 'ds_oficina': 'TECNOLOGIA DA INFORMACAO',
        'oficinas': ['TECNOLOGIA DA INFORMACAO'],
        'cd_multi_empresa': 1, 'empresa': 'HOSPITAL',
        'quantidade': 1, 'valor': Decimal('1500.50'),
    }]
    assert resposta['itens'][0]['empresa'] == 'HOSPITAL'


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


def test_data_emissao_aceita_timestamptz_texto_e_nulo():
    assert rota._data_emissao(datetime(2026, 11, 17, 12, 0, tzinfo=timezone.utc)) == '2026-11-17'
    assert rota._data_emissao('2026-11-05T10:00:00') == '2026-11-05'
    assert rota._data_emissao(None) is None


def test_filtro_por_data_usa_timestamp():
    assert "AT TIME ZONE 'America/Sao_Paulo')::date >= CAST(:desde AS date)" in str(rota.SQL_NFSE_RECEBIDAS)
    assert 'left(coalesce(data_hora' not in str(rota.SQL_NFSE_RECEBIDAS)


def test_nota_lancada_sem_regra_nao_entra_no_grupo_a_classificar():
    sem_regra_lancada = {**NOTA_TI, 'prestador_cnpj': '44444444000144', 'numero_nfse': '7'}
    lancadas = LANCADAS_MV + [{'cnpj': '44444444000144', 'nr_documento': '7', 'cd_ent_serv': 1,
                               'cd_con_pag': None, 'origem': 'ENT_SERV'}]
    postgres = FakeSession([[True], [True], [NOTA_NOVA, sem_regra_lancada], REGRAS])
    oracle = FakeSession([lancadas, [HOSPITAL]])

    resposta = rota.listar_recebidas(_usuario(perfil='ti'), postgres, oracle, situacao='todas')

    grupos = {g['ds_oficina']: g['quantidade'] for g in resposta['oficinas']}
    assert grupos == {rota.GRUPO_A_CLASSIFICAR: 1, rota.GRUPO_SEM_SETOR_LANCADA: 1}


def test_oficinas_homonimas_de_empresas_diferentes_ficam_separadas():
    pa = {'cd_multi_empresa': 2, 'ds_multi_empresa': 'PRONTO ATENDIMENTO', 'cnpj': '05613278000158'}
    nota_pa = {**NOTA_TI, 'prestador_cnpj': '55555555000155', 'numero_nfse': '8',
               'tomador_cnpj': '05613278000158'}
    nota_hosp = {**NOTA_TI, 'prestador_cnpj': '66666666000166', 'numero_nfse': '9'}
    regra = {**REGRAS[0], 'ds_oficina': 'CENTRAL DE RECEBIMENTO FISCAL'}
    regras = [
        {**regra, 'cnpj': '55555555000155', 'cd_multi_empresa': 2, 'cd_oficina': 12},
        {**regra, 'cnpj': '66666666000166', 'cd_multi_empresa': 1, 'cd_oficina': 5},
    ]
    postgres = FakeSession([[True], [True], [nota_pa, nota_hosp], regras])
    oracle = FakeSession([[], [HOSPITAL, pa]])

    resposta = rota.listar_recebidas(_usuario(perfil='ti'), postgres, oracle, situacao='todas')

    assert [(g['ds_oficina'], g['empresa']) for g in resposta['oficinas']] == [
        ('CENTRAL DE RECEBIMENTO FISCAL', 'HOSPITAL'),
        ('CENTRAL DE RECEBIMENTO FISCAL', 'PRONTO ATENDIMENTO'),
    ]


# ------------------------------------------------------------ setor do MV

OFICINAS_MV = [
    {'cd_oficina': 1, 'ds_oficina': 'MANUTENÇÃO E INFRAESTRUTURA', 'cd_setor': 43, 'nm_setor': 'MANUTENCAO PREDIAL', 'cd_multi_empresa': 1},
    {'cd_oficina': 4, 'ds_oficina': 'OBRAS', 'cd_setor': 43, 'nm_setor': 'MANUTENCAO PREDIAL', 'cd_multi_empresa': 1},
    {'cd_oficina': 5, 'ds_oficina': 'CENTRAL DE RECEBIMENTO FISCAL', 'cd_setor': 7, 'nm_setor': 'CONTABILIDADE', 'cd_multi_empresa': 1},
    {'cd_oficina': 12, 'ds_oficina': 'CENTRAL DE RECEBIMENTO FISCAL', 'cd_setor': 68, 'nm_setor': 'CONTABILIDADE', 'cd_multi_empresa': 2},
]
PA = {'cd_multi_empresa': 2, 'ds_multi_empresa': 'PRONTO ATENDIMENTO', 'cnpj': '05613278000158'}


def _cenario_setores():
    notas = [
        {**NOTA_TI, 'prestador_cnpj': '41000000000141', 'numero_nfse': '1', 'valor_servicos': '100'},
        {**NOTA_TI, 'prestador_cnpj': '44000000000144', 'numero_nfse': '2', 'valor_servicos': '50'},
        {**NOTA_TI, 'prestador_cnpj': '55000000000155', 'numero_nfse': '3', 'valor_servicos': '10'},
        {**NOTA_TI, 'prestador_cnpj': '12000000000112', 'numero_nfse': '4', 'valor_servicos': '5',
         'tomador_cnpj': '05613278000158'},
    ]
    regras = [
        {**REGRAS[0], 'cnpj': '41000000000141', 'cd_oficina': 1, 'ds_oficina': 'MANUTENÇÃO E INFRAESTRUTURA'},
        {**REGRAS[0], 'cnpj': '44000000000144', 'cd_oficina': 4, 'ds_oficina': 'OBRAS'},
        {**REGRAS[0], 'cnpj': '55000000000155', 'cd_oficina': 5, 'ds_oficina': 'CENTRAL DE RECEBIMENTO FISCAL'},
        {**REGRAS[0], 'cnpj': '12000000000112', 'cd_multi_empresa': 2, 'cd_oficina': 12,
         'ds_oficina': 'CENTRAL DE RECEBIMENTO FISCAL'},
    ]
    return (FakeSession([[True], [True], notas, regras]),
            FakeSession([[], [HOSPITAL, PA], OFICINAS_MV]))


def test_nota_mostra_o_setor_do_mv_da_oficina():
    resposta = rota.listar_recebidas(_usuario(perfil='ti'), *_cenario_setores(), situacao='todas')

    por_numero = {i['numero_nfse']: i for i in resposta['itens']}
    assert (por_numero['1']['cd_setor'], por_numero['1']['nm_setor']) == (43, 'MANUTENCAO PREDIAL')
    assert (por_numero['2']['cd_setor'], por_numero['2']['nm_setor']) == (43, 'MANUTENCAO PREDIAL')
    assert por_numero['3']['nm_setor'] == 'CONTABILIDADE'
    # A oficina continua na nota: é ela que vai para a entrada de serviço no MV.
    assert (por_numero['2']['cd_oficina'], por_numero['2']['ds_oficina']) == (4, 'OBRAS')


def test_oficinas_do_mesmo_setor_ficam_no_mesmo_grupo_e_empresas_separadas():
    resposta = rota.listar_recebidas(_usuario(perfil='ti'), *_cenario_setores(), situacao='todas')

    grupos = [(g['cd_setor'], g['nm_setor'], g['empresa'], g['quantidade'], g['valor'], g['oficinas'])
              for g in resposta['oficinas']]
    assert grupos == [
        (7, 'CONTABILIDADE', 'HOSPITAL', 1, Decimal('10'), ['CENTRAL DE RECEBIMENTO FISCAL']),
        (68, 'CONTABILIDADE', 'PRONTO ATENDIMENTO', 1, Decimal('5'), ['CENTRAL DE RECEBIMENTO FISCAL']),
        (43, 'MANUTENCAO PREDIAL', 'HOSPITAL', 2, Decimal('150'), ['MANUTENÇÃO E INFRAESTRUTURA', 'OBRAS']),
    ]


def test_filtro_por_setor_traz_todas_as_oficinas_do_setor():
    resposta = rota.listar_recebidas(_usuario(perfil='ti'), *_cenario_setores(), situacao='todas', cd_setor=43)

    assert sorted(i['numero_nfse'] for i in resposta['itens']) == ['1', '2']


def test_filtro_por_oficina_continua_valendo():
    resposta = rota.listar_recebidas(_usuario(perfil='ti'), *_cenario_setores(), situacao='todas', cd_oficina=4)

    assert [i['numero_nfse'] for i in resposta['itens']] == ['2']


def test_consulta_de_oficinas_traz_o_nome_do_setor():
    sql = str(rota.SQL_OFICINAS)
    assert 's.nm_setor AS nm_setor' in sql
    assert 'LEFT JOIN dbamv.setor s ON s.cd_setor = o.cd_setor' in sql


# ------------------------------------------------------------ situação no ISS


def test_status_nfse_prioriza_cancelamento_do_xml():
    assert rota.status_nfse('CANCELADA', 'NORMAL') == 'CANCELADA'
    assert rota.status_nfse(None, ' cancelada ') == 'CANCELADA'
    assert rota.status_nfse(None, None) is None
    assert rota.nfse_cancelada('SUBSTITUIDA') is True
    assert rota.nfse_cancelada('NORMAL') is False
    assert rota.nfse_cancelada(None) is False


def test_nota_cancelada_no_portal_sai_de_a_lancar_e_aparece_em_canceladas():
    cancelada = {**NOTA_TI, 'numero_nfse': '388581', 'situacao_portal': 'CANCELADA'}

    def sessoes():
        return (FakeSession([[True], [True], [NOTA_TI, cancelada], REGRAS]),
                FakeSession([[], [HOSPITAL]]))

    pendentes = rota.listar_recebidas(_usuario(perfil='ti'), *sessoes(), desde=date(2026, 11, 1))
    assert [i['numero_nfse'] for i in pendentes['itens']] == ['0000123']
    assert pendentes['itens'][0]['status_nfse'] == 'NORMAL'

    canceladas = rota.listar_recebidas(_usuario(perfil='ti'), *sessoes(), situacao='cancelada',
                                       desde=date(2026, 11, 1))
    assert [(i['numero_nfse'], i['situacao'], i['status_nfse']) for i in canceladas['itens']] == [
        ('388581', 'cancelada', 'CANCELADA')]


def test_nota_lancada_continua_lancada_mesmo_cancelada_e_mostra_status():
    lancada_cancelada = {**NOTA_LANCADA, 'situacao_portal': 'CANCELADA'}
    postgres = FakeSession([[True], [True], [lancada_cancelada], REGRAS])
    resposta = rota.listar_recebidas(_usuario(perfil='ti'), postgres, FakeSession([LANCADAS_MV, [HOSPITAL]]),
                                     situacao='todas')

    assert resposta['itens'][0]['situacao'] == 'lancada'
    assert resposta['itens'][0]['status_nfse'] == 'CANCELADA'


def test_sem_tabela_de_situacao_usa_consulta_sem_join():
    postgres = FakeSession([[True], [False], [{**NOTA_TI, 'situacao_portal': None}], REGRAS])
    resposta = rota.listar_recebidas(_usuario(perfil='ti'), postgres, FakeSession([[], [HOSPITAL]]))

    assert 'nfse_xml_tomador_situacao' not in postgres.executions[2][0]
    assert resposta['itens'][0]['status_nfse'] is None
    assert resposta['itens'][0]['situacao'] == 'pendente'


def test_join_da_situacao_normaliza_cnpj():
    sql = str(rota.SQL_NFSE_RECEBIDAS)
    assert 'LEFT JOIN api_prontocardio.nfse_xml_tomador_situacao s' in sql
    assert "regexp_replace(n.prestador_cnpj, '\\D', '', 'g')" in sql


# ------------------------------------------------ usuário de setor x regra do MV

LANCADA_POR = [
    {**LANCADAS_MV[0], 'cd_usuario': 'rh000938'},
]


def _sessoes_setor(login='RH000938', oficinas_mv=(2,), enviadas=(), lancadas=None):
    """Fila de um usuário de setor: oficinas da regra do MV pelo login vinculado."""
    postgres = FakeSession([
        [True], [True], [NOTA_TI, NOTA_NOVA, NOTA_LANCADA], REGRAS,
        [],                                   # oficinas concedidas no Receita Certa
        [login] if login else [],             # login MV vinculado
        [{'prestador_cnpj': c, 'numero_nfse': n} for c, n in enviadas],
    ])
    oracle_respostas = ([[{'cd_oficina': o} for o in oficinas_mv]] if login else []) + [
        LANCADA_POR if lancadas is None else lancadas, [HOSPITAL],
    ]
    return postgres, FakeSession(oracle_respostas)


def test_setor_ve_pendentes_pelas_oficinas_da_regra_do_mv():
    postgres, oracle = _sessoes_setor(oficinas_mv=(2,))
    resposta = rota.listar_recebidas(
        _usuario(telas=[rota.TELA_SETOR]), postgres, oracle, situacao='pendente'
    )

    assert [i['chave'] for i in resposta['itens']] == ['11111111000111-0000123']
    assert oracle.executions[0][1] == {'cd_usuario': 'RH000938'}
    assert "tp_funcao <> 'SS'" in oracle.executions[0][0]


def test_setor_sem_oficina_na_regra_nao_ve_pendentes():
    postgres, oracle = _sessoes_setor(oficinas_mv=(9,))
    resposta = rota.listar_recebidas(
        _usuario(telas=[rota.TELA_SETOR]), postgres, oracle, situacao='pendente'
    )

    assert resposta['itens'] == []


def test_historico_mostra_so_o_que_o_usuario_lancou_com_a_oficina_atual():
    # Lançada por ele, mesmo sem a oficina na regra atual (reorganização).
    postgres, oracle = _sessoes_setor(oficinas_mv=(9,))
    resposta = rota.listar_recebidas(
        _usuario(telas=[rota.TELA_SETOR]), postgres, oracle, situacao='lancada'
    )

    assert [i['chave'] for i in resposta['itens']] == ['33333333000133-55']
    item = resposta['itens'][0]
    assert item['ds_oficina'] == 'TECNOLOGIA DA INFORMACAO'  # oficina atual da regra
    assert item['mv']['lancado_por'] == ['RH000938']


def test_historico_nao_mostra_lancada_por_outro_usuario_da_mesma_oficina():
    postgres, oracle = _sessoes_setor(
        oficinas_mv=(2,), lancadas=[{**LANCADAS_MV[0], 'cd_usuario': 'RH000111'}]
    )
    resposta = rota.listar_recebidas(
        _usuario(telas=[rota.TELA_SETOR]), postgres, oracle, situacao='lancada'
    )

    assert resposta['itens'] == []


def test_historico_inclui_nota_com_boleto_enviado_pelo_usuario():
    postgres, oracle = _sessoes_setor(
        login=None, lancadas=[{**LANCADAS_MV[0], 'cd_usuario': 'RH000111'}],
        enviadas=[('33333333000133', '55')],
    )
    resposta = rota.listar_recebidas(
        _usuario(telas=[rota.TELA_SETOR]), postgres, oracle, situacao='lancada'
    )

    assert [i['chave'] for i in resposta['itens']] == ['33333333000133-55']


def test_gestor_nao_consulta_regra_do_mv():
    assert rota.acesso_do_usuario(FakeSession([]), FakeSession([]), _usuario(perfil='ti')) is None
