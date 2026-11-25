# ruff: noqa: PLR2004, E501, PT018
import re
from contextlib import contextmanager
from datetime import date, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app_prontocardio.routers import contas_pagar_financeiro as rota
from app_prontocardio.services.contas_pagar_desfazer import (
    executar_desfazer,
    planejar_desfazer,
)
from app_prontocardio.services.contas_pagar_importacao import ImportacaoFalhou

REGISTRO = {'id': 1, 'status': 'importada', 'cd_multi_empresa': 2, 'cd_con_pag': 108679, 'cd_ent_serv': 12463,
            'cd_usuario_mv': 'RH000938'}


def _respostas(**trocas):
    base = {
        'desfazer_titulo': [{'cd_con_pag': 108679, 'cd_fornecedor': 3373, 'nr_documento': '93125822', 'nr_serie': 'A',
                             'cd_multi_empresa': 2, 'tp_con_pag': 'ES', 'vl_bruto_conta': Decimal('2708.73'),
                             'cd_con_pag_previsao': None, 'cd_usuario_ins': 'RH000938', 'dt_lancamento': datetime(2026, 11, 18)}],
        'desfazer_entrada': [{'cd_ent_serv': 12463, 'cd_fornecedor': 3373, 'nr_documento': '93125822', 'nr_serie': 'A',
                              'cd_con_pag': 108679, 'sn_importada_fnfi': 'S', 'cd_oficina': 12, 'vl_total': Decimal('2708.73')}],
        'desfazer_filhos': [],
        'desfazer_parcelas': [{'cd_con_pag': 108679, 'cd_itcon_pag': 226477, 'nr_parcela': 1, 'dt_vencimento': date(2026, 11, 25),
                               'vl_duplicata': Decimal('2708.73'), 'tp_quitacao': 'C'}],
        'desfazer_bloqueios': [],
        'desfazer_itens': [{'cd_itent_serv': 12561, 'cd_servico': 10106, 'vl_total': Decimal('2708.73'), 'qt_impostos': 0}],
        'desfazer_duplicatas': [{'qt': 1}],
        'desfazer_alteracoes': [],
    }
    base.update(trocas)
    return base


class FakeCursor:
    """Responde SELECTs pela etiqueta; para DML devolve rowcount pela tabela (padrão: o esperado)."""

    def __init__(self, respostas, linhas_afetadas=None):
        self.respostas = respostas
        self.linhas_afetadas = linhas_afetadas or {}
        self.executados = []
        self.description, self._linhas, self.rowcount = [], [], 0

    def execute(self, sql, binds=None):
        self.executados.append((sql, binds))
        etiqueta = re.search(r'/\* rc:(\w+) \*/', sql)
        if etiqueta:
            linhas = self.respostas.get(etiqueta.group(1), [])
            colunas = list(linhas[0]) if linhas else ['x']
            self.description = [(c.upper(),) for c in colunas]
            self._linhas = [tuple(linha[c] for c in colunas) for linha in linhas]
            return
        alvo = re.match(r'(?:update|delete from) dbamv\.(\w+)', sql)
        tabela = alvo.group(1) if alvo else ''
        self.rowcount = self.linhas_afetadas.get(tabela, {'documento_entrada': 1, 'ent_serv': 1, 'con_pag': 1, 'itcon_pag': 1,
                                                           'ent_serv_conclusao': 1, 'duplicata': 1, 'itent_serv': 1}.get(tabela, 0))

    def fetchall(self):
        return self._linhas


def _plano(**trocas):
    cursor = FakeCursor(_respostas(**trocas))
    return planejar_desfazer(cursor, registro=REGISTRO), cursor


def test_plano_sem_bloqueios_lista_o_que_sera_removido():
    plano, cursor = _plano()
    assert plano['pode_desfazer'] is True
    assert plano['titulo']['cd_con_pag'] == 108679
    assert plano['entrada']['cd_ent_serv'] == 12463
    assert plano['qt_duplicatas'] == 1
    assert cursor.executados[0][1] == {'empresa': 2}  # ATRIBUI_EMPRESA na sessão do operador
    assert not any(sql.startswith(('update', 'delete', 'insert')) for sql, _ in cursor.executados)


