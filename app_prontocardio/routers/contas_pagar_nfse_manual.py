# ruff: noqa: E501, PLR0913
"""Contas a Pagar: cadastro de NFS-e de outros municípios pelo PDF (DANFSe).

1. ``POST /recebidas/manual/leitura``: envia o PDF; a API lê o DANFSe e guarda
   um rascunho com os dados extraídos (nada vai para a fila ainda).
2. ``GET /recebidas/manual/{id}``: o rascunho, para a tela de conferência.
3. ``POST /recebidas/manual/{id}/confirmar``: dados conferidos/editados; a nota
   entra na fila da NFS-e do setor (setor pela regra do fornecedor) e segue
   o mesmo caminho das notas do ISS Fortaleza: boleto, simulação, importação.
4. ``DELETE /recebidas/manual/{id}``: descarta o rascunho.
5. ``POST /recebidas/manual/{id}/excluir``: desfaz o cadastro de uma nota
   ativa que ainda não foi para o MV e não tem boleto.
"""

import json
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from http import HTTPStatus
from typing import Annotated

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, Field
from sqlalchemy import bindparam, text

from app_prontocardio.routers.contas_pagar_nfse import (
    FUSO_HOSPITAL,
    AcessoFila,
    SessionOracle,
    SessionPostgres,
    _chave_documento,
    _empresas_mv,
    _gestor,
    _lancadas_mv,
)
from app_prontocardio.services.danfse import DanfseIlegivel, ler_danfse
from app_prontocardio.services.nfse_manuais import (
    CAMPOS_EDITAVEIS,
    CAMPOS_NFSE,
    DDL_NFSE_MANUAIS,
    FONTE_NFSE_RECEBIDAS,
    VALORES,
    validar,
)
from app_prontocardio.services.payables_boleto import cnpj_valido
from app_prontocardio.services.payables_oficinas import somente_digitos

router = APIRouter(prefix='/app_glosas/financeiro/contas-a-pagar/nfse', tags=['contas-a-pagar-nfse-manual'])

TAMANHO_MAXIMO_PDF = 10 * 1024 * 1024
COLUNAS_GRAVADAS = tuple(c for c in CAMPOS_NFSE if c in CAMPOS_EDITAVEIS) + (
    'valor_pis', 'valor_cofins', 'valor_deducoes', 'base_calculo')

SQL_INSERIR_RASCUNHO = text("""
INSERT INTO api_prontocardio.contas_pagar_nfse_manuais
    (status, origem_leitura, leitura, nome_arquivo, arquivo, sha256, criado_por)
VALUES ('rascunho', :origem, CAST(:leitura AS jsonb), :nome, :arquivo, :sha256, :usuario_id)
RETURNING id
""").bindparams(bindparam('arquivo'))

SQL_RASCUNHO = text("""
SELECT id, status, origem_leitura, leitura, nome_arquivo, criado_por, criado_em, confirmado_em,
       prestador_cnpj, numero_nfse
  FROM api_prontocardio.contas_pagar_nfse_manuais WHERE id = :id
""")

SQL_JA_EXISTE = text(f"""
SELECT origem_nfse FROM {FONTE_NFSE_RECEBIDAS} n
 WHERE n.prestador_cnpj = :cnpj AND n.numero_nfse = :numero
 LIMIT 1
""")

SQL_TEM_BOLETO = text("""
SELECT count(*) FROM api_prontocardio.contas_pagar_boletos WHERE prestador_cnpj = :cnpj AND numero_nfse = :numero
""")

SQL_DESCARTAR = text("""
DELETE FROM api_prontocardio.contas_pagar_nfse_manuais WHERE id = :id AND status = 'rascunho'
""")

SQL_EXCLUIR = text("""
UPDATE api_prontocardio.contas_pagar_nfse_manuais
   SET status = 'excluida', excluido_por = :usuario_id, excluido_em = now(), motivo_exclusao = :motivo
 WHERE id = :id AND status = 'ativa'
""")


