# ruff: noqa: PLR2004

from copy import deepcopy
from datetime import date, datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock

from app_prontocardio.routers import contas_pagar


class FakeMappings:
    def __init__(self, rows):
        self.rows = rows

    def all(self):
        return self.rows

    def __iter__(self):
        return iter(self.rows)


class FakeResult:
    def __init__(self, rows):
        self.rows = rows

    def mappings(self):
        return FakeMappings(self.rows)

    def scalar_one(self):
        return self.rows[0]

    def scalar_one_or_none(self):
        return self.rows[0] if self.rows else None


class FakeSession:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.executions = []
        self.commits = 0

    def execute(self, statement, params=None):
        self.executions.append((str(statement), params))
        return FakeResult(next(self.responses, []))

    def commit(self):
        self.commits += 1


def test_consulta_oracle_converte_saldos_e_atraso(monkeypatch):
    monkeypatch.setattr(
        contas_pagar, '_hoje_hospital', lambda: date(2026, 9, 17)
    )
    oracle = FakeSession([
        [
            {
                'codigo_fornecedor': 10,
                'nome_fornecedor': 'Fornecedor crítico',
                'codigo_parcela_pk': 99,
                'codigo_contas_pagar': 88,
                'numero_documento': 'NF-10',
                'descricao_conta': 'Medicamentos',
                'numero_parcela': 1,
                'data_lancamento': datetime(2026, 5, 20, 0, 0),
                'data_emissao': datetime(2026, 5, 18, 0, 0),
                'valor_duplicata': Decimal('500000'),
                'valor_nota_fiscal': Decimal('1000000'),
                'data_vencimento': datetime(2026, 6, 9, 0, 0),
                'tipo_quitacao': 'parcialmente pago',
                'valor_honrado_oracle': Decimal('100000'),
            }
        ]
    ])

    titulos = contas_pagar._consultar_titulos_oracle(oracle, [10])
    resultado = contas_pagar._agrupar_fornecedores(
        titulos,
        {
            99: [
                {
                    'valor_pago': Decimal('50000'),
                }
            ]
        },
    )

    assert resultado[0]['dias_atraso'] == 100
    assert resultado[0]['vencimento_mais_antigo'] == date(2026, 6, 9)
    assert resultado[0]['titulos'][0]['data_lancamento'] == date(2026, 5, 20)
    assert resultado[0]['titulos'][0]['data_emissao'] == date(2026, 5, 18)
    assert resultado[0]['titulos'][0]['valor_nota_fiscal'] == Decimal(
        '1000000.00'
    )
    assert resultado[0]['valor_total'] == Decimal('500000.00')
    assert resultado[0]['valor_total_honrado'] == Decimal('150000.00')
    assert resultado[0]['valor_total_vencido'] == Decimal('350000.00')
    assert resultado[0]['saldo_a_pagar'] == Decimal('350000.00')
    assert resultado[0]['total_dias_vencidos'] == 100


def test_titulos_do_fornecedor_sao_ordenados_por_maior_atraso(monkeypatch):
    monkeypatch.setattr(
        contas_pagar, '_hoje_hospital', lambda: date(2026, 9, 17)
    )
    base = {
        'codigo_fornecedor': 10,
        'nome_fornecedor': 'Fornecedor crítico',
        'codigo_contas_pagar': 88,
        'numero_documento': 'NF-10',
        'descricao_conta': 'Medicamentos',
        'numero_parcela': 1,
        'valor_total': Decimal('100'),
        'tipo_quitacao': 'previsto',
        'valor_honrado_oracle': Decimal('0'),
    }
    titulos = [
        {**base, 'codigo_parcela': 1, 'data_vencimento': date(2026, 8, 1)},
        {**base, 'codigo_parcela': 2, 'data_vencimento': date(2026, 5, 1)},
        {**base, 'codigo_parcela': 3, 'data_vencimento': date(2026, 7, 1)},
    ]

    resultado = contas_pagar._agrupar_fornecedores(titulos, {})

    assert [
        titulo['codigo_parcela'] for titulo in resultado[0]['titulos']
    ] == [2, 3, 1]
    assert [
        titulo['dias_vencidos'] for titulo in resultado[0]['titulos']
    ] == sorted(
        [titulo['dias_vencidos'] for titulo in resultado[0]['titulos']],
        reverse=True,
    )


def test_titulo_sem_saldo_mostra_dias_sem_alterar_prioridade(monkeypatch):
    monkeypatch.setattr(
        contas_pagar, '_hoje_hospital', lambda: date(2026, 9, 20)
    )
    base = {
        'codigo_fornecedor': 10,
        'nome_fornecedor': 'Fornecedor',
        'tipo_quitacao': 'previsto',
        'valor_honrado_oracle': Decimal('0'),
    }
    titulos = [
        {
            **base,
            'codigo_parcela': 1,
            'data_vencimento': date(2022, 9, 10),
            'valor_total': Decimal('0'),
        },
        {
            **base,
            'codigo_parcela': 2,
            'data_vencimento': date(2026, 9, 3),
            'valor_total': Decimal('100'),
        },
    ]

    fornecedor = contas_pagar._agrupar_fornecedores(titulos, {})[0]

    assert fornecedor['titulos'][0]['codigo_parcela'] == 1
    assert (
        fornecedor['titulos'][0]['dias_vencidos']
        == (date(2026, 9, 20) - date(2022, 9, 10)).days
    )
    assert fornecedor['titulos'][1]['dias_vencidos'] == 17
    assert fornecedor['dias_atraso'] == 17
    assert fornecedor['total_dias_vencidos'] == 17
    assert fornecedor['titulos_vencidos'] == 1
    assert fornecedor['valor_total_vencido'] == Decimal('100')