@pytest.mark.parametrize(('trocas', 'trecho'), [
    ({'desfazer_parcelas': [{'cd_con_pag': 108679, 'cd_itcon_pag': 1, 'nr_parcela': 1, 'dt_vencimento': date(2026, 11, 25),
                             'vl_duplicata': Decimal('1'), 'tp_quitacao': 'Q'}]}, 'não está em aberto'),
    ({'desfazer_bloqueios': [{'motivo': 'pagamento', 'cd_con_pag': 108679, 'nr_parcela': 1}]}, 'Existe pagamento para o título, parcela 1'),
    ({'desfazer_bloqueios': [{'motivo': 'lote de pagamento', 'cd_con_pag': 900, 'nr_parcela': 1}]}, 'o imposto (título 900)'),
    ({'desfazer_titulo': []}, 'não existe mais no MV'),
    ({'desfazer_entrada': [{**_respostas()['desfazer_entrada'][0], 'cd_con_pag': 5}]}, 'ligada a outro título'),
    ({'desfazer_titulo': [{**_respostas()['desfazer_titulo'][0], 'cd_con_pag_previsao': 77}]}, 'previsão'),
])
def test_bloqueios_do_mv(trocas, trecho):
    plano, _ = _plano(**trocas)
    assert plano['pode_desfazer'] is False
    assert any(trecho in p for p in plano['pendencias']), plano['pendencias']


def test_executa_na_ordem_do_mv_e_remove_a_entrada():
    plano, _ = _plano()
    cursor = FakeCursor(_respostas())
    removido = executar_desfazer(cursor, plano)
    assert removido['cd_con_pag'] == 108679 and removido['cd_ent_serv'] == 12463
    ordem = [re.match(r'(update|delete from) dbamv\.(\w+)', sql).group(0) for sql, _ in cursor.executados]
    assert ordem == [
        'update dbamv.ent_serv', 'update dbamv.documento_entrada', 'update dbamv.ent_serv',
        'delete from dbamv.fnfi_log', 'delete from dbamv.itmvto_cotas', 'delete from dbamv.ratcon_pag',
        'delete from dbamv.tip_detcon_pag', 'delete from dbamv.itretencao_tributo', 'delete from dbamv.rep_desc',
        'update dbamv.repasse_prestador', 'delete from dbamv.historico_prorrogacao', 'delete from dbamv.itcon_pag',
        'delete from dbamv.con_pag',
        'delete from dbamv.ent_serv_conclusao', 'delete from dbamv.duplicata', 'delete from dbamv.documento_entrada',
        'delete from dbamv.itent_serv_imposto', 'delete from dbamv.itent_serv', 'delete from dbamv.ent_serv',
    ]
    assert not any('commit' in sql or 'rollback' in sql for sql, _ in cursor.executados)


def test_titulos_de_imposto_filhos_sao_removidos_antes_do_titulo():
    filhos = [{'cd_con_pag_filho': 900, 'cd_detalhamento': 2, 'vl_bruto_conta': Decimal('457.3'), 'nr_documento': 'D-45'},
              {'cd_con_pag_filho': 901, 'cd_detalhamento': 14, 'vl_bruto_conta': Decimal('914.62'), 'nr_documento': 'D-45'}]
    plano, _ = _plano(desfazer_filhos=filhos)
    cursor = FakeCursor(_respostas(), linhas_afetadas={'tip_detcon_pag': 2})
    removido = executar_desfazer(cursor, plano)
    assert removido['impostos'] == [900, 901]
    filhos_apagados = [b['cp'] for sql, b in cursor.executados if sql == 'delete from dbamv.con_pag where cd_con_pag = :cp']
    assert filhos_apagados == [900, 901, 108679]


