# ruff: noqa: PLR2004, E501, PT018
import re
from contextlib import contextmanager
from datetime import date, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app_prontocardio.routers import contas_pagar_boletos as rota
from app_prontocardio.services.contas_pagar_importacao import (
    ImportacaoFalhou,
    executar_importacao,
    simular_importacao,
)

AGORA = datetime(2026, 11, 18, 10, 30)

NOTA = {
    'prestador_cnpj': '63554067000198', 'prestador_razao_social': 'HAPVIDA', 'numero_nfse': '93125822',
    'valor_servicos': '2708.73', 'valor_liquido_nfse': '2708.73', 'data_emissao': date(2026, 11, 1),
    'cd_multi_empresa': 2, 'cd_oficina': 22,
}
BOLETO = {'id': 4, 'valor': Decimal('2708.73'), 'vencimento': date(2026, 11, 25), 'divergencias': []}
MODELO = {
    'cd_ent_serv': 12292, 'cd_fornecedor': 3373, 'nr_documento': '5.949', 'nr_serie': 'A',
    'dt_entrada': datetime(2026, 10, 29), 'cd_usuario': 'RH001185', 'cd_tip_doc': 11,
    'tp_documento_entrada': 'S', 'tp_entrega': 'T', 'cd_cfop': 400, 'sn_atualiza_preco': 'S',
    'cd_justifica': 6, 'tp_documento_ntfs': 'D', 'tp_tributacao_ntfs': 'O', 'tp_iss_retido_ntfs': 'I',
    'cd_modelo_entrada': '03', 'cd_id_cprb': 0, 'tp_status_sped_terceiros': 'N', 'sn_fechado': 'N',
    'it_cd_servico': 10106, 'qt_itens': 1,
}


# Recorte real de TIP_DETALHE/FAIXA_DETALHE (MV de produção, 18/11/2026).
TIPOS_IMPOSTO = [
    {'cd_detalhamento': 2, 'ds_detalhamento': 'IRRF - P. JURÍDICA', 'tp_detalhamento': 'J', 'tp_data': 'E', 'vl_percentual': Decimal('1.5'), 'qt_uso': 300},
    {'cd_detalhamento': 10, 'ds_detalhamento': 'IRRF - PJ GERAL 1%', 'tp_detalhamento': 'J', 'tp_data': 'E', 'vl_percentual': Decimal('1'), 'qt_uso': 20},
    {'cd_detalhamento': 3, 'ds_detalhamento': 'ISS - TERCEIROS 2%', 'tp_detalhamento': 'I', 'tp_data': 'E', 'vl_percentual': Decimal('2'), 'qt_uso': 50},
    {'cd_detalhamento': 9, 'ds_detalhamento': 'ISS - TERCEIROS 5%', 'tp_detalhamento': 'I', 'tp_data': 'E', 'vl_percentual': Decimal('5'), 'qt_uso': 120},
    {'cd_detalhamento': 14, 'ds_detalhamento': 'ISS - TERCEIROS 3%', 'tp_detalhamento': 'I', 'tp_data': 'E', 'vl_percentual': Decimal('3'), 'qt_uso': 200},
    {'cd_detalhamento': 11, 'ds_detalhamento': 'CSRF RETIDA 4,65%', 'tp_detalhamento': 'S', 'tp_data': 'E', 'vl_percentual': Decimal('4.65'), 'qt_uso': 250},
    {'cd_detalhamento': 7, 'ds_detalhamento': 'CSLL', 'tp_detalhamento': 'L', 'tp_data': 'P', 'vl_percentual': Decimal('1'), 'qt_uso': 1},
]


def _respostas(**trocas):
    base = {
        'config': [{'empresa': 2, 'tp_compartilhamento': '4', 'usuario': 'RH000938'}],
        'fornecedores': [{'cd_fornecedor': 288, 'nm_fornecedor': 'HAPVIDA LTDA', 'cd_cidade': 6553},
                         {'cd_fornecedor': 3373, 'nm_fornecedor': 'HAPVIDA', 'cd_cidade': 6553}],
        'modelo': [MODELO],
        'oficina': [{'cd_oficina': 22, 'ds_oficina': 'CENTRAL DE RECEBIMENTO FISCAL', 'cd_setor': 68, 'cd_multi_empresa': 2}],
        'forn_conta': [{'cd_reduzido': 241, 'cd_historico_padrao': 51}],
        'servico': [{'cd_servico': 10106, 'nm_servico': 'PLANO DE SAUDE', 'cd_reduzido': 699, 'cd_item_res': 99, 'na_oficina': 1}],
        'duplicidade': [],
        'detalhamentos': [],
        'obrigatorios': [{'table_name': 'ENT_SERV', 'coluna': 'cd_ent_serv'}, {'table_name': 'ENT_SERV', 'coluna': 'sn_fechado'},
                         {'table_name': 'DUPLICATA', 'coluna': 'vl_parcela'}],
        'titulo': [{'cd_con_pag': 108700}],
        'indicadores': [{'sn_importada_fnfi': 'S', 'cd_con_pag': 108700}],
        'parcelas_titulo': [{'qt_parcelas': 1, 'vl_parcelas': Decimal('2708.73')}],
        'titulos_imposto': [],
        'tipos_imposto': TIPOS_IMPOSTO,
        'impostos_fornecedor': [],
    }
    base.update(trocas)
    return base


