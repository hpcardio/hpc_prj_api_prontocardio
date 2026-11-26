# ruff: noqa: PLR2004, E501
import asyncio
import json
from datetime import date, datetime, timedelta
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

    def scalar_one_or_none(self):
        return self.rows[0] if self.rows else None

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


NOTA = {'prestador_cnpj': '11222333000181', 'prestador_razao_social': 'Software SA',
        'numero_nfse': '123', 'valor_servicos': '1600.00', 'valor_liquido_nfse': '1500.50',
        'tomador_cnpj': '08711085000128'}
EMPRESAS = [{'cd_multi_empresa': 1, 'ds_multi_empresa': 'HOSPITAL', 'cnpj': '08711085000128'}]
REGRAS = [{'cnpj': '11222333000181', 'cd_multi_empresa': 1, 'cd_oficina': 2, 'ds_oficina': 'TI',
           'status': 'definida', 'motivo': 'ok'}]


def _pdf(valor=Decimal('1500.50'), dias=15, cnpj='11.222.333/0001-81', impresso='1500,50'):
    linha = gerar_linha_bancaria(valor=valor, fator=FATOR_HOJE + dias)
    return gerar_pdf(f'CNPJ: {cnpj}', 'Valor do Documento', impresso, formatar(linha)), linha


OBSERVACAO = 'Transporte de pacientes do mês de novembro, contrato 12/2026'


def _enviar(postgres, oracle, conteudo, usuario=None, **kwargs):
    kwargs.setdefault('observacao', OBSERVACAO)
    arquivo = UploadFile(BytesIO(conteudo), filename='boleto.pdf')
    return asyncio.run(rota.enviar_boleto('11.222.333/0001-81', '123', usuario or _usuario(),
                                          postgres, oracle, arquivo, **kwargs))


@pytest.fixture(autouse=True)
def _hoje(monkeypatch):
    monkeypatch.setattr(rota, '_hoje', lambda: HOJE)
    monkeypatch.setattr(rota, '_agora_local', lambda: datetime(2026, 11, 17, 10, 0))  # terça


@pytest.fixture(autouse=True)
def _nota_normal(monkeypatch, request):
    if request.node.name != 'test_status_da_nota_combina_xml_e_portal':
        monkeypatch.setattr(rota, 'status_da_nota', lambda *a: 'NORMAL')


def test_setor_da_oficina_anexa_boleto_sem_divergencia():
    pdf, linha = _pdf()
    postgres = FakeSession([[NOTA], REGRAS, [2], [], [], [99]])
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
    pdf, _ = _pdf(valor=Decimal('1700.00'), dias=-2, cnpj='63.554.067/0001-98', impresso='1600,00')
    postgres = FakeSession([[NOTA], REGRAS, [2], [], [], [5]])
    resposta = _enviar(postgres, FakeSession([EMPRESAS]), pdf, justificativa_atraso='Fornecedor mandou o boleto atrasado')

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
    postgres = FakeSession([[NOTA], REGRAS, [2], [], anteriores, [6]])
    resposta = _enviar(postgres, FakeSession([EMPRESAS]), pdf)

    assert any('passa do valor líquido' in d for d in resposta['divergencias'])


def test_pdf_escaneado_aceita_linha_digitada():
    _, linha = _pdf()
    escaneado = gerar_pdf('imagem do boleto')
    postgres = FakeSession([[NOTA], REGRAS, [2], [], [], [7]])
    resposta = _enviar(postgres, FakeSession([EMPRESAS]), escaneado, linha_digitavel=formatar(linha))

    assert resposta['origem_leitura'] == 'manual'
    assert resposta['linha_digitavel'] == linha


def test_pdf_sem_linha_e_sem_digitacao_e_rejeitado():
    postgres = FakeSession([[NOTA], REGRAS, [2], []])
    with pytest.raises(HTTPException) as erro:
        _enviar(postgres, FakeSession([EMPRESAS]), gerar_pdf('imagem do boleto'))
    assert erro.value.status_code == 422
    assert 'Informe-a manualmente' in erro.value.detail


def test_arquivo_nao_pdf_e_rejeitado():
    postgres = FakeSession([[NOTA], REGRAS, [2], []])
    with pytest.raises(HTTPException) as erro:
        _enviar(postgres, FakeSession([EMPRESAS]), b'PK\x03\x04 planilha')
    assert erro.value.status_code == 422