@pytest.mark.parametrize(('tabela', 'afetadas', 'trecho'), [
    ('con_pag', 0, 'Título'), ('ent_serv', 2, 'Entrada'), ('tip_detcon_pag', 1, 'títulos de imposto'), ('duplicata', 0, 'Duplicatas'),
])
def test_contagem_diferente_aborta(tabela, afetadas, trecho):
    plano, _ = _plano()
    with pytest.raises(ImportacaoFalhou, match=trecho):
        executar_desfazer(FakeCursor(_respostas(), linhas_afetadas={tabela: afetadas}), plano)


def test_plano_bloqueado_nao_executa():
    plano, _ = _plano(desfazer_bloqueios=[{'motivo': 'pagamento', 'cd_con_pag': 108679, 'nr_parcela': 1}])
    cursor = FakeCursor(_respostas())
    with pytest.raises(ImportacaoFalhou):
        executar_desfazer(cursor, plano)
    assert cursor.executados == []


# ------------------------------------------------------------- endpoints

CONFIG = SimpleNamespace(MV_PROXY_USER='RC_PROXY', MV_PROXY_PASSWORD='x', CP_IMPORTACAO_MV_HABILITADA=True,
                         ORACLE_DATABASE_URL='oracle+oracledb://u:p@db:1521/?service_name=PRD')


def _usuario():
    return SimpleNamespace(id=4, nome='Amoras', perfil='admin', telas_permitidas=[])


class Sessao:
    def __init__(self):
        self.executados, self.commits = [], 0

    def execute(self, sql, params=None):
        self.executados.append((str(sql), params))
        return SimpleNamespace(rowcount=1)

    def commit(self):
        self.commits += 1


@pytest.fixture
def mv(monkeypatch):
    eventos = []
    estado = {'respostas': {}, 'afetadas': {}}

    class Conexao:
        def cursor(self):
            return FakeCursor(_respostas(**estado['respostas']), estado['afetadas'])

        def commit(self):
            eventos.append('commit')

        def rollback(self):
            eventos.append('rollback')

    @contextmanager
    def sessao(vinculo, usuario, config):
        eventos.append(vinculo['cd_usuario_mv'])
        yield Conexao()

    monkeypatch.setattr(rota, '_gestor', lambda u: True)
    monkeypatch.setattr(rota.boletos_rota, '_nota_ou_404', lambda *a, **k: {'cd_multi_empresa': 2, 'numero_nfse': '93125822'})
    monkeypatch.setattr(rota.boletos_rota, '_importacao_registrada', lambda *a: dict(REGISTRO))
    monkeypatch.setattr(rota.boletos_rota, '_sessao', sessao)
    monkeypatch.setattr(rota, '_vinculo', lambda *a: {'pode_gravar_no_mv': True, 'cd_usuario_mv': 'RH000938'})
    return SimpleNamespace(eventos=eventos, estado=estado)


def test_previa_nunca_grava(mv):
    resposta = rota.previa_desfazer('63554067000198', '93125822', _usuario(), Sessao(), None, CONFIG)
    assert resposta['pode_desfazer'] is True
    assert mv.eventos == ['RH000938', 'rollback']


def test_desfaz_confirma_e_registra(mv):
    sessao = Sessao()
    resposta = rota.desfazer_importacao('63554067000198', '93125822',
                                        rota.DesfazerInput(motivo='Boleto anexado errado pelo setor', cd_con_pag=108679),
                                        _usuario(), sessao, None, CONFIG)
    assert resposta['desfeita'] is True and resposta['cd_con_pag'] == 108679
    assert mv.eventos == ['RH000938', 'commit']
    registro = sessao.executados[-1][1]
    assert registro['motivo'] == 'Boleto anexado errado pelo setor'
    assert registro['login'] == 'RH000938'
    assert sessao.commits == 1