class FakeVar:
    def __init__(self):
        self.valor = None

    def getvalue(self):
        return self.valor


class FakeCursor:
    """Responde pela etiqueta /* rc:x */; sequences, UPDATE e a função de importação à parte."""

    def __init__(self, respostas, importa=1, rowcount=1):
        self.respostas = respostas
        self.importa = importa
        self.executados = []
        self.description = []
        self._linhas = []
        self.rowcount = rowcount
        self._rowcount = rowcount
        self.sequences = {'seq_ent_serv': 12500, 'seq_itent_serv': 12600}

    def var(self, tipo):
        return FakeVar()

    def execute(self, sql, binds=None):
        self.executados.append((sql, binds))
        if 'fnc_importa_mges_fnfi_serv' in sql:
            if isinstance(self.importa, Exception):
                raise self.importa
            binds['resultado'].valor = self.importa
            return
        sequencia = re.search(r'dbamv\.(\w+)\.nextval', sql)
        if sequencia:
            self.description, self._linhas = [('NEXTVAL',)], [(self.sequences[sequencia.group(1)],)]
            return
        self.rowcount = self._rowcount if sql.startswith('update') else 1
        etiqueta = re.search(r'/\* rc:(\w+) \*/', sql)
        linhas = self.respostas.get(etiqueta.group(1), []) if etiqueta else []
        colunas = list(linhas[0]) if linhas else ['x']
        self.description = [(c.upper(),) for c in colunas]
        self._linhas = [tuple(linha[c] for c in colunas) for linha in linhas]

    def fetchall(self):
        return self._linhas


def _simular(nota=NOTA, boletos=(BOLETO,), **trocas):
    cursor = FakeCursor(_respostas(**trocas))
    return simular_importacao(cursor, nota=dict(nota), boletos=list(boletos), agora=AGORA), cursor


def _codigos(resultado):
    return [p['codigo'] for p in resultado['pendencias']]


def test_nota_sem_retencao_fica_apta_e_monta_as_gravacoes():
    resultado, cursor = _simular()

    assert resultado['apta'] is True, resultado['pendencias']
    assert cursor.executados[0] == ('begin dbamv.pkg_mv2000.atribui_empresa(:empresa); end;', {'empresa': 2})
    tabelas = [(g['tabela'], g['operacao']) for g in resultado['gravacoes']]
    assert tabelas == [('ENT_SERV', 'INSERT'), ('ITENT_SERV', 'INSERT'), ('DOCUMENTO_ENTRADA', 'INSERT'),
                       ('DUPLICATA', 'INSERT'), ('DOCUMENTO_ENTRADA', 'UPDATE'), ('ENT_SERV_CONCLUSAO', 'INSERT')]
    ent = resultado['gravacoes'][0]['campos']
    assert ent['cd_fornecedor']['valor'] == 3373  # o do último lançamento, não o 288
    assert ent['cd_oficina']['valor'] == 22
    assert ent['nr_serie']['valor'] == 'A'
    assert ent['vl_total']['valor'] == Decimal('2708.73')
    assert ent['cd_usuario']['valor'] == 'RH000938'
    assert resultado['gravacoes'][1]['campos']['cd_servico']['valor'] == 10106
    dup = resultado['gravacoes'][3]['campos']
    assert (dup['cd_parcela']['valor'], dup['vl_parcela']['valor'], dup['dt_vencimento']['valor']) == (1, Decimal('2708.73'), date(2026, 11, 25))
    assert resultado['gravacoes'][4]['campos']['vl_saldo_documento']['valor'] == Decimal('0')
    assert resultado['importacao']['parametros'] == {'p_cd_fornecedor': 3373, 'p_nr_documento': '93125822',
                                                     'p_nr_serie': 'A', 'p_cd_multi_empresa': 2}
    assert resultado['importacao']['titulo_previsto']['cd_reduzido'] == 241
    assert any('2 cadastros' in a for a in resultado['avisos'])


def test_imposto_configurado_no_mv_bloqueia():
    resultado, _ = _simular(detalhamentos=[{'origem': 'fornecedor', 'cd_detalhamento': 11,
                                            'ds_detalhamento': 'CSRF', 'tp_detalhamento': 'C'}])
    assert 'impostos' in _codigos(resultado)


def test_titulo_ja_existente_no_financeiro():
    resultado, _ = _simular(duplicidade=[{'tabela': 'CON_PAG', 'chave': '108550', 'nr_serie': 'A'}])
    assert '-20001' in _codigos(resultado)


@pytest.mark.parametrize(('tp', 'bloqueia'), [('4', True), ('2', True), ('1', False)])
def test_conta_contabil_do_fornecedor_depende_do_compartilhamento(tp, bloqueia):
    resultado, _ = _simular(config=[{'empresa': 2, 'tp_compartilhamento': tp, 'usuario': 'RH000938'}], forn_conta=[])
    assert ('-20003' in _codigos(resultado)) is bloqueia


def test_servico_sem_contas_e_fora_da_oficina():
    resultado, _ = _simular(servico=[{'cd_servico': 10106, 'nm_servico': 'X', 'cd_reduzido': None,
                                      'cd_item_res': None, 'na_oficina': 0}])
    assert {'servico', '-20007', '-20008'} <= set(_codigos(resultado))


