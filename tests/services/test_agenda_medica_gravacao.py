"""Gravação da grade aprovada no SoulMV, com Oracle falso (sem banco real)."""

from datetime import date

import pytest

from app_prontocardio.services.agenda_medica_soulmv import (
    TABELAS_FILHAS_ESCALA,
    AgendaMedicaSoulMV,
    GeracaoAgendaErro,
)

DATA = date(2026, 11, 3)  # terça-feira: TP_DIA_SEMANA 3
NOVA_ESCALA = 9001
NOVA_AGENDA = 77001
CD_GERACAO = 501
CD_IT_GERACAO = 502
ESCALA = {
    'cd_escala_central': 757,
    'tp_dia_semana': 3,
    'cd_unidade_atendimento': 1,
    'cd_recurso_central': None,
    'cd_setor': 56,
    'cd_multi_empresa': 1,
}


class CursorFalso:
    def __init__(self, conexao):
        self.conexao = conexao
        self.rowcount = 0
        self._linhas = []

    def execute(self, sql, **binds):
        texto = ' '.join(sql.split())
        self.conexao.comandos.append((texto, binds))
        self.rowcount = 1
        if 'SEQ_ESCALA_CENTRAL' in texto:
            self._linhas = [(NOVA_ESCALA,)]
        elif 'SEQ_GERACAO_AGENDA' in texto:
            self._linhas = [(CD_GERACAO,)]
        elif 'SEQ_IT_GERACAO_AGENDA' in texto:
            self._linhas = [(CD_IT_GERACAO,)]
        elif texto.startswith('SELECT CD_AGENDA_CENTRAL'):
            self._linhas = [(a,) for a in self.conexao.agendas_criadas]
        elif texto.startswith('INSERT INTO DBAMV.ESCALA_CENTRAL ('):
            self.rowcount = self.conexao.escala_modelo_existe

    def fetchone(self):
        return self._linhas[0]

    def fetchall(self):
        return self._linhas

    def callproc(self, nome, parametros):
        self.conexao.comandos.append((nome, parametros))

    @staticmethod
    def var(tipo, tamanho=None):
        class Variavel:
            def getvalue(self):
                return 'mensagem do MV'

        return Variavel()

    def callfunc(self, nome, tipo, parametros):
        self.conexao.chamada_funcao = (nome, parametros)
        return self.conexao.retorno_funcao

    def close(self):
        pass


class ConexaoFalsa:
    def __init__(self):
        self.comandos = []
        self.chamada_funcao = None
        self.retorno_funcao = 1
        self.agendas_criadas = [NOVA_AGENDA]
        self.escala_modelo_existe = 1
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return CursorFalso(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


class SessaoFalsa:
    def __init__(self, conexao):
        self._conexao = conexao

    def connection(self):
        class Envoltorio:
            connection = self._conexao

        return Envoltorio()


@pytest.fixture
def conexao():
    return ConexaoFalsa()


def _gerar(conexao):
    return AgendaMedicaSoulMV(SessaoFalsa(conexao)).gerar_agenda_aprovada(
        ESCALA, 358, DATA, 'DBAMV'
    )


def test_grava_escala_do_medico_e_chama_a_funcao_oficial(conexao):
    resultado = _gerar(conexao)

    assert resultado['gravado_no_soulmv'] is True
    assert resultado['cd_escala_central'] == NOVA_ESCALA
    assert resultado['cd_agenda_central'] == [NOVA_AGENDA]
    assert conexao.commits == 1
    assert conexao.rollbacks == 0

    textos = [comando for comando, _ in conexao.comandos]
    escala = next(
        t for t in textos if t.startswith('INSERT INTO DBAMV.ESCALA_CENTRAL (')
    )
    assert (
        'FROM DBAMV.ESCALA_CENTRAL WHERE CD_ESCALA_CENTRAL = :modelo' in escala
    )
    for tabela in TABELAS_FILHAS_ESCALA:
        assert any(
            t.startswith(f'INSERT INTO DBAMV.{tabela} ') for t in textos
        )
    assert not any('AGENDA_CENTRAL (' in t and 'INSERT' in t for t in textos)
    assert not any(t.startswith(('UPDATE', 'DELETE')) for t in textos)

    nome, parametros = conexao.chamada_funcao
    assert nome == 'DBAMV.PKG_SCMA_M_GERA_AGENDA.FNC_GERA_AGENDA'
    assert parametros[:3] == ['A', NOVA_ESCALA, 358]
    # Só a terça marcada: segunda..domingo = '0','3','0','0','0','0','0'.
    assert parametros[8:15] == ['0', '3', '0', '0', '0', '0', '0']
    assert parametros[16:18] == [DATA, DATA]
    assert parametros[-3:] == [CD_IT_GERACAO, CD_GERACAO, 'S']


def test_retorno_diferente_de_sucesso_desfaz_tudo(conexao):
    conexao.retorno_funcao = 3
    with pytest.raises(GeracaoAgendaErro, match='retorno 3'):
        _gerar(conexao)
    assert conexao.commits == 0
    assert conexao.rollbacks == 1


def test_sucesso_sem_agenda_criada_desfaz_tudo(conexao):
    conexao.agendas_criadas = []
    with pytest.raises(GeracaoAgendaErro, match='nenhuma agenda'):
        _gerar(conexao)
    assert conexao.commits == 0
    assert conexao.rollbacks == 1


def test_escala_modelo_inexistente_nao_grava(conexao):
    conexao.escala_modelo_existe = 0
    with pytest.raises(GeracaoAgendaErro, match='Escala-modelo'):
        _gerar(conexao)
    assert conexao.chamada_funcao is None
    assert conexao.rollbacks == 1