def test_titulo_diferente_do_registrado(mv):
    with pytest.raises(HTTPException) as erro:
        rota.desfazer_importacao('63554067000198', '93125822', rota.DesfazerInput(motivo='x' * 12, cd_con_pag=1),
                                 _usuario(), Sessao(), None, CONFIG)
    assert erro.value.status_code == 409
    assert mv.eventos == []


def test_bloqueio_no_mv_volta_409_sem_gravar(mv):
    mv.estado['respostas'] = {'desfazer_bloqueios': [{'motivo': 'pagamento', 'cd_con_pag': 108679, 'nr_parcela': 1}]}
    with pytest.raises(HTTPException) as erro:
        rota.desfazer_importacao('63554067000198', '93125822', rota.DesfazerInput(motivo='x' * 12, cd_con_pag=108679),
                                 _usuario(), Sessao(), None, CONFIG)
    assert erro.value.status_code == 409 and 'pagamento' in erro.value.detail
    assert mv.eventos == ['RH000938', 'rollback']


def test_recusa_do_oracle_desfaz_tudo(mv):
    mv.estado['afetadas'] = {'con_pag': 0}
    sessao = Sessao()
    with pytest.raises(HTTPException) as erro:
        rota.desfazer_importacao('63554067000198', '93125822', rota.DesfazerInput(motivo='x' * 12, cd_con_pag=108679),
                                 _usuario(), sessao, None, CONFIG)
    assert erro.value.status_code == 422 and 'nada foi alterado' in erro.value.detail
    assert mv.eventos == ['RH000938', 'rollback']
    assert sessao.commits == 0


def test_so_importacoes_do_receita_certa(mv, monkeypatch):
    monkeypatch.setattr(rota.boletos_rota, '_importacao_registrada', lambda *a: None)
    with pytest.raises(HTTPException) as erro:
        rota.previa_desfazer('63554067000198', '93125822', _usuario(), Sessao(), None, CONFIG)
    assert erro.value.status_code == 409


def test_setor_nao_acessa(monkeypatch):
    monkeypatch.setattr(rota, '_gestor', lambda u: False)
    with pytest.raises(HTTPException) as erro:
        rota.conferencia_financeiro(_usuario(), None, None)
    assert erro.value.status_code == 403


# ------------------------------------------------------------- conferência


