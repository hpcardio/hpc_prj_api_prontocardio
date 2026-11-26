# ruff: noqa: PLR2004, E501, PT018
"""Contas a Receber — entrega 2: vínculos manuais (protocolo, solicitação, NFS) com auditoria."""

import json
from datetime import date, datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app_prontocardio.routers import contas_receber_vinculos as rota

USUARIO = SimpleNamespace(id=7, nome='Financeiro', perfil='usuario', telas_permitidas=['contas_receber_fluxo'])
REMESSA = {'cd_remessa': 501, 'cd_convenio': 46, 'convenio': 'ISSEC'}
VINCULO = {'cd_remessa': 501, 'origem_vinculo': 'planilha', 'id': 10, 'cd_convenio': 46, 'numero': 'P-1',
           'origem': 'planilha', 'data_protocolo': date(2026, 10, 2), 'solicitacao_nota_em': None,
           'solicitacao_nota_obs': None}
NOTA = {'numero_nfse': '30083', 'prestador_cnpj': '08711085000128', 'prestador_razao_social': 'HOSPITAL',
        'tomador_cnpj': '30974559000183', 'tomador_razao_social': 'FASSEC', 'valor_servicos': '2118.58',
        'valor_liquido_nfse': '2029.60', 'data_hora': datetime(2026, 11, 10, 15, 0, tzinfo=timezone.utc),
        'row_hash': 'abc', 'discriminacao': 'Serviços hospitalares'}


class FakeResult:
    def __init__(self, rows):
        self.rows = rows

    def mappings(self):
        return self

    def all(self):
        return self.rows

    def first(self):
        return self.rows[0] if self.rows else None

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


@pytest.fixture(autouse=True)
def _remessa_mv(monkeypatch):
    monkeypatch.setattr(rota, 'remessas_mv', lambda _oracle: [REMESSA])


def _eventos(postgres):
    return [p for sql, p in postgres.executions if 'INSERT INTO api_prontocardio.cr_eventos' in sql]


def test_vincula_protocolo_novo_com_auditoria():
    postgres = FakeSession([[], [10]])  # sem vínculo; id do protocolo
    resposta = rota.vincular_protocolo(501, rota.ProtocoloInput(numero='  P-2 ', data_protocolo=date(2026, 10, 5)),
                                       None, postgres, USUARIO)

    assert resposta['protocolo_id'] == 10 and resposta['numero'] == 'P-2' and resposta['origem'] == 'manual'
    evento, = _eventos(postgres)
    assert evento['entidade'] == 'protocolo_remessa' and evento['chave'] == '501' and evento['acao'] == 'criar'
    assert evento['antes'] is None and json.loads(evento['depois'])['numero'] == 'P-2'
    assert postgres.commits == 1


def test_confirmar_sugestao_do_portal_grava_origem_portal():
    postgres = FakeSession([[], [11]])
    resposta = rota.vincular_protocolo(501, rota.ProtocoloInput(numero='P-9', confirmado_do_portal=True),
                                       None, postgres, USUARIO)
    assert resposta['origem'] == 'portal_issec'


def test_trocar_protocolo_registra_o_anterior():
    postgres = FakeSession([[VINCULO], [12]])
    rota.vincular_protocolo(501, rota.ProtocoloInput(numero='P-3'), None, postgres, USUARIO)

    evento, = _eventos(postgres)
    assert evento['acao'] == 'editar'
    assert json.loads(evento['antes'])['numero'] == 'P-1'


def test_remessa_fora_do_mv():
    with pytest.raises(HTTPException) as erro:
        rota.vincular_protocolo(999, rota.ProtocoloInput(numero='P'), None, FakeSession([]), USUARIO)
    assert erro.value.status_code == 404


def test_remover_protocolo():
    postgres = FakeSession([[VINCULO]])
    assert rota.remover_protocolo(501, postgres, USUARIO)['removido'] is True
    assert any('DELETE FROM api_prontocardio.cr_protocolo_remessas' in sql for sql, _ in postgres.executions)
    assert _eventos(postgres)[0]['acao'] == 'remover'

    with pytest.raises(HTTPException) as erro:
        rota.remover_protocolo(501, FakeSession([[]]), USUARIO)
    assert erro.value.status_code == 404


