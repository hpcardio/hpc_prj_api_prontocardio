from datetime import date
from decimal import Decimal
from http import HTTPStatus
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app_prontocardio.routers import financeiro
from app_prontocardio.services import associacao_maida as maida

ITEM = {
    'id_registro': 'demo-1',
    'lote': '33210',
    'guia': '613630',
    'codigo_servico': '20010176',
    'data_realizacao': date(2026, 9, 28),
    'referencia': date(2026, 10, 1),
    'registro_glosa_id': 37887,
    'registro_cd_atendimento': 0,
    'associacao_id': None,
}


def _lancamento(**extra):
    base = {
        'cd_remessa': 20452,
        'cd_reg': 26300,
        'cd_lancamento': 15,
        'cd_atendimento': 357834,
        'cd_paciente': 86173,
        'nm_paciente': 'PACIENTE TESTE',
        'cd_convenio': 46,
        'nm_convenio': 'ISSEC',
        'tp_atendimento': 'Internação',
        'nr_guia': '313630',
        'cd_pro_fat': '20010176',
        'cd_tuss': None,
        'descricao': 'ECODOPPLER VERTEBRAL',
        'dt_atendimento': date(2026, 9, 27),
        'dt_alta': None,
        'dt_lancamento': date(2026, 9, 28),
        'qt_lancamento': Decimal('1'),
        'vl_total_conta': Decimal('106.40'),
    }
    return {**base, **extra}


class Resultado:
    def __init__(self, linhas=(), primeiro=None, rowcount=0):
        self.linhas = list(linhas)
        self.primeiro = primeiro
        self.rowcount = rowcount

    def first(self):
        return self.primeiro

    def scalars(self):
        return iter(self.linhas)


class Sessao:
    def __init__(self, ocupado=None):
        self.ocupado = ocupado
        self.sql = []
        self.added = []
        self.commits = 0

    def execute(self, query, params=None):
        sql = str(query)
        self.sql.append((sql, params))
        if 'ipm_brasindice_oracle' in sql:
            return Resultado([])
        if 'UNION ALL' in sql:
            return Resultado(primeiro=self.ocupado)
        if sql.lstrip().startswith('UPDATE'):
            return Resultado(rowcount=2)
        raise AssertionError(sql)

    def add(self, obj):
        obj.id = 9
        self.added.append(obj)

    def flush(self):
        pass

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass


def test_candidato_exige_issec_remessa_e_mesmo_procedimento():
    candidatos = maida.marcar_candidatos(Sessao(), ITEM, [
        _lancamento(cd_lancamento=1, cd_pro_fat='10101012'),
        _lancamento(cd_lancamento=2, cd_convenio=10),
        _lancamento(cd_lancamento=3, cd_remessa=None),
        _lancamento(cd_lancamento=4, cd_pro_fat='020010176'),
    ])

    assert [c['cd_lancamento'] for c in candidatos if c['selecionavel']] == [4]
    assert candidatos[0]['cd_lancamento'] == 4
    porcodigo = {c['cd_lancamento']: c for c in candidatos}
    assert porcodigo[1]['mesmo_procedimento'] is False
    assert porcodigo[2]['issec'] is False
    assert porcodigo[3]['faturado'] is False


def _associar(monkeypatch, sessao, lancamentos, **escolha):
    monkeypatch.setattr(maida, 'buscar_item', lambda *_: dict(ITEM))
    monkeypatch.setattr(
        maida, 'lancamentos_atendimento', lambda *_: lancamentos
    )
    params = {
        'cd_atendimento': 357834,
        'cd_remessa': 20452,
        'conta': 26300,
        'cd_lancamento': 15,
        **escolha,
    }
    return maida.associar(
        sessao, object(), id_registro='demo-1', usuario_id=3, **params
    )


def test_associar_grava_vinculo_e_aplica_contexto_do_mv(monkeypatch):
    sessao = Sessao()

    resultado = _associar(monkeypatch, sessao, [_lancamento()])

    associacao = sessao.added[0]
    assert associacao.nr == 'MAIDA'
    assert associacao.numero_processo == '33210'
    assert associacao.competencia_producao == '09/2026'
    assert (associacao.cd_remessa, associacao.conta,
            associacao.cd_lancamento) == (20452, 26300, 15)
    assert associacao.criterio_correspondencia == maida.CRITERIO_MAIDA
    update_sql, update_params = sessao.sql[-1]
    assert 'UPDATE api_prontocardio.registros_glosa' in update_sql
    # Synthetic launch, guide, procedure and process stay untouched.
    for campo in ('cd_lancamento =', 'guia =', 'procedimento =',
                  'processo_controle_fatura_gab ='):
        assert f'SET {campo}' not in update_sql
        assert f'\n                   {campo}' not in update_sql
    assert update_params['cd_remessa'] == 20452
    assert update_params['cd_atendimento'] == 357834
    assert update_params['registro_glosa_id'] == 37887
    assert resultado['registros_atualizados'] == 2
    assert sessao.commits == 1