def test_outro_setor_nao_pode_anexar():
    pdf, _ = _pdf()
    postgres = FakeSession([[NOTA], REGRAS, [5], []])
    with pytest.raises(HTTPException) as erro:
        _enviar(postgres, FakeSession([EMPRESAS]), pdf)
    assert erro.value.status_code == 403


def test_boleto_repetido_retorna_conflito():
    pdf, _ = _pdf()
    postgres = FakeSession([[NOTA], REGRAS, [2], [], [], IntegrityError('dup', {}, Exception())])
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
                 'tomador_razao_social': 'Hospital', 'discriminacao': '',
                 'situacao_xml': None, 'situacao_portal': None, 'situacao_consultada_em': None}
    sem_boleto = {**nota_fila, 'prestador_cnpj': '11222333000181', 'numero_nfse': '124'}
    regras = [{**REGRAS[0], 'nm_fornecedor': 'Software SA', 'origem': 'historico', 'qtd_entradas': 3,
               'participacao': Decimal('1'), 'usuario_id': 1, 'atualizado_em': None, 'cd_fornecedor': 1}]
    boletos = [{'prestador_cnpj': '11222333000181', 'numero_nfse': '123', 'quantidade': 1,
                'soma': Decimal('1500.50'), 'primeiro_vencimento': HOJE, 'com_divergencia': False, 'fora_do_prazo': True}]
    postgres = FakeSession([[True], [False], [nota_fila, sem_boleto], regras, [], boletos])
    oracle = FakeSession([[], EMPRESAS])

    resposta = rota_nfse.listar_recebidas(_usuario(perfil='ti'), postgres, oracle, situacao='pronta')

    assert [i['numero_nfse'] for i in resposta['itens']] == ['123']
    assert resposta['itens'][0]['pronta'] is True
    assert resposta['itens'][0]['boletos']['soma'] == Decimal('1500.50')
    assert resposta['itens'][0]['boletos']['fora_do_prazo'] is True  # a fila mostra "fora do prazo · justificado"


def test_nota_cancelada_no_iss_nao_aceita_boleto(monkeypatch):
    monkeypatch.setattr(rota, 'status_da_nota', lambda *a: 'CANCELADA')
    pdf, _ = _pdf()
    postgres = FakeSession([[NOTA], REGRAS, [2], []])

    with pytest.raises(HTTPException) as erro:
        _enviar(postgres, FakeSession([EMPRESAS]), pdf)

    assert erro.value.status_code == 409
    assert 'CANCELADA' in erro.value.detail
    assert postgres.commits == 0


def test_status_da_nota_combina_xml_e_portal():
    assert rota.status_da_nota(FakeSession([[True], ['CANCELADA'], [None]]), '1', '2') == 'CANCELADA'
    assert rota.status_da_nota(FakeSession([[True], ['NORMAL'], [None]]), '1', '2') == 'NORMAL'
    assert rota.status_da_nota(FakeSession([[False], [None]]), '1', '2') is None


# ------------------------------------------------------- prazo de 3 dias úteis


def test_boleto_dentro_do_prazo_nao_pede_justificativa():
    # Terça 17/11 10h + 72h úteis: qua, qui, (sex 20/11 é feriado), segunda 23/11 10h.
    postgres = FakeSession([[NOTA], REGRAS, [2], [], [], [5]])
    resposta = _enviar(postgres, FakeSession([EMPRESAS]), _pdf(dias=6)[0])  # vence 23/11
    assert resposta['fora_do_prazo'] is False
    assert resposta['justificativa_atraso'] is None
    gravado = postgres.executions[-1][1]
    assert gravado['prazo_minimo'] == date(2026, 11, 23)
    assert gravado['fora_do_prazo'] is False


def test_boleto_fora_do_prazo_sem_justificativa_e_recusado():
    postgres = FakeSession([[NOTA], REGRAS, [2], [], [], [5]])
    with pytest.raises(HTTPException) as erro:
        _enviar(postgres, FakeSession([EMPRESAS]), _pdf(dias=3)[0])  # vence sexta 20/11 (feriado) < segunda 23/11
    assert erro.value.status_code == 422
    assert '3 dias úteis' in erro.value.detail
    assert '23/11/2026 10:00' in erro.value.detail
    assert not any('INSERT INTO api_prontocardio.contas_pagar_boletos' in sql for sql, _ in postgres.executions)