def test_conferencia_junta_boletos_prazo_e_importacao(monkeypatch):
    itens = [
        {'chave': 'A-1', 'prestador_cnpj': 'A', 'prestador': 'Hapvida', 'numero_nfse': '1', 'situacao': 'lancada', 'pronta': False,
         'valor_servicos': Decimal('2708.73'), 'valor_liquido': Decimal('2708.73'), 'status_nfse': 'NORMAL', 'data_emissao': date(2026, 11, 1), 'mv': {}},
        {'chave': 'B-2', 'prestador_cnpj': 'B', 'prestador': 'DB3', 'numero_nfse': '2', 'situacao': 'pendente', 'pronta': True,
         'valor_servicos': Decimal('560'), 'valor_liquido': Decimal('548.80'), 'status_nfse': 'NORMAL', 'data_emissao': date(2026, 11, 13), 'mv': None},
        {'chave': 'C-3', 'prestador_cnpj': 'C', 'prestador': 'Sem boleto', 'numero_nfse': '3', 'situacao': 'pendente', 'pronta': False,
         'valor_servicos': Decimal('10'), 'valor_liquido': Decimal('10'), 'status_nfse': 'NORMAL', 'data_emissao': date(2026, 11, 13), 'mv': None},
        {'chave': 'D-4', 'prestador_cnpj': 'D', 'prestador': 'Divergente', 'numero_nfse': '4', 'situacao': 'pendente', 'pronta': False,
         'valor_servicos': Decimal('100'), 'valor_liquido': Decimal('100'), 'status_nfse': 'NORMAL', 'data_emissao': date(2026, 11, 10), 'mv': None},
    ]
    monkeypatch.setattr(rota, '_gestor', lambda u: True)
    monkeypatch.setattr(rota, 'listar_recebidas', lambda *a, **k: {'desde': date(2026, 10, 1), 'itens': itens})
    monkeypatch.setattr(rota.boletos_rota, '_agora_local', lambda: datetime(2026, 11, 18, 10, 0))
    boleto = {'banco': '033', 'linha_digitavel': '0339', 'origem_leitura': 'pdf', 'enviado_em': datetime(2026, 11, 18, 9),
              'prazo_minimo': date(2026, 11, 24), 'justificativa_atraso': None, 'enviado_por_nome': 'Setor'}

    class S:
        def execute(self, sql, params=None):
            texto = str(sql)
            if 'contas_pagar_boletos' in texto:
                linhas = [{**boleto, 'id': 1, 'prestador_cnpj': 'A', 'numero_nfse': '1', 'valor': Decimal('2708.73'), 'vencimento': date(2026, 11, 25), 'divergencias': '[]', 'fora_do_prazo': False},
                          {**boleto, 'id': 2, 'prestador_cnpj': 'B', 'numero_nfse': '2', 'valor': Decimal('548.80'), 'vencimento': date(2026, 11, 20), 'divergencias': '[]', 'fora_do_prazo': True, 'justificativa_atraso': 'Fornecedor atrasou'},
                          {**boleto, 'id': 3, 'prestador_cnpj': 'D', 'numero_nfse': '4', 'valor': Decimal('90'), 'vencimento': date(2026, 12, 1), 'divergencias': '["Valor difere"]', 'fora_do_prazo': False}]
                return SimpleNamespace(mappings=lambda: iter(linhas))
            if 'to_regclass' in texto:
                return SimpleNamespace(scalar_one=lambda: True)
            return SimpleNamespace(mappings=lambda: iter([{'prestador_cnpj': 'A', 'numero_nfse': '1', 'status': 'importada', 'cd_con_pag': 108679, 'cd_ent_serv': 12463}]))

    resposta = rota.conferencia_financeiro(_usuario(), S(), None)
    etapas = {i['numero_nfse']: i['etapa'] for i in resposta['itens']}
    assert etapas == {'2': 'a_importar', '1': 'importada', '4': 'pendencia'}  # a nota sem boleto não aparece
    assert [i['numero_nfse'] for i in resposta['itens']] == ['2', '1', '4']  # por vencimento
    db3 = next(i for i in resposta['itens'] if i['numero_nfse'] == '2')
    assert db3['fora_do_prazo'] is True and db3['valor_retido'] == Decimal('11.20')
    assert next(i for i in resposta['itens'] if i['numero_nfse'] == '1')['pode_desfazer'] is True
    pend = next(i for i in resposta['itens'] if i['numero_nfse'] == '4')['pendencias']
    assert 'Valor difere' in pend and any('Boletos somam R$ 90.00' in p for p in pend)
    assert resposta['resumo']['a_importar'] == {'quantidade': 1, 'valor_liquido': Decimal('548.80')}
    assert resposta['fora_do_prazo'] == 1
    assert resposta['prazo_minimo_hoje'] == datetime(2026, 11, 24, 10, 0)

    so_pendencia = rota.conferencia_financeiro(_usuario(), S(), None, etapa='pendencia')
    assert [i['numero_nfse'] for i in so_pendencia['itens']] == ['4']
    assert so_pendencia['resumo']['importada']['quantidade'] == 1  # o resumo é sempre do conjunto todo


def test_alteracoes_feitas_no_mv_aparecem_no_plano():
    alteracao = {'dt_modificacao': datetime(2026, 11, 18, 12, 2, 31), 'cd_usuario': 'RH000850',
                 'campos': 'Conta alterada. Campos: Observação: 05 / DP / PLANO DE SAUDE NOV/26; '}
    plano, _ = _plano(desfazer_alteracoes=[alteracao])
    assert plano['pode_desfazer'] is True  # não bloqueia, mas a tela pede confirmação
    assert plano['alteracoes_no_mv'] == [alteracao]