def test_solicitacao_no_protocolo_gravado():
    postgres = FakeSession([[VINCULO]])
    resposta = rota.registrar_solicitacao(501, rota.SolicitacaoInput(data=date(2026, 11, 20), observacao='  pediu  por e-mail '),
                                          None, postgres, USUARIO)
    assert resposta == {'protocolo_id': 10, 'data': date(2026, 11, 20), 'observacao': 'pediu por e-mail'}
    evento, = _eventos(postgres)
    assert evento['entidade'] == 'solicitacao_nota' and evento['acao'] == 'criar'


def test_solicitacao_sem_protocolo_pede_o_protocolo():
    with pytest.raises(HTTPException) as erro:
        rota.registrar_solicitacao(501, rota.SolicitacaoInput(data=date(2026, 11, 20)), None, FakeSession([[]]), USUARIO)
    assert erro.value.status_code == 409


def test_solicitacao_grava_o_protocolo_sugerido_pelo_portal():
    postgres = FakeSession([[], [13]])  # sem vínculo; cria protocolo 13
    rota.registrar_solicitacao(501, rota.SolicitacaoInput(data=date(2026, 11, 20), protocolo_numero='P-7'),
                               None, postgres, USUARIO)
    entidades = [e['entidade'] for e in _eventos(postgres)]
    assert entidades == ['protocolo_remessa', 'solicitacao_nota']
    assert json.loads(_eventos(postgres)[0]['depois'])['origem'] == 'portal_issec'


def test_remover_solicitacao():
    postgres = FakeSession([[{**VINCULO, 'solicitacao_nota_em': date(2026, 11, 20), 'solicitacao_nota_obs': 'x'}]])
    rota.remover_solicitacao(501, postgres, USUARIO)
    assert _eventos(postgres)[0]['acao'] == 'remover'


def test_vincular_nfse_usa_os_dados_do_iss():
    postgres = FakeSession([[NOTA], [VINCULO], []])  # nota; vínculo; sem NF anterior
    resposta = rota.vincular_nfse(501, rota.NfseInput(prestador_cnpj='08711085000128', numero_nfse='30083'),
                                  None, postgres, USUARIO)
    assert resposta['valor'] == Decimal('2118.58')
    assert resposta['data_emissao'] == date(2026, 11, 10)
    gravado = next(p for sql, p in postgres.executions if 'INSERT INTO api_prontocardio.cr_protocolo_nfse' in sql)
    assert gravado['row_hash'] == 'abc' and gravado['protocolo_id'] == 10
    assert _eventos(postgres)[0]['entidade'] == 'protocolo_nfse'


def test_vincular_nfse_inexistente_ou_cancelada():
    with pytest.raises(HTTPException) as erro:
        rota.vincular_nfse(501, rota.NfseInput(prestador_cnpj='08711085000128', numero_nfse='1'),
                           None, FakeSession([[]]), USUARIO)
    assert erro.value.status_code == 404


def test_remover_nfse():
    postgres = FakeSession([[VINCULO], [{'protocolo_id': 10, 'numero_nfse': '30083'}]])
    rota.remover_nfse(501, postgres, USUARIO)
    assert _eventos(postgres)[0]['acao'] == 'remover'


def test_busca_nfse_por_valor_e_mostra_protocolos_ja_ligados():
    postgres = FakeSession([[NOTA], [{'prestador_cnpj': '08711085000128', 'numero_nfse': '30083', 'protocolo': 'P-1', 'cd_convenio': 46}]])
    resposta = rota.buscar_nfse(postgres, USUARIO, q='2.118,58')

    params = postgres.executions[0][1]
    assert params['valor'] == Decimal('2118.58')
    assert params['prestadores'] == list(rota.PRESTADORES_HOSPITAL)
    nota, = resposta['notas']
    assert nota['numero_nfse'] == '30083' and nota['protocolos_vinculados'] == ['P-1']
    assert nota['valor_liquido'] == Decimal('2029.60')


def test_busca_nfse_por_numero():
    postgres = FakeSession([[]])
    rota.buscar_nfse(postgres, USUARIO, q='030083')
    params = postgres.executions[0][1]
    assert params['numero'] == '30083' and params['valor'] is None
