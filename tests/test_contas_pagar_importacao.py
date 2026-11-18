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


def test_nota_com_retencao_fica_bloqueada():
    resultado, _ = _simular(nota={**NOTA, 'valor_liquido_nfse': '2600.00'},
                            boletos=[{**BOLETO, 'valor': Decimal('2600.00')}])
    assert 'retencao' in _codigos(resultado)
    assert resultado['apta'] is False


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
    monkeypatch.setattr(rota, '_nota_ou_404', lambda *a: dict(NOTA))
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
    monkeypatch.setattr(rota, '_nota_ou_404', lambda *a: dict(NOTA))
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
    monkeypatch.setattr(rota, '_nota_ou_404', lambda *a: dict(NOTA))
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