def test_oficina_sem_setor():
    resultado, _ = _simular(oficina=[{'cd_oficina': 22, 'ds_oficina': 'X', 'cd_setor': None, 'cd_multi_empresa': 2}])
    assert '-20006' in _codigos(resultado)


def test_sem_historico_com_dois_cadastros_nao_escolhe_fornecedor():
    resultado, _ = _simular(modelo=[])
    assert _codigos(resultado) == ['fornecedor']
    assert resultado['gravacoes'] == []


def test_fornecedor_nao_cadastrado():
    resultado, _ = _simular(fornecedores=[])
    assert _codigos(resultado) == ['fornecedor']


def test_boletos_somando_diferente_do_liquido_e_vencido():
    resultado, _ = _simular(boletos=[{**BOLETO, 'valor': Decimal('2000.00'), 'vencimento': date(2026, 11, 17)}])
    mensagens = ' '.join(p['mensagem'] for p in resultado['pendencias'])
    assert 'soma dos boletos' in mensagens
    assert 'venceu' in mensagens


def test_sem_boleto():
    resultado, _ = _simular(boletos=[])
    assert '-20002' in _codigos(resultado)


def test_campo_obrigatorio_sem_valor():
    resultado, _ = _simular(obrigatorios=[{'table_name': 'ENT_SERV', 'coluna': 'cd_cno'}])
    assert 'ENT_SERV.CD_CNO' in resultado['pendencias'][0]['mensagem']


def test_duas_parcelas_em_ordem_de_vencimento():
    boletos = [{**BOLETO, 'id': 9, 'valor': Decimal('1000.00'), 'vencimento': date(2026, 12, 25)},
               {**BOLETO, 'id': 8, 'valor': Decimal('1708.73'), 'vencimento': date(2026, 11, 25)}]
    resultado, _ = _simular(boletos=boletos)
    dups = [g['campos'] for g in resultado['gravacoes'] if g['tabela'] == 'DUPLICATA']
    assert [(d['cd_parcela']['valor'], d['vl_parcela']['valor']) for d in dups] == [(1, Decimal('1708.73')), (2, Decimal('1000.00'))]
    assert resultado['apta'] is True


# ------------------------------------------------------------- gravação


def test_assinatura_ignora_a_hora_mas_nao_os_valores():
    a, _ = _simular()
    b = simular_importacao(FakeCursor(_respostas()), nota=dict(NOTA), boletos=[BOLETO], agora=datetime(2026, 11, 18, 11, 59))
    c, _ = _simular(boletos=[{**BOLETO, 'vencimento': date(2026, 11, 26)}])
    assert a['assinatura'] == b['assinatura']
    assert a['assinatura'] != c['assinatura']


def test_executa_o_plano_e_chama_a_funcao_de_importacao():
    resultado, _ = _simular()
    cursor = FakeCursor(_respostas())

    ids = executar_importacao(cursor, resultado)

    assert ids == {'ent_serv': 12500, 'itent_serv': 12600, 'cd_con_pag': 108700}
    sqls = [sql for sql, _ in cursor.executados]
    inserts = [re.match(r'insert into dbamv\.(\w+)', sql).group(1) for sql in sqls if sql.startswith('insert')]
    assert inserts == ['ent_serv', 'itent_serv', 'documento_entrada', 'duplicata', 'ent_serv_conclusao']
    ent = next(b for sql, b in cursor.executados if sql.startswith('insert into dbamv.ent_serv '))
    assert ent['cd_ent_serv'] == 12500 and ent['cd_fornecedor'] == 3373 and ent['cd_usuario'] == 'RH000938'
    assert 'cd_ord_com' not in ent  # campos sem valor ficam fora do INSERT
    item = next(b for sql, b in cursor.executados if sql.startswith('insert into dbamv.itent_serv '))
    assert (item['cd_itent_serv'], item['cd_ent_serv']) == (12600, 12500)
    conclusao = next((sql, b) for sql, b in cursor.executados if 'ent_serv_conclusao' in sql)
    assert 'sysdate' in conclusao[0] and conclusao[1] == {'cd_ent_serv': 12500, 'cd_usuario': 'RH000938'}
    chamada = next(b for sql, b in cursor.executados if 'fnc_importa' in sql)
    assert {k: chamada[k] for k in ('fornecedor', 'documento', 'serie', 'empresa')} == {
        'fornecedor': 3373, 'documento': '93125822', 'serie': 'A', 'empresa': 2}
    assert not any('commit' in sql or 'rollback' in sql for sql in sqls)


def test_nota_bloqueada_nao_grava():
    resultado, _ = _simular(duplicidade=[{'tabela': 'CON_PAG', 'chave': '1', 'nr_serie': 'A'}])
    cursor = FakeCursor(_respostas())
    with pytest.raises(ImportacaoFalhou):
        executar_importacao(cursor, resultado)
    assert cursor.executados == []