def test_justificativa_curta_demais_e_recusada():
    with pytest.raises(HTTPException) as erro:
        _enviar(FakeSession([[NOTA], REGRAS, [2], [], [], [5]]), FakeSession([EMPRESAS]), _pdf(dias=3)[0],
                justificativa_atraso='  atraso ')
    assert erro.value.status_code == 422


def test_boleto_fora_do_prazo_com_justificativa_segue_para_o_financeiro():
    postgres = FakeSession([[NOTA], REGRAS, [2], [], [], [5]])
    resposta = _enviar(postgres, FakeSession([EMPRESAS]), _pdf(dias=3)[0],
                       justificativa_atraso='  O fornecedor   só enviou o boleto hoje. ')
    assert resposta['fora_do_prazo'] is True
    gravado = postgres.executions[-1][1]
    assert gravado['fora_do_prazo'] is True
    assert gravado['justificativa_atraso'] == 'O fornecedor só enviou o boleto hoje.'
    assert resposta['divergencias'] == []


def test_listagem_informa_o_prazo_minimo_de_quem_envia_agora():
    postgres = FakeSession([[NOTA], REGRAS, [2], [], []])
    resposta = rota.listar_boletos('11.222.333/0001-81', '123', _usuario(), postgres, FakeSession([EMPRESAS]))
    assert resposta['prazo_minimo_hoje'] == datetime(2026, 11, 23, 10, 0)



def _setor_com_colega(lancado_por):
    """Usuário RH000938 (oficina 9 na regra do MV); a regra do fornecedor aponta para 2."""
    postgres = FakeSession([[NOTA], REGRAS, [], ['RH000938']])
    lancamento = {'cnpj': '11222333000181', 'cd_usuario': lancado_por, 'dt_entrada': datetime(2026, 1, 5)}
    oracle = FakeSession([
        EMPRESAS,
        [{'cd_oficina': 9}],                                        # regra do usuário
        [{'cd_usuario': 'RH000938', 'cd_oficina': 9},
         {'cd_usuario': 'RH000777', 'cd_oficina': 9},
         {'cd_usuario': 'RH000111', 'cd_oficina': 5}],              # regra de todos (2 sem ninguém)
        [lancamento],                                               # ENT_SERV do fornecedor
    ])
    return postgres, oracle


def test_fornecedor_de_oficina_sem_usuarios_lancado_por_colega_e_do_usuario():
    postgres, oracle = _setor_com_colega('RH000777')
    nota = rota._nota_ou_404(postgres, oracle, _usuario(), '11222333000181', '123')

    assert nota['cd_oficina'] == 9  # oficina da regra do usuário, não a da regra do fornecedor


def test_fornecedor_de_outra_oficina_e_recusado():
    postgres, oracle = _setor_com_colega('RH000111')
    with pytest.raises(HTTPException) as erro:
        rota._nota_ou_404(postgres, oracle, _usuario(), '11222333000181', '123')
    assert erro.value.status_code == 403


# ------------------------------------------------ desfazer envio ao financeiro

def _desfazer(postgres, oracle, usuario=None):
    return rota.desfazer_envio_ao_financeiro('11222333000181', '123', usuario or _usuario(), postgres, oracle)


def _sessoes_desfazer(boletos, importacao=None, lancada=None):
    """Setor com a oficina 2 concedida; nota da oficina 2."""
    postgres = FakeSession([
        [NOTA], REGRAS, [2], [],                       # nota, regras, oficinas concedidas, login MV
        boletos,                                       # boletos da nota
        [True], [importacao] if importacao else [],    # tabela de importações e registro
        [], [],                                        # DELETE + commit
    ])
    lanc = [lancada] if lancada else []
    oracle = FakeSession([EMPRESAS, [], lanc])        # empresas, ENT_SERV do fornecedor, lançadas no MV
    return postgres, oracle


def test_desfazer_envio_exclui_os_boletos_e_volta_para_a_lancar():
    postgres, oracle = _sessoes_desfazer([{'id': 1, 'enviado_por': 7}, {'id': 2, 'enviado_por': 7}])
    resposta = _desfazer(postgres, oracle)

    assert resposta == {'desfeito': True, 'boletos_removidos': 2}
    assert any('DELETE FROM api_prontocardio.contas_pagar_boletos' in sql for sql, _ in postgres.executions)
    assert postgres.commits == 1