def test_associar_recusa_lancamento_de_outro_procedimento(monkeypatch):
    sessao = Sessao()

    with pytest.raises(HTTPException) as erro:
        _associar(
            monkeypatch, sessao, [_lancamento(cd_pro_fat='10101012')]
        )

    assert erro.value.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert not sessao.added
    assert sessao.commits == 0


def test_associar_recusa_lancamento_fora_do_atendimento(monkeypatch):
    with pytest.raises(HTTPException) as erro:
        _associar(monkeypatch, Sessao(), [_lancamento()], cd_lancamento=99)

    assert erro.value.status_code == HTTPStatus.UNPROCESSABLE_ENTITY


def test_associar_recusa_lancamento_ja_vinculado(monkeypatch):
    sessao = Sessao(ocupado=(1,))

    with pytest.raises(HTTPException) as erro:
        _associar(monkeypatch, sessao, [_lancamento()])

    assert erro.value.status_code == HTTPStatus.CONFLICT
    assert not sessao.added


def test_associar_recusa_item_ja_associado(monkeypatch):
    monkeypatch.setattr(
        maida, 'buscar_item', lambda *_: {**ITEM, 'associacao_id': 5}
    )

    with pytest.raises(HTTPException) as erro:
        maida.associar(
            Sessao(), object(), id_registro='demo-1', cd_atendimento=1,
            cd_remessa=1, conta=1, cd_lancamento=1, usuario_id=3,
        )

    assert erro.value.status_code == HTTPStatus.CONFLICT


def test_excluir_restaura_contexto_provisorio(monkeypatch):
    associacao = SimpleNamespace(nr='MAIDA', glosa_id_registro='demo-1')
    removidos = []

    class SessaoExcluir(Sessao):
        def get(self, _modelo, _id):
            return associacao

        def delete(self, obj):
            removidos.append(obj)

    sessao = SessaoExcluir()
    monkeypatch.setattr(maida, 'buscar_item', lambda *_: dict(ITEM))

    resultado = maida.excluir(sessao, 9)

    sql, params = sessao.sql[-1]
    assert 'cd_atendimento = 0' in sql
    assert 'cd_convenio = 10' in sql
    assert params == {'id_registro': 'demo-1', 'registro_glosa_id': 37887}
    assert removidos == [associacao]
    assert resultado == {'registros_restaurados': 2}


def test_excluir_nao_mexe_em_associacao_do_ipm_saude():
    class SessaoIpm(Sessao):
        def get(self, _modelo, _id):
            return SimpleNamespace(nr='4124085')

    with pytest.raises(HTTPException) as erro:
        maida.excluir(SessaoIpm(), 9)

    assert erro.value.status_code == HTTPStatus.NOT_FOUND


def test_rotas_do_ipm_saude_nao_alteram_associacao_da_maida():
    class SessaoRota:
        def get(self, _modelo, _id):
            return SimpleNamespace(nr='MAIDA')

        def delete(self, _obj):
            raise AssertionError('não deve excluir')

    with pytest.raises(HTTPException) as erro:
        financeiro.excluir_associacao_item_ipm(
            associacao_id=9, usuario_atual=object(), session=SessaoRota()
        )

    assert erro.value.status_code == HTTPStatus.NOT_FOUND


def test_listagem_filtra_lote_guia_e_pendentes():
    chamadas = []

    class SessaoLista:
        def execute(self, query, params=None):
            chamadas.append((str(query), params))
            if 'COUNT(*)' in str(query):
                return SimpleNamespace(scalar=lambda: 1)
            return SimpleNamespace(
                mappings=lambda: SimpleNamespace(all=lambda: [ITEM])
            )

    resultado = maida.listar_itens(
        SessaoLista(), lote=' 33210 ', guia='613630', limit=5, offset=0
    )

    sql, params = chamadas[-1]
    assert params['lote'] == '33210'
    assert params['guia'] == '613630'
    assert params['nr_maida'] == 'MAIDA'
    assert 'associacao.id IS NULL' in sql
    assert 'demo.origem_maida IS NOT NULL' in sql
    assert resultado['total'] == 1
    assert resultado['itens'] == [ITEM]