@pytest.mark.parametrize(('trocas', 'kw', 'mensagem'), [
    ({}, {'importa': 0}, 'retornou falso'),
    ({}, {'rowcount': 0}, 'não foi encontrado'),
    ({'titulo': []}, {}, 'encontrados 0'),
    ({'indicadores': [{'sn_importada_fnfi': 'N', 'cd_con_pag': None}]}, {}, 'marcada como importada'),
])
def test_falhas_depois_dos_inserts_levantam_erro(trocas, kw, mensagem):
    resultado, _ = _simular()
    with pytest.raises(ImportacaoFalhou, match=mensagem):
        executar_importacao(FakeCursor(_respostas(**trocas), **kw), resultado)


def test_plano_com_coluna_estranha_e_recusado():
    resultado, _ = _simular()
    resultado['gravacoes'][0]['campos']['x; drop table y'] = {'valor': 1, 'origem': 'x'}
    with pytest.raises(ImportacaoFalhou, match='Coluna inválida'):
        executar_importacao(FakeCursor(_respostas()), resultado)


def test_mensagem_oracle_nao_traz_o_sql():
    exc = Exception('ORA-20003: Fornecedor não possui conta contabil !3373\nORA-06512: at line 4\nSQL: insert ...')
    assert rota.mensagem_oracle(exc) == 'ORA-20003: Fornecedor não possui conta contabil !3373'
    assert rota.mensagem_oracle(ValueError('x')) == 'ValueError'


# ------------------------------------------------------------- endpoint


def _usuario(perfil='admin'):
    return SimpleNamespace(id=4, nome='Amoras', perfil=perfil, telas_permitidas=[])


CONFIG = SimpleNamespace(MV_PROXY_USER='RC_PROXY', MV_PROXY_PASSWORD='x', CP_IMPORTACAO_MV_HABILITADA=True,
                         ORACLE_DATABASE_URL='oracle+oracledb://u:p@db:1521/?service_name=PRD')


class FakeResult:
    def __init__(self, linhas):
        self.linhas = linhas

    def mappings(self):
        return self

    def first(self):
        return self.linhas[0] if self.linhas else None

    def all(self):
        return self.linhas

    def scalar_one(self):
        return self.linhas[0]


class FakeSession:
    def __init__(self, conflito=False):
        self.conflito = conflito
        self.executados = []
        self.commits = 0
        self.rollbacks = 0

    def execute(self, sql, params=None):
        texto = str(sql)
        self.executados.append((texto, params))
        if 'valor_iss_retido' in texto:
            return FakeResult([{'data_hora': datetime(2026, 11, 1, 15, 50, tzinfo=rota.FUSO_HOSPITAL),
                                'valor_servicos': '2708.73', 'valor_liquido_nfse': '2708.73'}])
        if 'FROM api_prontocardio.contas_pagar_boletos' in texto:
            return FakeResult([{**BOLETO, 'divergencias': '[]'}])
        if 'to_regclass' in texto:
            return FakeResult([True])
        if 'INSERT INTO api_prontocardio.contas_pagar_importacoes' in texto:
            if self.conflito:
                raise rota.IntegrityError('x', {}, Exception('duplicate'))
            return FakeResult([77])
        return FakeResult([])

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def sql(self, trecho):
        return [p for s, p in self.executados if trecho in s]


@pytest.fixture
def mv_fake(monkeypatch):
    eventos = []
    estado = {'cursor_kw': {}, 'respostas': {}}

    class Conexao:
        def cursor(self):
            return FakeCursor(_respostas(**estado['respostas']), **estado['cursor_kw'])

        def rollback(self):
            eventos.append('rollback')

        def commit(self):
            eventos.append('commit')

    @contextmanager
    def sessao(login, **kw):
        eventos.append(login)
        yield Conexao()

    monkeypatch.setattr(rota, '_gestor', lambda u: True)
    monkeypatch.setattr(rota, '_nota_ou_404', lambda *a, **k: dict(NOTA))
    monkeypatch.setattr(rota, 'status_da_nota', lambda *a: 'NORMAL')
    monkeypatch.setattr(rota, '_vinculo', lambda *a: {'pode_gravar_no_mv': True, 'cd_usuario_mv': 'RH000938'})
    monkeypatch.setattr(rota.mv, 'sessao_mv', sessao)
    return SimpleNamespace(eventos=eventos, estado=estado)


def _assinatura_atual():
    resultado, _ = _simular()
    return resultado['assinatura']


def test_endpoint_exige_financeiro(monkeypatch):
    monkeypatch.setattr(rota, '_gestor', lambda u: False)
    for chamada in (lambda: rota.simular_importacao_mv('63554067000198', '93125822', _usuario('usuario'), None, None, CONFIG),
                    lambda: rota.importar_mv('63554067000198', '93125822', rota.ImportacaoInput(assinatura='a' * 20),
                                             _usuario('usuario'), None, None, CONFIG)):
        with pytest.raises(HTTPException) as erro:
            chamada()
        assert erro.value.status_code == 403


def test_endpoint_exige_login_mv(monkeypatch):
    monkeypatch.setattr(rota, '_gestor', lambda u: True)
    monkeypatch.setattr(rota, '_nota_ou_404', lambda *a, **k: dict(NOTA))
    monkeypatch.setattr(rota, 'status_da_nota', lambda *a: 'NORMAL')
    monkeypatch.setattr(rota, '_vinculo', lambda *a: {'pode_gravar_no_mv': False})
    with pytest.raises(HTTPException) as erro:
        rota.simular_importacao_mv('63554067000198', '93125822', _usuario(), None, None, CONFIG)
    assert erro.value.status_code == 412


