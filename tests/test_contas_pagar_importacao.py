# ruff: noqa: PLR2004, E501
import re
from datetime import date, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app_prontocardio.routers import contas_pagar_boletos as rota
from app_prontocardio.services.contas_pagar_importacao import (
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
    }
    base.update(trocas)
    return base


class FakeCursor:
    def __init__(self, respostas):
        self.respostas = respostas
        self.executados = []
        self.description = []
        self._linhas = []

    def execute(self, sql, binds=None):
        self.executados.append((sql, binds))
        etiqueta = re.search(r'/\* rc:(\w+) \*/', sql)
        linhas = self.respostas[etiqueta.group(1)] if etiqueta else []
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


# ------------------------------------------------------------- endpoint


def _usuario(perfil='admin'):
    return SimpleNamespace(id=4, nome='Amoras', perfil=perfil, telas_permitidas=[])


def test_endpoint_exige_financeiro(monkeypatch):
    monkeypatch.setattr(rota, '_gestor', lambda u: False)
    with pytest.raises(HTTPException) as erro:
        rota.simular_importacao_mv('63554067000198', '93125822', _usuario('usuario'), None, None, None)
    assert erro.value.status_code == 403


def test_endpoint_exige_login_mv(monkeypatch):
    monkeypatch.setattr(rota, '_gestor', lambda u: True)
    monkeypatch.setattr(rota, '_nota_ou_404', lambda *a: dict(NOTA))
    monkeypatch.setattr(rota, 'status_da_nota', lambda *a: 'NORMAL')
    monkeypatch.setattr(rota, '_vinculo', lambda *a: {'pode_gravar_no_mv': False})
    with pytest.raises(HTTPException) as erro:
        rota.simular_importacao_mv('63554067000198', '93125822', _usuario(), None, None, None)
    assert erro.value.status_code == 412


def test_endpoint_nunca_confirma_a_transacao(monkeypatch):
    from contextlib import contextmanager  # noqa: PLC0415

    eventos = []

    class Conexao:
        def cursor(self):
            return FakeCursor(_respostas())

        def rollback(self):
            eventos.append('rollback')

        def commit(self):
            eventos.append('commit')

    @contextmanager
    def sessao(login, **kw):
        eventos.append(login)
        yield Conexao()

    class Session:
        def execute(self, sql, params=None):
            texto = str(sql)
            if 'valor_iss_retido' in texto:
                linhas = [{'data_hora': datetime(2026, 11, 1, 15, 50, tzinfo=rota.FUSO_HOSPITAL), 'valor_servicos': '2708.73',
                           'valor_liquido_nfse': '2708.73'}]
            else:
                linhas = [{**BOLETO, 'divergencias': '[]', 'prestador_cnpj': NOTA['prestador_cnpj']}]
            return SimpleNamespace(mappings=lambda: SimpleNamespace(first=lambda: linhas[0], all=lambda: linhas))

    monkeypatch.setattr(rota, '_gestor', lambda u: True)
    monkeypatch.setattr(rota, '_nota_ou_404', lambda *a: dict(NOTA))
    monkeypatch.setattr(rota, 'status_da_nota', lambda *a: 'NORMAL')
    monkeypatch.setattr(rota, '_vinculo', lambda *a: {'pode_gravar_no_mv': True, 'cd_usuario_mv': 'RH000938'})
    monkeypatch.setattr(rota.mv, 'sessao_mv', sessao)
    config = SimpleNamespace(MV_PROXY_USER='RC_PROXY', MV_PROXY_PASSWORD='x',
                             ORACLE_DATABASE_URL='oracle+oracledb://u:p@db:1521/?service_name=PRD')

    resposta = rota.simular_importacao_mv('63554067000198', '93125822', _usuario(), Session(), None, config)

    assert resposta['simulacao'] is True
    assert resposta['apta'] is True
    assert resposta['nota']['data_emissao'] == date(2026, 11, 1)
    assert eventos == ['RH000938', 'rollback']
