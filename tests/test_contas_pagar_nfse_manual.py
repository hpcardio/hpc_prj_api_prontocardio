# ruff: noqa: PLR2004, E501, PT018
import asyncio
import json
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException, UploadFile

from app_prontocardio.routers import contas_pagar_nfse_manual as rota
from app_prontocardio.services import danfse

TEXTO = (Path(__file__).parent / 'fixtures' / 'danfse_com_retencao.txt').read_text()
EMPRESAS = {'05613278000158': {'cd_multi_empresa': 2, 'ds_multi_empresa': 'PRONTO ATENDIMENTO'},
            '08711085000128': {'cd_multi_empresa': 1, 'ds_multi_empresa': 'HOSPITAL'}}
DADOS_TELA = {'numero_nfse': '6631', 'chave_acesso': '23044001207595068000100000000000663126110386618210',
              'data_emissao': '2026-11-06T12:07', 'competencia_nfse': '2026-11-01', 'prestador_cnpj': '07.595.068/0001-00',
              'prestador_razao_social': 'MULTIMEDICOS SERVICOS MEDICOS LTDA', 'prestador_municipio': 'FORTALEZA/CE',
              'tomador_cnpj': '08711085000128', 'discriminacao': 'Serviços médicos', 'valor_servicos': '18.551,40',
              'iss_retido': 'sim', 'valor_iss': '556,54', 'valor_ir': '278,27', 'valor_csll': '862,64', 'valor_inss': '',
              'valor_liquido_nfse': '16.853,95', 'aliquota': '3,00'}


class Resultado:
    def __init__(self, valor):
        self.valor = valor

    def scalar_one(self):
        return self.valor

    def scalar_one_or_none(self):
        return self.valor

    def mappings(self):
        return SimpleNamespace(first=lambda: self.valor)


class Sessao:
    def __init__(self, rascunho=None, existe=None, boletos=0):
        self.rascunho, self.existe, self.boletos = rascunho, existe, boletos
        self.executados, self.commits = [], 0

    def execute(self, sql, params=None):
        texto = str(sql)
        self.executados.append((texto, params))
        if 'INSERT INTO api_prontocardio.contas_pagar_nfse_manuais' in texto:
            return Resultado(41)
        if 'SELECT id, status, origem_leitura' in texto:
            return Resultado(self.rascunho)
        if 'SELECT origem_nfse' in texto:
            return Resultado(self.existe)
        if 'contas_pagar_boletos' in texto:
            return Resultado(self.boletos)
        return Resultado(None)

    def commit(self):
        self.commits += 1


def _usuario(uid=7, perfil='usuario'):
    return SimpleNamespace(id=uid, nome='Setor', perfil=perfil, telas_permitidas=['contas_pagar_nfse_setor'])


@pytest.fixture(autouse=True)
def mv(monkeypatch):
    monkeypatch.setattr(rota, '_empresas_mv', lambda oracle: EMPRESAS)
    monkeypatch.setattr(rota, '_lancadas_mv', lambda oracle, cnpjs: {})
    monkeypatch.setattr(rota, '_gestor', lambda u: u.perfil == 'admin')


def _rascunho(**troca):
    return {'id': 41, 'status': 'rascunho', 'origem_leitura': 'danfse', 'leitura': {'dados': {}, 'avisos': []}, 'nome_arquivo': 'nf.pdf',
            'criado_por': 7, 'criado_em': None, 'confirmado_em': None, 'prestador_cnpj': '07595068000100', 'numero_nfse': '6631', **troca}


def test_leitura_guarda_rascunho_com_os_dados_do_danfse(monkeypatch):
    monkeypatch.setattr(rota, 'ler_danfse', lambda conteudo: danfse.ler_danfse_texto(TEXTO))
    sessao = Sessao()
    resposta = asyncio.run(rota.ler_pdf_nfse(_usuario(), sessao, None, UploadFile(BytesIO(b'%PDF-1.4'), filename='nf.pdf')))
    assert resposta['id'] == 41
    assert resposta['reconhecido'] is True
    assert resposta['dados']['valor_liquido_nfse'] == '16853.95'
    gravado = next(p for s, p in sessao.executados if 'INSERT INTO' in s)
    assert gravado['origem'] == 'danfse' and gravado['usuario_id'] == 7
    assert json.loads(gravado['leitura'])['dados']['numero_nfse'] == '6631'
    assert sessao.commits == 1


