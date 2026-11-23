# ruff: noqa: PLR2004, E501
import asyncio
from datetime import date, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app_prontocardio.routers import contas_pagar_extratos as rota
from app_prontocardio.services.extratos_bancarios import Extrato, LinhaExtrato


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


USUARIO = SimpleNamespace(id=1, nome='Financeiro', perfil='ti', telas_permitidas=[])
CONTAS = [
    {'cd_con_cor': 3, 'ds_con_cor': '05-BB C/C 59138-6', 'cd_banco': 1, 'cd_agencia': '2917', 'nr_conta': '59138', 'cd_multi_empresa': 1, 'baixas_recentes': 0},
    {'cd_con_cor': 20, 'ds_con_cor': '05 BB', 'cd_banco': 1, 'cd_agencia': '2917', 'nr_conta': '59138', 'cd_multi_empresa': 2, 'baixas_recentes': 207},
    {'cd_con_cor': 13, 'ds_con_cor': '05-SANTANDER', 'cd_banco': 33, 'cd_agencia': '700', 'nr_conta': '13000656', 'cd_multi_empresa': 1, 'baixas_recentes': 0},
    {'cd_con_cor': 36, 'ds_con_cor': '05-SANTANDER APLICACAO', 'cd_banco': 33, 'cd_agencia': '0700', 'nr_conta': '130006564', 'cd_multi_empresa': 2, 'baixas_recentes': 0},
    {'cd_con_cor': 1, 'ds_con_cor': '08-BB C/C 211433-X', 'cd_banco': 1, 'cd_agencia': '2917', 'nr_conta': '211433', 'cd_multi_empresa': 1, 'baixas_recentes': 2424},
]


def _extrato(banco=1, conta='59138', dv='6', linhas=()):
    return Extrato(banco=banco, agencia='2917', conta=conta, conta_dv=dv, linhas=list(linhas),
                   periodo_inicio=date(2026, 11, 18), periodo_fim=date(2026, 11, 19),
                   saldo_inicial=Decimal('0'), saldo_final=Decimal('0'), conferido=True)


def test_conta_do_mv_mais_usada_nas_baixas_vem_primeiro():
    candidatas = rota.contas_candidatas(_extrato(), CONTAS)
    assert [c['cd_con_cor'] for c in candidatas] == [20, 3]


def test_conta_com_digito_colado_e_aplicacao_por_ultimo():
    candidatas = rota.contas_candidatas(_extrato(banco=33, conta='13000656', dv='4'), CONTAS)
    assert [c['cd_con_cor'] for c in candidatas] == [13, 36]


def test_conta_fora_do_mv_nao_tem_candidata():
    assert rota.contas_candidatas(_extrato(conta='99999'), CONTAS) == []


def test_linhas_identicas_no_mesmo_extrato_tem_chaves_diferentes_e_estaveis():
    linha = LinhaExtrato(date(2026, 11, 11), 'DB CX CAP', '369639', Decimal('-418.57'))
    extrato = _extrato(linhas=[linha, linha])
    chaves = rota._chaves(extrato)
    assert chaves[0] != chaves[1]
    # A continuação do histórico muda entre a impressão diária e a mensal.
    continuada = LinhaExtrato(linha.data, 'DB CX CAP', '0369639', linha.valor)
    continuada.historico += ' mais texto da continuação'
    outra = _extrato(linhas=[continuada])
    assert rota._chaves(outra)[0] == chaves[0]


def _titulo(cd, valor, vencimento, pago='0', fornecedor='FORNECEDOR'):
    return {'cd_itcon_pag': cd, 'cd_con_pag': cd * 10, 'nr_parcela': 1, 'dt_vencimento': datetime.combine(vencimento, datetime.min.time()),
            'dt_prevista_pag': None, 'vl_duplicata': Decimal(valor), 'vl_soma_pago': Decimal(pago),
            'cd_fornecedor': 5, 'nm_fornecedor': fornecedor, 'nr_documento': '123', 'ds_con_pag': 'SERVICO', 'cd_multi_empresa': 1}


def _debito(i, valor, dia):
    return {'id': i, 'data': date(2026, 11, dia), 'historico': 'Pagamento de Boleto', 'documento': None,
            'valor': Decimal(valor), 'extrato_id': 9, 'nome_arquivo': '08 BB.pdf'}