def test_simulacao_nunca_confirma_a_transacao(mv_fake):
    resposta = rota.simular_importacao_mv('63554067000198', '93125822', _usuario(), FakeSession(), None, CONFIG)

    assert resposta['simulacao'] is True
    assert resposta['apta'] is True
    assert resposta['assinatura'] == _assinatura_atual()
    assert resposta['gravacao_habilitada'] is True
    assert resposta['nota']['data_emissao'] == date(2026, 11, 1)
    assert mv_fake.eventos == ['RH000938', 'rollback']


def test_importa_grava_e_registra(mv_fake):
    session = FakeSession()

    resposta = rota.importar_mv('63554067000198', '93125822', rota.ImportacaoInput(assinatura=_assinatura_atual()),
                                _usuario(), session, None, CONFIG)

    assert resposta == {'importada': True, 'nota': resposta['nota'], 'cd_usuario_mv': 'RH000938',
                        'cd_ent_serv': 12500, 'cd_con_pag': 108700}
    assert mv_fake.eventos == ['RH000938', 'commit']
    inicio = session.sql('INSERT INTO api_prontocardio.contas_pagar_importacoes')[0]
    assert inicio['login'] == 'RH000938' and inicio['usuario_id'] == 4
    fim = session.sql('UPDATE api_prontocardio.contas_pagar_importacoes')[0]
    assert (fim['status'], fim['ent_serv'], fim['con_pag'], fim['erro']) == ('importada', 12500, 108700, None)
    assert '"apta": true' in fim['plano']


def test_assinatura_diferente_nao_grava_e_libera_nova_tentativa(mv_fake):
    session = FakeSession()
    with pytest.raises(HTTPException) as erro:
        rota.importar_mv('63554067000198', '93125822', rota.ImportacaoInput(assinatura='0' * 20),
                         _usuario(), session, None, CONFIG)
    assert erro.value.status_code == 409
    assert 'Simule de novo' in erro.value.detail
    assert mv_fake.eventos == ['RH000938', 'rollback']
    assert session.sql('DELETE FROM api_prontocardio.contas_pagar_importacoes') == [{'id': 77}]


def test_nota_que_deixou_de_estar_apta(mv_fake):
    mv_fake.estado['respostas'] = {'duplicidade': [{'tabela': 'CON_PAG', 'chave': '1', 'nr_serie': 'A'}]}
    with pytest.raises(HTTPException) as erro:
        rota.importar_mv('63554067000198', '93125822', rota.ImportacaoInput(assinatura=_assinatura_atual()),
                         _usuario(), FakeSession(), None, CONFIG)
    assert erro.value.status_code == 409
    assert 'deixou de estar apta' in erro.value.detail
    assert 'commit' not in mv_fake.eventos


def test_erro_do_mv_desfaz_e_registra(mv_fake):
    mv_fake.estado['cursor_kw'] = {'importa': Exception('ORA-20007: Servico 10106 sem conta\nSQL: begin ...')}
    session = FakeSession()
    with pytest.raises(HTTPException) as erro:
        rota.importar_mv('63554067000198', '93125822', rota.ImportacaoInput(assinatura=_assinatura_atual()),
                         _usuario(), session, None, CONFIG)
    assert erro.value.status_code == 422
    assert 'nada foi gravado' in erro.value.detail and 'ORA-20007' in erro.value.detail
    assert 'SQL' not in erro.value.detail
    assert mv_fake.eventos == ['RH000938', 'rollback']
    fim = session.sql('UPDATE api_prontocardio.contas_pagar_importacoes')[0]
    assert fim['status'] == 'erro' and fim['erro'].startswith('ORA-20007')


def test_importacao_repetida_e_recusada_antes_de_abrir_o_mv(mv_fake):
    with pytest.raises(HTTPException) as erro:
        rota.importar_mv('63554067000198', '93125822', rota.ImportacaoInput(assinatura=_assinatura_atual()),
                         _usuario(), FakeSession(conflito=True), None, CONFIG)
    assert erro.value.status_code == 409
    assert mv_fake.eventos == []


def test_chave_de_desligamento(mv_fake):
    with pytest.raises(HTTPException) as erro:
        rota.importar_mv('63554067000198', '93125822', rota.ImportacaoInput(assinatura='a' * 20), _usuario(),
                         FakeSession(), None, SimpleNamespace(**{**vars(CONFIG), 'CP_IMPORTACAO_MV_HABILITADA': False}))
    assert erro.value.status_code == 503
    assert mv_fake.eventos == []


# ------------------------------------------------------- prazo / justificativa


def test_boleto_fora_do_prazo_justificado_vai_para_a_observacao_do_mv():
    atrasado = {**BOLETO, 'fora_do_prazo': True, 'justificativa_atraso': 'Fornecedor enviou o boleto hoje.',
                'enviado_por_nome': 'Setor TI', 'enviado_em': datetime(2026, 11, 24, 9, 15)}
    resultado, _ = _simular(boletos=[atrasado])

    assert resultado['apta'] is True
    assert any('Fornecedor enviou o boleto hoje.' in a for a in resultado['avisos'])
    ent = resultado['gravacoes'][0]['campos']
    doc = resultado['gravacoes'][2]['campos']
    esperado = ('Venc. 25/11/2026 fora do prazo de 3 dias úteis (enviado por Setor TI em 24/11/2026 09:15). '
                'Justificativa: Fornecedor enviou o boleto hoje.')
    assert ent['ds_observacao']['valor'] == esperado
    assert doc['observacao']['valor'] == esperado