def test_leitura_avisa_nota_que_ja_esta_na_fila(monkeypatch):
    monkeypatch.setattr(rota, 'ler_danfse', lambda conteudo: danfse.ler_danfse_texto(TEXTO))
    resposta = asyncio.run(rota.ler_pdf_nfse(_usuario(), Sessao(existe='iss'), None, UploadFile(BytesIO(b'%PDF'), filename='a.pdf')))
    assert any('já está na fila' in a for a in resposta['avisos'])


def test_pdf_ilegivel(monkeypatch):
    def falha(conteudo):
        raise danfse.DanfseIlegivel('Envie a NFS-e em PDF.')
    monkeypatch.setattr(rota, 'ler_danfse', falha)
    with pytest.raises(HTTPException) as erro:
        asyncio.run(rota.ler_pdf_nfse(_usuario(), Sessao(), None, UploadFile(BytesIO(b'x'), filename='a.txt')))
    assert erro.value.status_code == 422


def test_confirmar_normaliza_e_ativa_a_nota():
    sessao = Sessao(rascunho=_rascunho())
    resposta = rota.confirmar_cadastro(41, rota.ConfirmacaoInput(dados=DADOS_TELA), _usuario(), sessao, None)
    assert resposta['chave'] == '07595068000100-6631'
    sql, params = next((s, p) for s, p in sessao.executados if 'UPDATE api_prontocardio.contas_pagar_nfse_manuais' in s)
    assert "status = 'ativa'" in sql
    assert params['valor_servicos'] == '18551.40'
    assert params['valor_iss_retido'] == '556.54'  # ISS marcado como retido assume o valor do ISS
    assert params['iss_retido'] == '1'
    assert params['prestador_cnpj'] == '07595068000100'
    assert params['tomador_razao_social'] == 'HOSPITAL'
    assert params['data_hora'].isoformat().startswith('2026-11-06T12:07')
    assert sessao.commits == 1


def test_confirmar_com_erros_nao_grava():
    sessao = Sessao(rascunho=_rascunho())
    with pytest.raises(HTTPException) as erro:
        rota.confirmar_cadastro(41, rota.ConfirmacaoInput(dados={**DADOS_TELA, 'valor_liquido_nfse': '17.000,00', 'tomador_cnpj': '11222333000181'}),
                                _usuario(), sessao, None)
    assert erro.value.status_code == 422
    erros = erro.value.detail['erros']
    assert any('não fecha' in e for e in erros) and any('tomador' in e for e in erros)
    assert sessao.commits == 0


def test_confirmar_nota_que_ja_existe():
    with pytest.raises(HTTPException) as erro:
        rota.confirmar_cadastro(41, rota.ConfirmacaoInput(dados=DADOS_TELA), _usuario(), Sessao(rascunho=_rascunho(), existe='iss'), None)
    assert any('já está na fila' in e for e in erro.value.detail['erros'])


def test_confirmar_nota_ja_lancada_no_mv(monkeypatch):
    monkeypatch.setattr(rota, '_lancadas_mv', lambda oracle, cnpjs: {('07595068000100', '6631'): {'cd_con_pag': 1}})
    with pytest.raises(HTTPException) as erro:
        rota.confirmar_cadastro(41, rota.ConfirmacaoInput(dados=DADOS_TELA), _usuario(), Sessao(rascunho=_rascunho()), None)
    assert any('já foi lançada no MV' in e for e in erro.value.detail['erros'])


def test_rascunho_de_outro_usuario():
    with pytest.raises(HTTPException) as erro:
        rota.ver_rascunho(41, _usuario(uid=99), Sessao(rascunho=_rascunho()))
    assert erro.value.status_code == 403
    assert rota.ver_rascunho(41, _usuario(uid=99, perfil='admin'), Sessao(rascunho=_rascunho()))['id'] == 41


def test_excluir_cadastro_ativo_sem_boleto():
    sessao = Sessao(rascunho=_rascunho(status='ativa'))
    assert rota.excluir_cadastro(41, rota.ExclusaoInput(motivo='PDF errado'), _usuario(), sessao, None) == {'excluido': True}
    assert any("SET status = 'excluida'" in s for s, _ in sessao.executados)


def test_nao_exclui_com_boleto():
    with pytest.raises(HTTPException) as erro:
        rota.excluir_cadastro(41, rota.ExclusaoInput(motivo='PDF errado'), _usuario(), Sessao(rascunho=_rascunho(status='ativa'), boletos=1), None)
    assert 'boletos' in erro.value.detail


def test_descartar_so_rascunho():
    with pytest.raises(HTTPException):
        rota.descartar_rascunho(41, _usuario(), Sessao(rascunho=_rascunho(status='ativa')))
    sessao = Sessao(rascunho=_rascunho())
    rota.descartar_rascunho(41, _usuario(), sessao)
    assert sessao.commits == 1