def test_sugestao_por_valor_e_janela_do_vencimento():
    titulos = [
        _titulo(1, '450.00', date(2026, 11, 11)),            # +7 dias: dentro
        _titulo(2, '700.00', date(2026, 11, 1)),             # +17 dias: fora da janela
        _titulo(3, '1000.00', date(2026, 11, 18), pago='400.00'),  # saldo 600
    ]
    sugestoes = rota.sugerir_baixas(
        [_debito(1, '-450.00', 18), _debito(2, '-700.00', 18), _debito(3, '-600.00', 18)], titulos
    )
    assert [s['situacao'] for s in sugestoes] == ['unico', 'sem_titulo', 'unico']
    assert sugestoes[0]['candidatos'][0]['dias'] == 7
    assert sugestoes[2]['candidatos'][0]['saldo'] == Decimal('600.00')


def test_valores_repetidos_ficam_ambiguos_e_nada_e_escolhido_sozinho():
    titulos = [_titulo(1, '252.00', date(2026, 11, 18)), _titulo(2, '252.00', date(2026, 11, 16))]
    sugestoes = rota.sugerir_baixas([_debito(1, '-252.00', 18)], titulos)
    assert sugestoes[0]['situacao'] == 'ambiguo'
    assert [c['cd_itcon_pag'] for c in sugestoes[0]['candidatos']] == [1, 2]  # mais próximo primeiro

    disputa = rota.sugerir_baixas([_debito(1, '-2180.40', 18), _debito(2, '-2180.40', 18)],
                                  [_titulo(7, '2180.40', date(2026, 11, 18))])
    assert [s['situacao'] for s in disputa] == ['ambiguo', 'ambiguo']
    assert disputa[0]['titulos_em_outros_debitos'] == [7]


LINHA = {'id': 5, 'data': date(2026, 11, 18), 'historico': 'Pagamento de Boleto', 'documento': None,
         'valor': Decimal('-450.00'), 'repetida': False, 'extrato_id': 9, 'cd_con_cor': 1,
         'cd_multi_empresa': 1, 'nome_arquivo': '08 BB.pdf'}
TITULO = {**_titulo(222704, '450.00', date(2026, 11, 11)), 'tp_quitacao': None, 'cd_status': None,
          'cd_itcon_pag_estorno': None, 'cd_lote_pgto': None, 'notas_credito': 0}


def _mv(monkeypatch, titulo):
    respostas = {
        id(rota.SQL_TITULO): [titulo] if titulo else [],
        id(rota.SQL_LANCAMENTO_DEBITO): [{'cd_lan_concor': 14, 'ds_lan_concor': 'PAGTO DEBITO C/C'}],
        id(rota.SQL_MOEDA_EMPRESA): [{'cd_moeda': 'R$'}],
        id(rota.SQL_PROCESSOS_CONTA): [{'processo_pagamento': 28, 'processo_movimento': 547}],
    }
    monkeypatch.setattr(rota, 'read_payables_rows', lambda oracle, sql, params: respostas[id(sql)])


def test_simulacao_monta_o_que_a_o_pag_gravaria_sem_gravar(monkeypatch):
    _mv(monkeypatch, TITULO)
    postgres = FakeSession([[LINHA]])
    resposta = rota.simular_baixa(5, rota.SimulacaoInput(cd_itcon_pag=222704), USUARIO, postgres, object())

    assert resposta['pode_baixar'] is True
    assert resposta['gravacao_habilitada'] is False
    assert resposta['pagcon_pag']['vl_pago'] == Decimal('450.00')
    assert resposta['pagcon_pag']['tp_pagamento'] == '5'
    assert resposta['pagcon_pag']['cd_processo'] == 28
    assert resposta['mov_concor']['cd_lan_concor'] == 14
    assert resposta['mov_concor']['nr_documento_identificacao'] == 'DEB C/C- 222704'
    assert resposta['mov_concor']['cd_processo'] == 547
    assert postgres.commits == 0
    assert all(sql.lstrip().upper().startswith('SELECT') for sql, _ in postgres.executions)


@pytest.mark.parametrize(
    ('linha', 'titulo', 'pendencia'),
    [
        ({}, {'tp_quitacao': 'Q'}, 'já está quitada'),
        ({}, {'vl_soma_pago': Decimal('100')}, 'é diferente do débito'),
        ({'repetida': True}, {}, 'já veio em outro extrato'),
        ({'valor': Decimal('450.00')}, {}, 'não é um débito'),
        ({'cd_multi_empresa': 2}, {}, 'outra empresa'),
        ({'cd_con_cor': None}, {}, 'não está ligado a uma conta'),
        ({}, {'cd_itcon_pag_estorno': 1}, 'estornada'),
    ],
)
def test_simulacao_aponta_pendencias(monkeypatch, linha, titulo, pendencia):
    _mv(monkeypatch, {**TITULO, **titulo})
    resposta = rota.simular_baixa(5, rota.SimulacaoInput(cd_itcon_pag=222704), USUARIO,
                                  FakeSession([[{**LINHA, **linha}]]), object())
    assert resposta['pode_baixar'] is False
    assert any(pendencia in p for p in resposta['pendencias'])