def test_hoje_hospital_nao_adianta_data_quando_utc_virou_o_dia(monkeypatch):
    class DataHoraFixa:
        @staticmethod
        def now(fuso):
            horario_utc = datetime(2026, 9, 21, 2, 30, tzinfo=timezone.utc)
            return horario_utc.astimezone(fuso)

    monkeypatch.setattr(contas_pagar, 'datetime', DataHoraFixa)

    assert contas_pagar._hoje_hospital() == date(2026, 9, 20)


def test_divida_inicial_usa_primeiro_snapshot_do_acompanhamento():
    session = FakeSession([[Decimal('9727809.96')]])

    valor = contas_pagar._valor_vencido_inicio_acompanhamento(session)

    sql, _ = session.executions[0]
    assert 'ORDER BY data_referencia ASC' in sql
    assert 'LIMIT 1' in sql
    assert valor == Decimal('9727809.96')


def test_divida_inicial_inexistente_retorna_none():
    session = FakeSession([[]])

    assert contas_pagar._valor_vencido_inicio_acompanhamento(session) is None


def test_pagamento_titulo_pode_ser_incluido_atualizado_e_excluido():
    usuario = SimpleNamespace(id=7)
    payload = contas_pagar.PagamentoTituloInput(
        data_pagamento='2026-09-17',
        valor_pago='1250.50',
        banco='Banco Pronto',
        agencia='0001',
        numero_conta='12345-6',
        observacao='Pagamento parcial',
    )
    inclusao = FakeSession([[31]])

    resposta = contas_pagar.registrar_pagamento_titulo(
        10, 99, payload, usuario, inclusao
    )

    assert resposta['id'] == 31
    assert inclusao.executions[0][1]['parcela'] == 99
    assert inclusao.executions[0][1]['banco'] == 'Banco Pronto'
    assert inclusao.executions[0][1]['agencia'] == '0001'
    assert inclusao.executions[0][1]['numero_conta'] == '12345-6'
    assert inclusao.commits == 1

    atualizacao = FakeSession([[31]])
    resposta = contas_pagar.atualizar_pagamento_titulo(
        10, 99, 31, payload, usuario, atualizacao
    )
    assert resposta['detail'] == 'Pagamento atualizado com sucesso.'
    assert atualizacao.commits == 1

    exclusao = FakeSession([[31]])
    resposta = contas_pagar.excluir_pagamento_titulo(
        10, 99, 31, usuario, exclusao
    )
    assert resposta.status_code == 204
    assert exclusao.commits == 1


def test_criticidade_e_atualizada_sem_sobrescrever_tratamento():
    session = FakeSession([[]])

    resposta = contas_pagar.atualizar_criticidade_fornecedor(
        10,
        contas_pagar.CriticidadeFornecedorInput(critico=True),
        SimpleNamespace(id=7),
        session,
    )

    sql, params = session.executions[0]
    assert 'critico = EXCLUDED.critico' in sql
    assert 'pagamento_imediato = EXCLUDED' not in sql
    assert params == {'codigo': 10, 'critico': True, 'usuario_id': 7}
    assert resposta['detail'] == 'Fornecedor marcado como crítico.'


def test_prioridade_ordena_criticos_e_depois_maior_atraso():
    fornecedores = [
        {
            'codigo': 1,
            'critico': False,
            'dias_atraso': 500,
            'valor_total_vencido': Decimal('1'),
        },
        {
            'codigo': 2,
            'critico': True,
            'dias_atraso': 30,
            'valor_total_vencido': Decimal('1'),
        },
        {
            'codigo': 3,
            'critico': True,
            'dias_atraso': 90,
            'valor_total_vencido': Decimal('1'),
        },
        {
            'codigo': 4,
            'critico': False,
            'dias_atraso': 100,
            'valor_total_vencido': Decimal('1'),
        },
    ]

    fornecedores.sort(key=contas_pagar._chave_prioridade_fornecedor)

    assert [item['codigo'] for item in fornecedores] == [3, 2, 1, 4]


def test_salvar_tratamento_faz_upsert_com_usuario():
    session = FakeSession([[]])
    payload = contas_pagar.TratamentoInput(
        critico=True,
        pagamento_imediato='100000',
        status='NEGOCIACAO',
        responsavel='Ana',
    )

    resposta = contas_pagar.salvar_tratamento(
        10, payload, SimpleNamespace(id=7), session
    )

    assert resposta['detail'] == 'Tratamento salvo com sucesso.'
    assert session.executions[0][1]['codigo'] == 10
    assert session.executions[0][1]['usuario'] == 7
    assert session.commits == 1