class ConfirmacaoInput(BaseModel):
    dados: dict[str, str | None] = Field(description='Campos conferidos na tela (CAMPOS_EDITAVEIS).')


class ExclusaoInput(BaseModel):
    motivo: str = Field(min_length=5, max_length=300)


def _decimal(valor) -> Decimal | None:
    """Aceita 1.234,56 · 1234,56 · 1234.56."""
    texto = str(valor or '').strip().replace('R$', '').replace(' ', '')
    if not texto or texto == '-':
        return None
    if ',' in texto:
        texto = texto.replace('.', '').replace(',', '.')
    try:
        return Decimal(texto).quantize(Decimal('0.01'))
    except InvalidOperation as exc:
        raise HTTPException(HTTPStatus.UNPROCESSABLE_ENTITY, f'Valor inválido: {valor}') from exc


def _data(valor) -> datetime | None:
    texto = str(valor or '').strip()
    if not texto:
        return None
    for formato in ('%Y-%m-%dT%H:%M:%S', '%Y-%m-%dT%H:%M', '%Y-%m-%d', '%d/%m/%Y %H:%M:%S', '%d/%m/%Y %H:%M', '%d/%m/%Y'):
        try:
            return datetime.strptime(texto[:19], formato)
        except ValueError:
            continue
    raise HTTPException(HTTPStatus.UNPROCESSABLE_ENTITY, f'Data inválida: {valor}')


def _json(valor):
    """Dinheiro como texto (Decimal -> '16853.95'): float perderia centavos."""
    return jsonable_encoder(valor, custom_encoder={Decimal: str})


def _garantir_tabela(session) -> None:
    for ddl in DDL_NFSE_MANUAIS:
        session.execute(text(ddl))


def _rascunho(session, usuario, manual_id: int) -> dict:
    linha = session.execute(SQL_RASCUNHO, {'id': manual_id}).mappings().first()
    if linha is None:
        raise HTTPException(HTTPStatus.NOT_FOUND, 'Cadastro de NFS-e não encontrado.')
    if linha['criado_por'] != usuario.id and not _gestor(usuario):
        raise HTTPException(HTTPStatus.FORBIDDEN, 'Este cadastro é de outro usuário.')
    registro = dict(linha)
    registro['leitura'] = registro['leitura'] if isinstance(registro['leitura'], dict) else json.loads(registro['leitura'] or '{}')
    return registro


def _normalizar(dados: dict) -> dict:
    """Dados da tela -> tipos da tabela."""
    limpos = {k: (str(v).strip() if v is not None else None) or None for k, v in dados.items() if k in CAMPOS_EDITAVEIS}
    for campo in ('prestador_cnpj', 'tomador_cnpj'):
        limpos[campo] = somente_digitos(limpos.get(campo)) or None
    limpos['numero_nfse'] = re.sub(r'[^0-9A-Za-z]', '', limpos.get('numero_nfse') or '') or None
    limpos['chave_acesso'] = somente_digitos(limpos.get('chave_acesso')) or None
    for campo in VALORES:
        limpos[campo] = _decimal(limpos.get(campo)) or Decimal('0')
    limpos['iss_retido'] = '1' if str(limpos.get('iss_retido') or '').lower() in ('1', 'sim', 's', 'true', 'on') else '2'
    if limpos['iss_retido'] == '1' and not limpos['valor_iss_retido']:
        limpos['valor_iss_retido'] = limpos['valor_iss']
    if limpos['iss_retido'] != '1':
        limpos['valor_iss_retido'] = Decimal('0')
    emissao = _data(limpos.get('data_emissao'))
    limpos['data_emissao'] = emissao.isoformat() if emissao else None
    competencia = _data(limpos.get('competencia_nfse'))
    limpos['competencia_nfse'] = competencia.date().isoformat() if competencia else None
    if limpos.get('aliquota'):
        limpos['aliquota'] = limpos['aliquota'].replace(',', '.')
    return limpos