def test_desfazer_envio_bloqueado_se_o_financeiro_ja_importou():
    postgres, oracle = _sessoes_desfazer([{'id': 1, 'enviado_por': 7}], importacao={'status': 'importada'})
    with pytest.raises(HTTPException) as erro:
        _desfazer(postgres, oracle)
    assert erro.value.status_code == 409
    assert 'importou' in erro.value.detail
    assert postgres.commits == 0


def test_desfazer_envio_bloqueado_se_ja_lancada_no_mv():
    lancada = {'cnpj': '11222333000181', 'nr_documento': '0123', 'cd_ent_serv': 55, 'cd_con_pag': 66,
               'cd_usuario': 'RH000111', 'origem': 'ENT_SERV'}
    postgres, oracle = _sessoes_desfazer([{'id': 1, 'enviado_por': 7}], lancada=lancada)
    with pytest.raises(HTTPException) as erro:
        _desfazer(postgres, oracle)
    assert erro.value.status_code == 409
    assert 'entrada 55' in erro.value.detail


def test_desfazer_envio_so_por_quem_enviou():
    postgres, oracle = _sessoes_desfazer([{'id': 1, 'enviado_por': 99}])
    with pytest.raises(HTTPException) as erro:
        _desfazer(postgres, oracle)
    assert erro.value.status_code == 403


def test_desfazer_envio_sem_boleto():
    postgres, oracle = _sessoes_desfazer([])
    with pytest.raises(HTTPException) as erro:
        _desfazer(postgres, oracle)
    assert erro.value.status_code == 404


def test_nota_enviada_ao_financeiro_sai_de_a_lancar():
    nota_fila = {**NOTA, 'codigo_verificacao_nfse': 'A', 'data_hora': None, 'competencia_nfse': None,
                 'tomador_razao_social': 'Hospital', 'discriminacao': '',
                 'situacao_xml': None, 'situacao_portal': None, 'situacao_consultada_em': None}
    sem_boleto = {**nota_fila, 'numero_nfse': '124'}
    divergente = {**nota_fila, 'numero_nfse': '125'}
    regras = [{**REGRAS[0], 'nm_fornecedor': 'Software SA', 'origem': 'historico', 'qtd_entradas': 3,
               'participacao': Decimal('1'), 'usuario_id': 1, 'atualizado_em': None, 'cd_fornecedor': 1}]
    boletos = [
        {'prestador_cnpj': '11222333000181', 'numero_nfse': '123', 'quantidade': 1, 'soma': Decimal('1500.50'),
         'primeiro_vencimento': HOJE, 'com_divergencia': False, 'fora_do_prazo': False},
        {'prestador_cnpj': '11222333000181', 'numero_nfse': '125', 'quantidade': 1, 'soma': Decimal('9'),
         'primeiro_vencimento': HOJE, 'com_divergencia': True, 'fora_do_prazo': False},
    ]
    postgres = FakeSession([[True], [False], [nota_fila, sem_boleto, divergente], regras, [], boletos])
    oracle = FakeSession([[], EMPRESAS])

    resposta = rota_nfse.listar_recebidas(_usuario(perfil='ti'), postgres, oracle, situacao='pendente')

    # 123 (pronta) só em "Prontas para o financeiro"; sem boleto e divergente seguem em "A lançar".
    assert [i['numero_nfse'] for i in resposta['itens']] == ['124', '125']



def test_envio_ao_financeiro_exige_observacao():
    for observacao in (None, '', '   curta demais   '):
        with pytest.raises(HTTPException) as erro:
            _enviar(FakeSession([]), FakeSession([]), _pdf()[0], observacao=observacao)
        assert erro.value.status_code == 422
        assert 'do que se trata a NFS-e' in erro.value.detail


def test_observacao_e_gravada_com_o_boleto():
    pdf, _ = _pdf()
    postgres = FakeSession([[NOTA], REGRAS, [2], [], [], [99]])
    resposta = _enviar(postgres, FakeSession([EMPRESAS]), pdf,
                       observacao='  Transporte   de pacientes de novembro, contrato 12/2026  ')

    gravado = next(p for sql, p in postgres.executions if 'INSERT INTO api_prontocardio.contas_pagar_boletos' in sql)
    assert gravado['observacao'] == 'Transporte de pacientes de novembro, contrato 12/2026'
    assert resposta['observacao'] == gravado['observacao']