def test_excluir_remove_apenas_tratamento_operacional():
    session = FakeSession([[]])

    resposta = contas_pagar.excluir_tratamento(
        10, SimpleNamespace(id=7), session
    )

    sql, params = session.executions[0]
    assert 'contas_pagar_tratamentos' in sql
    assert 'HPC_V_CONTAS_A_PAGAR' not in sql
    assert params == {'codigo': 10}
    assert resposta.status_code == 204


def test_exportacao_completa_preserva_filtros_e_nao_registra_snapshot(
    monkeypatch,
):

    suppliers = []
    for codigo in range(1, 206):
        item = dict(
            codigo_fornecedor=codigo,
            nome_fornecedor='Fornecedor',
            dias_atraso=codigo,
            vencimento_mais_antigo=date(2026, 1, 1),
            titulos=[],
            titulos_vencidos=1,
            titulos_correntes=0,
        )
        for campo in (
            'valor_total',
            'valor_total_vencido',
            'valor_total_honrado',
            'saldo_a_pagar',
            'valor_a_pagar_ate_hoje',
            'valor_corrente',
            'novos_vencidos_7d',
        ):
            item[campo] = Decimal('10')
        suppliers.append(item)
    consulta = Mock(side_effect=lambda *a, **k: deepcopy(suppliers))
    detalhes = Mock(return_value=[])
    snapshot = Mock()
    monkeypatch.setattr(contas_pagar, '_consultar_oracle', consulta)
    monkeypatch.setattr(contas_pagar, '_consultar_titulos_oracle', detalhes)
    monkeypatch.setattr(contas_pagar, '_pagamentos_manuais', lambda s: {})
    monkeypatch.setattr(
        contas_pagar,
        '_tratamentos',
        lambda s: {
            i: {'critico': True, 'status': 'NEGOCIACAO'} for i in range(1, 151)
        },
    )
    monkeypatch.setattr(contas_pagar, '_registrar_snapshot', snapshot)
    monkeypatch.setattr(contas_pagar, '_historico', lambda s: [])
    monkeypatch.setattr(
        contas_pagar, '_valor_vencido_inicio_acompanhamento', lambda s: None
    )
    args = dict(
        _=None,
        session=None,
        oracle=None,
        q='Fornecedor',
        criticidade='criticos',
        status='NEGOCIACAO',
        page=9,
        page_size=20,
    )
    result = contas_pagar.listar_contas_pagar(**args, exportar=True)
    assert result['resumo']['valor_a_pagar_ate_hoje'] == '2050.00'
    assert result['total'] == 150
    assert len(result['fornecedores']) == 150
    assert result['total_pages'] == 1
    assert result['page'] == 1
    assert result['fornecedores'][0]['codigo_fornecedor'] == 150
    assert result['fornecedores'][-1]['codigo_fornecedor'] == 1
    consulta.assert_called_once()
    detalhes.assert_called_once()
    assert len(detalhes.call_args.args[1]) == 150
    snapshot.assert_not_called()
    args['page'] = 1
    paged = contas_pagar.listar_contas_pagar(**args)
    assert len(paged['fornecedores']) == 20
    assert paged['total_pages'] == 8
    assert paged['resumo']['valor_a_pagar_ate_hoje'] == '2050.00'
    snapshot.assert_called_once()


def test_titulos_exportacao_respeita_limite_oracle():
    oracle = FakeSession([[], []])
    assert (
        contas_pagar._consultar_titulos_oracle(oracle, list(range(1001))) == []
    )
    assert [
        len(params['codigos_fornecedor']) for _, params in oracle.executions
    ] == [1000, 1]


def test_total_ate_hoje_inclui_hoje_exclui_futuro_e_desconta_pagamentos(
    monkeypatch,
):
    monkeypatch.setattr(
        contas_pagar, '_hoje_hospital', lambda: date(2026, 10, 22)
    )
    titulos = [
        dict(
            codigo_fornecedor=1,
            nome_fornecedor='Teste',
            codigo_parcela=n,
            valor_total=Decimal('100'),
            valor_honrado_oracle=Decimal('20'),
            data_vencimento=day,
        )
        for n, day in enumerate(
            (date(2026, 10, 21), date(2026, 10, 22), date(2026, 10, 23), None),
            1,
        )
    ]
    result = contas_pagar._agrupar_fornecedores(
        titulos, {2: [{'valor_pago': Decimal('30')}]}
    )[0]
    assert result['valor_a_pagar_ate_hoje'] == Decimal('130')
    assert result['valor_total_vencido'] == Decimal('80')
    monkeypatch.setattr(
        contas_pagar, '_hoje_hospital', lambda: date(2026, 10, 23)
    )
    assert contas_pagar._agrupar_fornecedores(titulos, {})[0][
        'valor_a_pagar_ate_hoje'
    ] == Decimal('240')