def test_simulacao_avisa_lote_de_pagamento(monkeypatch):
    _mv(monkeypatch, {**TITULO, 'cd_lote_pgto': 743})
    resposta = rota.simular_baixa(5, rota.SimulacaoInput(cd_itcon_pag=222704), USUARIO, FakeSession([[LINHA]]), object())
    assert resposta['pode_baixar'] is True
    assert any('lote de pagamento 743' in a for a in resposta['avisos'])


class Arquivo:
    filename = '08 BB.pdf'

    def __init__(self, conteudo):
        self.conteudo = conteudo

    async def read(self, limite):
        return self.conteudo[:limite]


def test_envio_grava_extrato_linhas_e_marca_repetidas(monkeypatch):
    linhas = [LinhaExtrato(date(2026, 11, 18), 'Pagamento de Boleto', '811', Decimal('-450.00')),
              LinhaExtrato(date(2026, 11, 19), 'Pix - Recebido', None, Decimal('100.00'))]
    extrato = _extrato(conta='211433', dv='X', linhas=linhas)
    monkeypatch.setattr(rota, 'ler_extrato', lambda conteudo, nome: extrato)
    monkeypatch.setattr(rota, 'contas_mv', lambda oracle: CONTAS)
    ja_existe = rota._chaves(extrato)[0]
    postgres = FakeSession([[], [ja_existe], [77]])

    resposta = asyncio.run(rota.enviar_extrato(USUARIO, postgres, object(), Arquivo(b'%PDF-1.4 conteudo')))

    assert resposta['id'] == 77
    assert resposta['cd_con_cor'] == 1
    assert resposta['linhas_repetidas'] == 1
    inseridas = [p for sql, p in postgres.executions if 'contas_pagar_extrato_linhas' in sql and 'INSERT' in sql]
    assert [(p['ordem'], p['repetida']) for p in inseridas] == [(1, True), (2, False)]
    extrato_inserido = next(p for sql, p in postgres.executions if 'INSERT INTO api_prontocardio.contas_pagar_extratos' in sql)
    assert extrato_inserido['cd_multi_empresa'] == 1
    assert extrato_inserido['arquivo'] == b'%PDF-1.4 conteudo'
    assert postgres.commits == 1


def test_mesmo_arquivo_duas_vezes_e_recusado(monkeypatch):
    postgres = FakeSession([[{'id': 3, 'nome_arquivo': '08 BB.pdf'}]])
    with pytest.raises(HTTPException) as erro:
        asyncio.run(rota.enviar_extrato(USUARIO, postgres, object(), Arquivo(b'%PDF-1.4')))
    assert erro.value.status_code == 409


def test_extrato_que_nao_fecha_nao_e_gravado(monkeypatch):
    def recusar(conteudo, nome):
        raise rota.ExtratoInvalido('O extrato não fecha.')
    monkeypatch.setattr(rota, 'ler_extrato', recusar)
    postgres = FakeSession([[]])
    with pytest.raises(HTTPException) as erro:
        asyncio.run(rota.enviar_extrato(USUARIO, postgres, object(), Arquivo(b'%PDF-1.4')))
    assert erro.value.status_code == 422
    assert postgres.commits == 0


def test_conta_fora_do_mv_fica_guardada_com_aviso(monkeypatch):
    monkeypatch.setattr(rota, 'ler_extrato', lambda conteudo, nome: _extrato(conta='586434108', dv='4'))
    monkeypatch.setattr(rota, 'contas_mv', lambda oracle: CONTAS)
    resposta = asyncio.run(rota.enviar_extrato(USUARIO, FakeSession([[], [55]]), object(), Arquivo(b'%PDF')))
    assert resposta['cd_con_cor'] is None
    assert any('não cadastrada no MV' in a for a in resposta['avisos'])


def test_sugestoes_exigem_periodo_curto():
    with pytest.raises(HTTPException) as erro:
        rota.sugestoes_de_baixa(USUARIO, FakeSession([]), object(), 1, date(2026, 1, 1), date(2026, 6, 1))
    assert erro.value.status_code == 422
