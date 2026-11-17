# ruff: noqa: PLR2004, E501
import asyncio
import json
from datetime import date, timedelta
from decimal import Decimal
from io import BytesIO
from types import SimpleNamespace

import pytest
from fastapi import HTTPException, UploadFile
from sqlalchemy.exc import IntegrityError

from app_prontocardio.routers import contas_pagar_boletos as rota
from app_prontocardio.routers import contas_pagar_nfse as rota_nfse
from tests.test_payables_boleto import (
    formatar,
    gerar_linha_bancaria,
    gerar_pdf,
)

HOJE = date(2026, 11, 17)
FATOR_HOJE = (HOJE - date(2025, 2, 22)).days + 1000


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
        if isinstance(self.rows, Exception):
            raise self.rows
        return self.rows[0]


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.executions = []
        self.commits = 0
        self.rollbacks = 0

    def execute(self, statement, params=None):
        self.executions.append((str(statement), params))
        resposta = self.responses.pop(0) if self.responses else []
        if isinstance(resposta, Exception):
            raise resposta
        return FakeResult(resposta)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def _usuario(perfil='usuario', telas=(rota_nfse.TELA_SETOR,), uid=7):
    return SimpleNamespace(id=uid, nome='Setor', perfil=perfil, telas_permitidas=list(telas))


NOTA = {'prestador_cnpj': '11111111000111', 'prestador_razao_social': 'Software SA',
        'numero_nfse': '123', 'valor_servicos': '1600.00', 'valor_liquido_nfse': '1500.50',
        'tomador_cnpj': '08711085000128'}
EMPRESAS = [{'cd_multi_empresa': 1, 'ds_multi_empresa': 'HOSPITAL', 'cnpj': '08711085000128'}]
REGRAS = [{'cnpj': '11111111000111', 'cd_multi_empresa': 1, 'cd_oficina': 2, 'ds_oficina': 'TI',
           'status': 'definida', 'motivo': 'ok'}]


def _pdf(valor=Decimal('1500.50'), dias=15, cnpj='11.111.111/0001-11', impresso='1500,50'):
    linha = gerar_linha_bancaria(valor=valor, fator=FATOR_HOJE + dias)
    return gerar_pdf(f'CNPJ: {cnpj}', 'Valor do Documento', impresso, formatar(linha)), linha


def _enviar(postgres, oracle, conteudo, usuario=None, **kwargs):
    arquivo = UploadFile(BytesIO(conteudo), filename='boleto.pdf')
    return asyncio.run(rota.enviar_boleto('11.111.111/0001-11', '123', usuario or _usuario(),
                                          postgres, oracle, arquivo, **kwargs))


@pytest.fixture(autouse=True)
def _hoje(monkeypatch):
    monkeypatch.setattr(rota, '_hoje', lambda: HOJE)


def test_setor_da_oficina_anexa_boleto_sem_divergencia():
    pdf, linha = _pdf()
    postgres = FakeSession([[NOTA], REGRAS, [2], [], [99]])
    resposta = _enviar(postgres, FakeSession([EMPRESAS]), pdf)

    assert resposta['id'] == 99
    assert resposta['divergencias'] == []
    assert resposta['origem_leitura'] == 'pdf'
    assert resposta['vencimento'] == HOJE + timedelta(days=15)
    insert = postgres.executions[-1][1]
    assert insert['linha_digitavel'] == linha
    assert insert['empresa'] == 1
    assert insert['valor'] == Decimal('1500.50')
    assert insert['arquivo'] == pdf
    assert len(insert['sha256']) == 64
    assert postgres.commits == 1


def test_divergencias_ficam_registradas():
    pdf, _ = _pdf(valor=Decimal('1700.00'), dias=-2, cnpj='99.999.999/0001-99', impresso='1600,00')
    postgres = FakeSession([[NOTA], REGRAS, [2], [], [5]])
    resposta = _enviar(postgres, FakeSession([EMPRESAS]), pdf)

    assert len(resposta['divergencias']) == 4
    texto = ' '.join(resposta['divergencias'])
    assert 'CNPJ do prestador' in texto
    assert 'Valor do Documento' in texto
    assert 'passa do valor líquido' in texto
    assert 'vencido' in texto
    assert json.loads(postgres.executions[-1][1]['divergencias']) == resposta['divergencias']