@router.post('/recebidas/manual/leitura', status_code=HTTPStatus.CREATED)
async def ler_pdf_nfse(usuario: AcessoFila, session: SessionPostgres, oracle: SessionOracle,
                       arquivo: Annotated[UploadFile, File()]):
    """Lê o PDF da NFS-e (DANFSe) e guarda um rascunho para conferência. Nada entra na fila ainda."""
    conteudo = await arquivo.read(TAMANHO_MAXIMO_PDF + 1)
    if len(conteudo) > TAMANHO_MAXIMO_PDF:
        raise HTTPException(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, 'O PDF deve ter até 10 MB.')
    try:
        leitura = ler_danfse(conteudo)
    except DanfseIlegivel as exc:
        raise HTTPException(HTTPStatus.UNPROCESSABLE_ENTITY, str(exc)) from exc
    dados = leitura.dados
    empresas = _empresas_mv(oracle)
    if dados.get('tomador_cnpj') and dados['tomador_cnpj'] not in empresas:
        leitura.avisos.append('O tomador desta NFS-e não é uma empresa do grupo no MV.')
    if dados.get('prestador_cnpj') and dados.get('numero_nfse'):
        existente = session.execute(SQL_JA_EXISTE, {'cnpj': dados['prestador_cnpj'], 'numero': dados['numero_nfse']}).scalar_one_or_none()
        if existente:
            leitura.avisos.append('Esta NFS-e já está na fila' + (' (veio do ISS Fortaleza).' if existente == 'iss' else ' (cadastrada por PDF).'))
    _garantir_tabela(session)
    novo_id = session.execute(SQL_INSERIR_RASCUNHO, {
        'origem': 'danfse' if leitura.reconhecido else 'manual',
        'leitura': json.dumps(_json(leitura.as_dict())),
        'nome': (arquivo.filename or 'nfse.pdf')[:255], 'arquivo': conteudo, 'sha256': sha256(conteudo).hexdigest(),
        'usuario_id': usuario.id,
    }).scalar_one()
    session.commit()
    return {'id': novo_id, **_json(leitura.as_dict())}


@router.get('/recebidas/manual/{manual_id}')
def ver_rascunho(manual_id: int, usuario: AcessoFila, session: SessionPostgres):
    registro = _rascunho(session, usuario, manual_id)
    return {'id': registro['id'], 'status': registro['status'], 'nome_arquivo': registro['nome_arquivo'],
            'criado_em': registro['criado_em'], **registro['leitura']}