def test_observacao_cabe_nos_500_caracteres_da_funcao():
    atrasado = {**BOLETO, 'fora_do_prazo': True, 'justificativa_atraso': 'x' * 400}
    resultado, _ = _simular(boletos=[atrasado, {**atrasado, 'id': 5, 'valor': Decimal('0.00')}],
                            nota={**NOTA})
    texto = resultado['gravacoes'][0]['campos']['ds_observacao']['valor']
    assert len(texto) == 500
    assert texto.endswith('...')


def test_fora_do_prazo_sem_justificativa_bloqueia():
    resultado, _ = _simular(boletos=[{**BOLETO, 'fora_do_prazo': True, 'justificativa_atraso': None}])
    assert 'prazo' in _codigos(resultado)


def test_boleto_no_prazo_nao_mexe_na_observacao():
    resultado, _ = _simular()
    assert 'ds_observacao' not in resultado['gravacoes'][0]['campos']
    assert 'observacao' not in resultado['gravacoes'][2]['campos']


# ------------------------------------------------------- ver NFS-e


def test_ver_nfse_traz_campos_do_xml(monkeypatch):
    monkeypatch.setattr(rota, '_nota_ou_404', lambda *a, **k: dict(NOTA))
    monkeypatch.setattr(rota, 'status_da_nota', lambda *a: 'NORMAL')

    class Sessao:
        def execute(self, sql, params=None):
            return FakeResult([{'data_hora': datetime(2026, 11, 1, 15, 50, 46, tzinfo=rota.FUSO_HOSPITAL),
                                'numero_nfse': '93125822', 'valor_servicos': '2708.73',
                                'xml_campos': '{"nfse_infnfse_chaveacesso": "2304400126", "nfse_infnfse_identificacaorps_serie": "NF"}',
                                'xml_documento': '<Nfse/>'}])

    dados = rota.ver_nfse('63.554.067/0001-98', '93125822', _usuario(), Sessao(), None)
    assert dados['chave_acesso'] == '2304400126'
    assert dados['rps_serie'] == 'NF'
    assert dados['status_nfse'] == 'NORMAL'
    assert 'xml_documento' not in dados
    assert dados['data_emissao'] == datetime(2026, 11, 1, 15, 50, 46)

    xml = rota.baixar_xml_nfse('63554067000198', '93125822', _usuario(), Sessao(), None)
    assert xml.body == b'<Nfse/>'
    assert 'nfse-63554067000198-93125822.xml' in xml.headers['content-disposition']


# ------------------------------------------------------- retenções (casos reais da fila)

# Auriocardio 45: serviços 30.487,20; líquido 27.697,63; ISS 3%, IRRF 1,5% e CSRF 4,65% (no campo da CSLL).
AURIOCARDIO = {**NOTA, 'valor_servicos': '30487.2', 'valor_liquido_nfse': '27697.63', 'iss_retido': '1',
               'valor_iss_retido': '914.62', 'valor_ir': '457.3', 'valor_csll': '1417.65'}
BOLETO_LIQUIDO = {**BOLETO, 'valor': Decimal('27697.63')}


def _simular_retencao(nota=AURIOCARDIO, boletos=(BOLETO_LIQUIDO,), **trocas):
    trocas.setdefault('impostos_fornecedor', [{'cd_detalhamento': 2}, {'cd_detalhamento': 11}, {'cd_detalhamento': 14}])
    return _simular(nota=nota, boletos=boletos, **trocas)


def test_retencoes_viram_impostos_do_mv_e_duplicata_bruta():
    resultado, _ = _simular_retencao()

    assert resultado['apta'] is True, resultado['pendencias']
    impostos = [g['campos'] for g in resultado['gravacoes'] if g['tabela'] == 'ITENT_SERV_IMPOSTO']
    assert sorted((i['cd_detalhamento']['valor'], i['vl_total_imposto']['valor'], i['vl_percentual_aliquota']['valor']) for i in impostos) == [
        (2, Decimal('457.3'), Decimal('1.5')), (11, Decimal('1417.65'), Decimal('4.65')), (14, Decimal('914.62'), Decimal('3'))]
    assert all(i['vl_base_calculo']['valor'] == Decimal('30487.2') and i['sn_retem']['valor'] == 'S' for i in impostos)
    item = next(g['campos'] for g in resultado['gravacoes'] if g['tabela'] == 'ITENT_SERV')
    assert item['vl_imposto']['valor'] == Decimal('2789.57')
    dup = next(g['campos'] for g in resultado['gravacoes'] if g['tabela'] == 'DUPLICATA')
    assert dup['vl_parcela']['valor'] == Decimal('30487.20')  # bruto: a função desconta os impostos da parcela 1
    saldo = next(g['campos'] for g in resultado['gravacoes'] if g['operacao'] == 'UPDATE')
    assert saldo['vl_saldo_documento']['valor'] == Decimal('0')
    titulo = resultado['importacao']['titulo_previsto']
    assert titulo['parcelas'][0]['vl_duplicata'] == Decimal('27697.63')
    assert titulo['vl_liquido'] == Decimal('27697.63')
    assert [(i['cd_detalhamento'], i['nr_documento']) for i in titulo['impostos']] == [(2, 'D-93125822'), (11, 'D-93125822'), (14, 'D-93125822')]
    ordem = [g['tabela'] for g in resultado['gravacoes']]
    assert ordem[:5] == ['ENT_SERV', 'ITENT_SERV', 'ITENT_SERV_IMPOSTO', 'ITENT_SERV_IMPOSTO', 'ITENT_SERV_IMPOSTO']