def test_soma_com_boletos_anteriores_respeita_o_liquido():
    pdf, _ = _pdf(valor=Decimal('600.00'), impresso='600,00')
    anteriores = [{'valor': Decimal('1000.00')}]
    postgres = FakeSession([[NOTA], REGRAS, [2], anteriores, [6]])
    resposta = _enviar(postgres, FakeSession([EMPRESAS]), pdf)

    assert any('passa do valor líquido' in d for d in resposta['divergencias'])


def test_pdf_escaneado_aceita_linha_digitada():
    _, linha = _pdf()
    escaneado = gerar_pdf('imagem do boleto')
    postgres = FakeSession([[NOTA], REGRAS, [2], [], [7]])
    resposta = _enviar(postgres, FakeSession([EMPRESAS]), escaneado, linha_digitavel=formatar(linha))

    assert resposta['origem_leitura'] == 'manual'
    assert resposta['linha_digitavel'] == linha


def test_pdf_sem_linha_e_sem_digitacao_e_rejeitado():
    postgres = FakeSession([[NOTA], REGRAS, [2]])
    with pytest.raises(HTTPException) as erro:
        _enviar(postgres, FakeSession([EMPRESAS]), gerar_pdf('imagem do boleto'))
    assert erro.value.status_code == 422
    assert 'Informe-a manualmente' in erro.value.detail


def test_arquivo_nao_pdf_e_rejeitado():
    postgres = FakeSession([[NOTA], REGRAS, [2]])
    with pytest.raises(HTTPException) as erro:
        _enviar(postgres, FakeSession([EMPRESAS]), b'PK\x03\x04 planilha')
    assert erro.value.status_code == 422


def test_outro_setor_nao_pode_anexar():
    pdf, _ = _pdf()
    postgres = FakeSession([[NOTA], REGRAS, [5]])
    with pytest.raises(HTTPException) as erro:
        _enviar(postgres, FakeSession([EMPRESAS]), pdf)
    assert erro.value.status_code == 403


def test_boleto_repetido_retorna_conflito():
    pdf, _ = _pdf()
    postgres = FakeSession([[NOTA], REGRAS, [2], [], IntegrityError('dup', {}, Exception())])
    with pytest.raises(HTTPException) as erro:
        _enviar(postgres, FakeSession([EMPRESAS]), pdf)
    assert erro.value.status_code == 409
    assert postgres.rollbacks == 1


def test_nota_inexistente():
    with pytest.raises(HTTPException) as erro:
        _enviar(FakeSession([[]]), FakeSession([]), _pdf()[0])
    assert erro.value.status_code == 404


def test_resumo_marca_pronta_so_sem_divergencia():
    ok = [{'valor': Decimal('1500.50'), 'divergencias': []}]
    ruim = [{'valor': Decimal('1500.50'), 'divergencias': ['Boleto vencido.']}]

    assert rota.resumo_boletos(ok, NOTA)['pronta'] is True
    assert rota.resumo_boletos(ruim, NOTA)['pronta'] is False
    assert rota.resumo_boletos([], NOTA)['pronta'] is False


def test_fila_filtra_notas_prontas():
    nota_fila = {**NOTA, 'codigo_verificacao_nfse': 'A', 'data_hora': None, 'competencia_nfse': None,
                 'tomador_razao_social': 'Hospital', 'discriminacao': ''}
    sem_boleto = {**nota_fila, 'prestador_cnpj': '11111111000111', 'numero_nfse': '124'}
    regras = [{**REGRAS[0], 'nm_fornecedor': 'Software SA', 'origem': 'historico', 'qtd_entradas': 3,
               'participacao': Decimal('1'), 'usuario_id': 1, 'atualizado_em': None, 'cd_fornecedor': 1}]
    boletos = [{'prestador_cnpj': '11111111000111', 'numero_nfse': '123', 'quantidade': 1,
                'soma': Decimal('1500.50'), 'primeiro_vencimento': HOJE, 'com_divergencia': False}]
    postgres = FakeSession([[True], [nota_fila, sem_boleto], regras, boletos])
    oracle = FakeSession([[], EMPRESAS])

    resposta = rota_nfse.listar_recebidas(_usuario(perfil='ti'), postgres, oracle, situacao='pronta')

    assert [i['numero_nfse'] for i in resposta['itens']] == ['123']
    assert resposta['itens'][0]['pronta'] is True
    assert resposta['itens'][0]['boletos']['soma'] == Decimal('1500.50')