@router.post('/recebidas/manual/{manual_id}/confirmar')
def confirmar_cadastro(manual_id: int, entrada: ConfirmacaoInput, usuario: AcessoFila,
                       session: SessionPostgres, oracle: SessionOracle):
    """Valida os dados conferidos e coloca a NFS-e na fila (setor pela regra do fornecedor)."""
    registro = _rascunho(session, usuario, manual_id)
    if registro['status'] != 'rascunho':
        raise HTTPException(HTTPStatus.CONFLICT, 'Este cadastro já foi confirmado ou descartado.')
    dados = _normalizar(entrada.dados)
    empresas = _empresas_mv(oracle)
    erros = validar(dados, cnpjs_do_grupo=set(empresas), cnpj_valido=cnpj_valido)
    if dados.get('prestador_cnpj') and dados.get('numero_nfse'):
        existente = session.execute(SQL_JA_EXISTE, {'cnpj': dados['prestador_cnpj'], 'numero': dados['numero_nfse']}).scalar_one_or_none()
        if existente:
            erros.append('Esta NFS-e já está na fila da NFS-e do setor.')
        elif _lancadas_mv(oracle, [dados['prestador_cnpj']]).get((dados['prestador_cnpj'], _chave_documento(dados['numero_nfse']))):
            erros.append('Esta NFS-e já foi lançada no MV.')
    if erros:
        raise HTTPException(HTTPStatus.UNPROCESSABLE_ENTITY, {'erros': erros})
    emissao = datetime.fromisoformat(dados['data_emissao']).replace(tzinfo=FUSO_HOSPITAL)
    empresa = empresas[dados['tomador_cnpj']]
    colunas = {c: (str(dados[c]) if dados.get(c) is not None else None) for c in COLUNAS_GRAVADAS if c in dados}
    colunas.update({'valor_pis': '0', 'valor_cofins': '0', 'valor_deducoes': '0',
                    'base_calculo': str(dados['valor_servicos']), 'tomador_razao_social': dados.get('tomador_razao_social') or empresa.get('ds_multi_empresa')})
    sets = ', '.join(f'{c} = :{c}' for c in colunas)
    session.execute(text(f"""
        UPDATE api_prontocardio.contas_pagar_nfse_manuais
           SET {sets}, chave_acesso = :chave_acesso, prestador_municipio = :prestador_municipio,
               prestador_codigo_municipio = :prestador_codigo_municipio, data_hora = :data_hora,
               status = 'ativa', confirmado_por = :usuario_id, confirmado_em = now()
         WHERE id = :id AND status = 'rascunho'"""), {
        **colunas, 'chave_acesso': dados.get('chave_acesso'), 'prestador_municipio': dados.get('prestador_municipio'),
        'prestador_codigo_municipio': dados.get('prestador_codigo_municipio'), 'data_hora': emissao,
        'usuario_id': usuario.id, 'id': manual_id,
    })
    session.commit()
    return {'confirmado': True, 'chave': f'{dados["prestador_cnpj"]}-{dados["numero_nfse"]}',
            'prestador_cnpj': dados['prestador_cnpj'], 'numero_nfse': dados['numero_nfse'],
            'data_emissao': emissao.date()}


@router.delete('/recebidas/manual/{manual_id}', status_code=HTTPStatus.NO_CONTENT)
def descartar_rascunho(manual_id: int, usuario: AcessoFila, session: SessionPostgres):
    registro = _rascunho(session, usuario, manual_id)
    if registro['status'] != 'rascunho':
        raise HTTPException(HTTPStatus.CONFLICT, 'Só rascunhos podem ser descartados; use excluir cadastro.')
    session.execute(SQL_DESCARTAR, {'id': manual_id})
    session.commit()


@router.post('/recebidas/manual/{manual_id}/excluir')
def excluir_cadastro(manual_id: int, entrada: ExclusaoInput, usuario: AcessoFila,
                     session: SessionPostgres, oracle: SessionOracle):
    """Tira da fila uma NFS-e cadastrada por PDF, se ainda não foi para o MV e não tem boleto."""
    registro = _rascunho(session, usuario, manual_id)
    if registro['status'] != 'ativa':
        raise HTTPException(HTTPStatus.CONFLICT, 'Este cadastro não está ativo.')
    chave = {'cnpj': registro['prestador_cnpj'], 'numero': registro['numero_nfse']}
    if session.execute(SQL_TEM_BOLETO, chave).scalar_one():
        raise HTTPException(HTTPStatus.CONFLICT, 'Exclua os boletos desta NFS-e antes de excluir o cadastro.')
    if _lancadas_mv(oracle, [registro['prestador_cnpj']]).get((registro['prestador_cnpj'], _chave_documento(registro['numero_nfse']))):
        raise HTTPException(HTTPStatus.CONFLICT, 'Esta NFS-e já foi lançada no MV; desfaça a importação antes.')
    session.execute(SQL_EXCLUIR, {'id': manual_id, 'usuario_id': usuario.id, 'motivo': ' '.join(entrada.motivo.split())})
    session.commit()
    return {'excluido': True}