def test_iss_5_por_cento_usa_o_codigo_do_historico():
    # Hiteck 2119: só ISS 5%.
    nota = {**NOTA, 'valor_servicos': '5325.71', 'valor_liquido_nfse': '5059.42', 'iss_retido': '1', 'valor_iss_retido': '266.29'}
    resultado, _ = _simular_retencao(nota=nota, boletos=[{**BOLETO, 'valor': Decimal('5059.42')}], impostos_fornecedor=[{'cd_detalhamento': 9}])
    assert resultado['apta'] is True
    assert [i['cd_detalhamento'] for i in resultado['escolhas']['impostos_retidos']] == [9]


def test_pis_e_cofins_so_informativos_ficam_de_fora():
    # G4 Flex 23275: PIS/COFINS no XML, mas o líquido só desconta CSRF (campo CSLL), IR e ISS.
    nota = {**NOTA, 'valor_servicos': '3065.97', 'valor_liquido_nfse': '2724.11', 'iss_retido': '1', 'valor_iss_retido': '153.3',
            'valor_pis': '19.93', 'valor_cofins': '91.98', 'valor_csll': '142.57', 'valor_ir': '45.99'}
    resultado, _ = _simular_retencao(nota=nota, boletos=[{**BOLETO, 'valor': Decimal('2724.11')}],
                                     impostos_fornecedor=[{'cd_detalhamento': 9}])
    assert resultado['apta'] is True, resultado['pendencias']
    assert sorted(i['cd_detalhamento'] for i in resultado['escolhas']['impostos_retidos']) == [2, 9, 11]


def test_iss_2_por_cento_sem_historico():
    # DB3 5744268: ISS 2%, PIS/COFINS informativos.
    nota = {**NOTA, 'valor_servicos': '560', 'valor_liquido_nfse': '548.8', 'iss_retido': '1', 'valor_iss_retido': '11.2',
            'valor_pis': '9.06', 'valor_cofins': '41.71'}
    resultado, _ = _simular_retencao(nota=nota, boletos=[{**BOLETO, 'valor': Decimal('548.80')}], impostos_fornecedor=[])
    assert resultado['apta'] is True
    assert [i['cd_detalhamento'] for i in resultado['escolhas']['impostos_retidos']] == [3]


def test_aliquota_sem_imposto_no_mv_bloqueia():
    # B B Felix 1019: "CSLL" de 3%, que não existe no MV.
    nota = {**NOTA, 'valor_servicos': '203940.36', 'valor_liquido_nfse': '191703.94', 'iss_retido': '1',
            'valor_iss_retido': '6118.21', 'valor_csll': '6118.21'}
    resultado, _ = _simular_retencao(nota=nota, boletos=[{**BOLETO, 'valor': Decimal('191703.94')}])
    assert resultado['apta'] is False
    assert any('CSLL de R$ 6118.21 (3.00%)' in p['mensagem'] for p in resultado['pendencias'])


def test_irrf_acumulado_de_notas_anteriores_usa_o_imposto_do_fornecedor():
    # Healthcare Alliance 1622 (Recife): IRRF 1,5% desta nota daria 6,76 e o da 1620, 6,65 (ambos < R$ 10);
    # o fornecedor reteve os dois juntos (13,40). CSRF 4,65% normal.
    nota = {**NOTA, 'valor_servicos': '450.41', 'valor_liquido_nfse': '416.07', 'valor_ir': '13.40', 'valor_csll': '20.94'}
    resultado, _ = _simular_retencao(nota=nota, boletos=[{**BOLETO, 'valor': Decimal('416.07')}])
    assert resultado['apta'] is True, resultado['pendencias']
    impostos = {i['cd_detalhamento']: i['valor'] for i in resultado['escolhas']['impostos_retidos']}
    assert impostos == {2: Decimal('13.40'), 11: Decimal('20.94')}
    assert any('acumulou retenções anteriores' in a and 'R$ 6.76' in a for a in resultado['avisos'])
    titulo = resultado['importacao']['titulo_previsto']
    assert titulo['vl_liquido'] == Decimal('416.07')


def test_irrf_acima_da_aliquota_sem_imposto_do_fornecedor_bloqueia():
    nota = {**NOTA, 'valor_servicos': '450.41', 'valor_liquido_nfse': '416.07', 'valor_ir': '13.40', 'valor_csll': '20.94'}
    resultado, _ = _simular_retencao(nota=nota, boletos=[{**BOLETO, 'valor': Decimal('416.07')}],
                                     impostos_fornecedor=[{'cd_detalhamento': 11}])
    assert resultado['apta'] is False
    assert any('IRRF de R$ 13.40 (2.98%)' in p['mensagem'] for p in resultado['pendencias'])


def test_irrf_abaixo_da_aliquota_continua_bloqueando():
    nota = {**NOTA, 'valor_servicos': '450.41', 'valor_liquido_nfse': '426.47', 'valor_ir': '3.00', 'valor_csll': '20.94'}
    resultado, _ = _simular_retencao(nota=nota, boletos=[{**BOLETO, 'valor': Decimal('426.47')}])
    assert resultado['apta'] is False
    assert any('IRRF de R$ 3.00' in p['mensagem'] for p in resultado['pendencias'])


def test_retencao_que_nao_fecha_com_o_xml_bloqueia():
    nota = {**AURIOCARDIO, 'valor_liquido_nfse': '27000.00'}
    resultado, _ = _simular_retencao(nota=nota, boletos=[{**BOLETO, 'valor': Decimal('27000.00')}])
    assert 'retencao' in _codigos(resultado)
    assert 'não fecha' in resultado['pendencias'][0]['mensagem']


def test_outras_retencoes_bloqueiam():
    nota = {**NOTA, 'valor_liquido_nfse': '2608.73', 'outras_retencoes': '100'}
    resultado, _ = _simular_retencao(nota=nota, boletos=[{**BOLETO, 'valor': Decimal('2608.73')}])
    assert any('outras retenções' in p['mensagem'] for p in resultado['pendencias'])


def test_imposto_com_outra_regra_de_vencimento_bloqueia():
    tipos = [{**t, 'tp_data': 'V'} if t['cd_detalhamento'] == 14 else t for t in TIPOS_IMPOSTO]
    resultado, _ = _simular_retencao(tipos_imposto=tipos)
    assert any('outra regra de vencimento' in p['mensagem'] for p in resultado['pendencias'])


def test_duas_parcelas_com_retencao_descontam_so_da_primeira():
    boletos = [{**BOLETO, 'id': 8, 'valor': Decimal('17697.63'), 'vencimento': date(2026, 11, 25)},
               {**BOLETO, 'id': 9, 'valor': Decimal('10000.00'), 'vencimento': date(2026, 12, 25)}]
    resultado, _ = _simular_retencao(boletos=boletos)
    dups = [g['campos']['vl_parcela']['valor'] for g in resultado['gravacoes'] if g['tabela'] == 'DUPLICATA']
    assert dups == [Decimal('20487.20'), Decimal('10000.00')]


def test_sem_retencao_mas_fornecedor_com_imposto_no_mv_bloqueia():
    resultado, _ = _simular(detalhamentos=[{'origem': 'fornecedor', 'cd_detalhamento': 11, 'ds_detalhamento': 'CSRF', 'tp_detalhamento': 'S'}])
    assert 'impostos' in _codigos(resultado)


def test_sem_retencao_com_imposto_so_no_servico_e_aviso():
    resultado, _ = _simular(detalhamentos=[{'origem': 'servico', 'cd_detalhamento': 14, 'ds_detalhamento': 'ISS 3%', 'tp_detalhamento': 'I'}])
    assert resultado['apta'] is True
    assert any('nada será retido' in a for a in resultado['avisos'])


def test_executa_impostos_com_sequence_propria_e_confere_o_titulo():
    resultado, _ = _simular_retencao()
    cursor = FakeCursor(_respostas(parcelas_titulo=[{'qt_parcelas': 1, 'vl_parcelas': Decimal('27697.63')}],
                                   titulos_imposto=[{'cd_detalhamento': 2, 'cd_con_pag_filho': 1},
                                                    {'cd_detalhamento': 11, 'cd_con_pag_filho': 2},
                                                    {'cd_detalhamento': 14, 'cd_con_pag_filho': 3}]))
    cursor.sequences['seq_itent_serv_imposto'] = 1200
    ids = executar_importacao(cursor, resultado)
    assert ids['cd_con_pag'] == 108700
    impostos = [b for sql, b in cursor.executados if sql.startswith('insert into dbamv.itent_serv_imposto ')]
    assert len(impostos) == 3
    assert all(b['cd_itent_serv'] == 12600 and b['cd_itent_serv_imposto'] == 1200 for b in impostos)


@pytest.mark.parametrize(('trocas', 'mensagem'), [
    ({'parcelas_titulo': [{'qt_parcelas': 1, 'vl_parcelas': Decimal('30487.20')}], 'titulos_imposto': []}, 'parcelas do título somam'),
    ({'parcelas_titulo': [{'qt_parcelas': 1, 'vl_parcelas': Decimal('27697.63')}], 'titulos_imposto': [{'cd_detalhamento': 2, 'cd_con_pag_filho': 1}]}, 'Títulos de imposto'),
])
def test_titulo_que_nao_fecha_desfaz_a_importacao(trocas, mensagem):
    resultado, _ = _simular_retencao()
    cursor = FakeCursor(_respostas(**trocas))
    cursor.sequences['seq_itent_serv_imposto'] = 1200
    with pytest.raises(ImportacaoFalhou, match=mensagem):
        executar_importacao(cursor, resultado)
