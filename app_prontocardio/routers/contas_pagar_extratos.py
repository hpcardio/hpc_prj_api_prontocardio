# ruff: noqa: E501
"""Contas a Pagar, fase 5: extratos bancários e baixa de títulos.

5a: o financeiro envia o extrato (PDF; XLSX no Santander). O arquivo é lido e
conferido com os saldos; extrato que não fecha é recusado inteiro. Cada
extrato fica ligado a uma conta corrente do MV (``CON_COR``), sugerida pelo
banco + número da conta e pela conta com mais baixas recentes; o financeiro
pode trocar. Linhas que já vieram em outro extrato (os PDFs diários se
sobrepõem) ficam marcadas como repetidas.

5b: para cada débito do extrato, sugere os títulos em aberto do MV com o
mesmo valor e vencimento próximo, e simula a baixa: mostra o que seria
gravado em ``PAGCON_PAG`` e ``MOV_CONCOR`` (tela O_PAG), sem gravar nada.
"""

import hashlib
import re
from datetime import date, timedelta
from decimal import Decimal
from http import HTTPStatus
from typing import Annotated

from fastapi import APIRouter, File, HTTPException, Query, Response, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import bindparam, text

from app_prontocardio.routers.contas_pagar_nfse import (
    AcessoRegras,
    SessionOracle,
    SessionPostgres,
)
from app_prontocardio.services.extratos_bancarios import (
    NOMES_BANCO,
    Extrato,
    ExtratoInvalido,
    ler_extrato,
)
from app_prontocardio.services.payables_mv import read_payables_rows

router = APIRouter(
    prefix='/app_glosas/financeiro/contas-a-pagar/extratos',
    tags=['contas-a-pagar-extratos'],
)

TAMANHO_MAXIMO = 8 * 1024 * 1024
# Janela da sugestão de baixa (data do débito - vencimento), medida nas baixas
# de out/2026 da conta 08 BB: 95% dos casamentos ficaram entre -5 e +10 dias.
DIAS_ANTES_VENCIMENTO = 5
DIAS_DEPOIS_VENCIMENTO = 10
DIAS_ATIVIDADE_CONTA = 90
MAX_DIAS_CONSULTA = 62
TAMANHO_CHAVE_HISTORICO = 25


# ---------------------------------------------------------------- MV

SQL_CONTAS_MV = text("""
SELECT c.cd_con_cor AS cd_con_cor, c.ds_con_cor AS ds_con_cor, c.cd_banco AS cd_banco,
       c.cd_agencia AS cd_agencia, c.nr_conta AS nr_conta, c.cd_multi_empresa AS cd_multi_empresa,
       (SELECT COUNT(*) FROM dbamv.pagcon_pag p
         WHERE p.cd_con_cor = c.cd_con_cor
           AND p.dt_pagamento >= TRUNC(SYSDATE) - :dias) AS baixas_recentes
  FROM dbamv.con_cor c
 WHERE NVL(c.sn_ativo, 'S') = 'S'
 ORDER BY c.cd_multi_empresa, c.cd_con_cor
""")

SQL_TITULOS_ABERTOS = text("""
SELECT i.cd_itcon_pag AS cd_itcon_pag, i.cd_con_pag AS cd_con_pag, i.nr_parcela AS nr_parcela,
       i.dt_vencimento AS dt_vencimento, i.dt_prevista_pag AS dt_prevista_pag,
       i.vl_duplicata AS vl_duplicata, NVL(i.vl_soma_pago, 0) AS vl_soma_pago,
       c.cd_fornecedor AS cd_fornecedor, f.nm_fornecedor AS nm_fornecedor,
       c.nr_documento AS nr_documento, c.ds_con_pag AS ds_con_pag,
       c.cd_multi_empresa AS cd_multi_empresa
  FROM dbamv.itcon_pag i
  JOIN dbamv.con_pag c ON c.cd_con_pag = i.cd_con_pag
  LEFT JOIN dbamv.fornecedor f ON f.cd_fornecedor = c.cd_fornecedor
 WHERE c.cd_multi_empresa = :empresa
   AND NVL(i.tp_quitacao, 'X') <> 'Q'
   AND i.cd_itcon_pag_estorno IS NULL
   AND i.vl_duplicata - NVL(i.vl_soma_pago, 0) > 0
   AND i.dt_vencimento BETWEEN :inicio AND :fim
""")

SQL_TITULO = text("""
SELECT i.cd_itcon_pag AS cd_itcon_pag, i.cd_con_pag AS cd_con_pag, i.nr_parcela AS nr_parcela,
       i.dt_vencimento AS dt_vencimento, i.vl_duplicata AS vl_duplicata,
       NVL(i.vl_soma_pago, 0) AS vl_soma_pago, i.tp_quitacao AS tp_quitacao,
       i.cd_status AS cd_status, i.cd_itcon_pag_estorno AS cd_itcon_pag_estorno,
       c.cd_fornecedor AS cd_fornecedor, f.nm_fornecedor AS nm_fornecedor,
       c.nr_documento AS nr_documento, c.ds_con_pag AS ds_con_pag,
       c.cd_multi_empresa AS cd_multi_empresa,
       (SELECT MAX(v.cd_lote_pgto) FROM dbamv.v_pag v WHERE v.cd_itcon_pag = i.cd_itcon_pag) AS cd_lote_pgto,
       (SELECT COUNT(*) FROM dbamv.nota_credito_debito_fnfi n
         WHERE n.cd_fornecedor_cliente = c.cd_fornecedor
           AND n.cd_con_pag_operacao IS NOT NULL) AS notas_credito
  FROM dbamv.itcon_pag i
  JOIN dbamv.con_pag c ON c.cd_con_pag = i.cd_con_pag
  LEFT JOIN dbamv.fornecedor f ON f.cd_fornecedor = c.cd_fornecedor
 WHERE i.cd_itcon_pag = :cd_itcon_pag
""")

# Mesmo bloco da O_PAG (6jvcqtm41dvzd): lançamento de débito da empresa.
SQL_LANCAMENTO_DEBITO = text("""
SELECT l.cd_lan_concor AS cd_lan_concor, l.ds_lan_concor AS ds_lan_concor
  FROM dbamv.lan_concor l, dbamv.config_financ cf
 WHERE l.cd_lan_concor = cf.cd_lanc_debito_conta
   AND cf.cd_multi_empresa = :empresa
""")

SQL_MOEDA_EMPRESA = text(
    'SELECT cd_moeda AS cd_moeda FROM dbamv.multi_empresas WHERE cd_multi_empresa = :empresa'
)

# Processos usados nas baixas recentes da conta (a O_PAG recebe o processo do
# usuário; aqui é sugerido o mais usado, para conferência na simulação).
SQL_PROCESSOS_CONTA = text("""
SELECT (SELECT STATS_MODE(p.cd_processo) FROM dbamv.pagcon_pag p
         WHERE p.cd_con_cor = :conta AND p.dt_pagamento >= TRUNC(SYSDATE) - :dias) AS processo_pagamento,
       (SELECT STATS_MODE(m.cd_processo) FROM dbamv.mov_concor m
         WHERE m.cd_con_cor = :conta AND m.cd_pagcon_pag IS NOT NULL
           AND m.dt_movimentacao >= TRUNC(SYSDATE) - :dias) AS processo_movimento
  FROM dual
""")


def _digitos(valor) -> str:
    return re.sub(r'\D', '', str(valor or ''))


def _sem_zeros(valor) -> str:
    return _digitos(valor).lstrip('0') or '0'


def contas_mv(oracle) -> list[dict]:
    return [dict(linha) for linha in read_payables_rows(
        oracle, SQL_CONTAS_MV, {'dias': DIAS_ATIVIDADE_CONTA}
    )]


def contas_candidatas(extrato: Extrato, contas: list[dict]) -> list[dict]:
    """Contas do MV com o mesmo banco e número; a mais usada nas baixas primeiro.

    O MV grava o número ora sem o dígito (13000656), ora com ele colado
    (130006564); as duas formas são aceitas.
    """
    numeros = {_sem_zeros(extrato.conta)}
    if extrato.conta_dv and extrato.conta_dv.isdigit():
        numeros.add(_sem_zeros(extrato.conta + extrato.conta_dv))
    candidatas = [
        conta for conta in contas
        if int(conta['cd_banco'] or 0) == extrato.banco
        and _sem_zeros(conta['nr_conta']) in numeros
    ]
    return sorted(candidatas, key=lambda conta: (
        -int(conta['baixas_recentes'] or 0),
        'APLIC' in (conta['ds_con_cor'] or '').upper(),
        _sem_zeros(conta['cd_agencia']) != _sem_zeros(extrato.agencia),
        int(conta['cd_con_cor']),
    ))


# ---------------------------------------------------------------- PostgreSQL

SQL_EXTRATO_POR_HASH = text(
    'SELECT id, nome_arquivo FROM api_prontocardio.contas_pagar_extratos WHERE sha256 = :sha256'
)

SQL_INSERIR_EXTRATO = text("""
INSERT INTO api_prontocardio.contas_pagar_extratos
    (sha256, nome_arquivo, arquivo, banco, agencia, conta, conta_dv, cd_con_cor, cd_multi_empresa,
     periodo_inicio, periodo_fim, saldo_inicial, saldo_final, conferido, avisos, qtd_linhas,
     enviado_por, enviado_por_nome)
VALUES (:sha256, :nome_arquivo, :arquivo, :banco, :agencia, :conta, :conta_dv, :cd_con_cor,
        :cd_multi_empresa, :periodo_inicio, :periodo_fim, :saldo_inicial, :saldo_final, :conferido,
        :avisos, :qtd_linhas, :enviado_por, :enviado_por_nome)
RETURNING id
""")

SQL_CHAVES_EXISTENTES = text("""
SELECT DISTINCT chave FROM api_prontocardio.contas_pagar_extrato_linhas WHERE chave IN :chaves
""").bindparams(bindparam('chaves', expanding=True))

SQL_INSERIR_LINHA = text("""
INSERT INTO api_prontocardio.contas_pagar_extrato_linhas
    (extrato_id, ordem, data, historico, documento, valor, chave, repetida)
VALUES (:extrato_id, :ordem, :data, :historico, :documento, :valor, :chave, :repetida)
""")

SQL_LISTAR_EXTRATOS = text("""
SELECT id, nome_arquivo, banco, agencia, conta, conta_dv, cd_con_cor, cd_multi_empresa,
       periodo_inicio, periodo_fim, saldo_inicial, saldo_final, conferido, avisos, qtd_linhas,
       enviado_por_nome, criado_em,
       (SELECT COUNT(*) FROM api_prontocardio.contas_pagar_extrato_linhas l
         WHERE l.extrato_id = e.id AND l.repetida) AS linhas_repetidas
  FROM api_prontocardio.contas_pagar_extratos e
 ORDER BY criado_em DESC, id DESC
 LIMIT :limite
""")

SQL_EXTRATO = text(
    'SELECT * FROM api_prontocardio.contas_pagar_extratos WHERE id = :id'
)

SQL_LINHAS_EXTRATO = text("""
SELECT id, ordem, data, historico, documento, valor, repetida
  FROM api_prontocardio.contas_pagar_extrato_linhas
 WHERE extrato_id = :extrato_id
 ORDER BY ordem
""")

SQL_DEBITOS_CONTA = text("""
SELECT l.id, l.data, l.historico, l.documento, l.valor, l.extrato_id, e.nome_arquivo
  FROM api_prontocardio.contas_pagar_extrato_linhas l
  JOIN api_prontocardio.contas_pagar_extratos e ON e.id = l.extrato_id
 WHERE e.cd_con_cor = :cd_con_cor
   AND l.valor < 0
   AND NOT l.repetida
   AND l.data BETWEEN :desde AND :ate
 ORDER BY l.data, l.id
""")

SQL_LINHA = text("""
SELECT l.id, l.data, l.historico, l.documento, l.valor, l.repetida, l.extrato_id,
       e.cd_con_cor, e.cd_multi_empresa, e.nome_arquivo
  FROM api_prontocardio.contas_pagar_extrato_linhas l
  JOIN api_prontocardio.contas_pagar_extratos e ON e.id = l.extrato_id
 WHERE l.id = :id
""")


def chave_linha(extrato: Extrato, linha, ocorrencia: int) -> str:
    """Identidade do lançamento entre arquivos que se sobrepõem.

    O histórico entra só pela descrição da 1ª linha: a continuação muda entre
    a impressão diária e a mensal.
    """
    historico = re.sub(r'[^0-9A-Z]', '', linha.descricao.upper())[:TAMANHO_CHAVE_HISTORICO]
    bruto = '|'.join((
        str(extrato.banco), _sem_zeros(extrato.agencia), _sem_zeros(extrato.conta),
        linha.data.isoformat(), f'{linha.valor:.2f}', _sem_zeros(linha.documento or ''),
        historico, str(ocorrencia),
    ))
    return hashlib.sha256(bruto.encode('utf-8')).hexdigest()


def _chaves(extrato: Extrato) -> list[str]:
    vistos: dict[tuple, int] = {}
    chaves = []
    for linha in extrato.linhas:
        identidade = (
            linha.data, linha.valor, _sem_zeros(linha.documento or ''),
            re.sub(r'[^0-9A-Z]', '', linha.descricao.upper())[:TAMANHO_CHAVE_HISTORICO],
        )
        vistos[identidade] = vistos.get(identidade, 0) + 1
        chaves.append(chave_linha(extrato, linha, vistos[identidade]))
    return chaves


def _extrato_publico(linha: dict, contas: dict[int, dict] | None = None) -> dict:
    dados = {k: v for k, v in dict(linha).items() if k not in ('arquivo', 'sha256')}
    dados['nome_banco'] = NOMES_BANCO.get(dados['banco'], str(dados['banco']))
    dados['avisos'] = [a for a in (dados.get('avisos') or '').split('\n') if a]
    if contas is not None:
        conta = contas.get(dados.get('cd_con_cor'))
        dados['ds_con_cor'] = conta['ds_con_cor'] if conta else None
    return dados


# ---------------------------------------------------------------- 5a: extratos


@router.get('/contas-mv')
def listar_contas_mv(_: AcessoRegras, oracle: SessionOracle):
    return {'contas': contas_mv(oracle)}


@router.post('', status_code=HTTPStatus.CREATED)
async def enviar_extrato(
    usuario: AcessoRegras,
    session: SessionPostgres,
    oracle: SessionOracle,
    arquivo: Annotated[UploadFile, File()],
):
    conteudo = await arquivo.read(TAMANHO_MAXIMO + 1)
    if len(conteudo) > TAMANHO_MAXIMO:
        raise HTTPException(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, 'Arquivo maior que 8 MB.')
    nome = (arquivo.filename or 'extrato')[:255]
    sha256 = hashlib.sha256(conteudo).hexdigest()
    repetido = session.execute(SQL_EXTRATO_POR_HASH, {'sha256': sha256}).mappings().first()
    if repetido:
        raise HTTPException(
            HTTPStatus.CONFLICT,
            f'Este arquivo já foi enviado (extrato {repetido["id"]}, {repetido["nome_arquivo"]}).',
        )
    try:
        extrato = ler_extrato(conteudo, nome)
    except ExtratoInvalido as exc:
        raise HTTPException(HTTPStatus.UNPROCESSABLE_ENTITY, str(exc)) from exc

    candidatas = contas_candidatas(extrato, contas_mv(oracle))
    conta = candidatas[0] if candidatas else None
    avisos = list(extrato.avisos)
    if conta is None:
        avisos.append('Conta não cadastrada no MV: o extrato fica guardado, mas fora da baixa.')
    chaves = _chaves(extrato)
    existentes = set(
        session.execute(SQL_CHAVES_EXISTENTES, {'chaves': chaves}).scalars()
    ) if chaves else set()

    extrato_id = session.execute(SQL_INSERIR_EXTRATO, {
        'sha256': sha256, 'nome_arquivo': nome, 'arquivo': conteudo,
        'banco': extrato.banco, 'agencia': extrato.agencia, 'conta': extrato.conta,
        'conta_dv': extrato.conta_dv,
        'cd_con_cor': int(conta['cd_con_cor']) if conta else None,
        'cd_multi_empresa': int(conta['cd_multi_empresa']) if conta else None,
        'periodo_inicio': extrato.periodo_inicio, 'periodo_fim': extrato.periodo_fim,
        'saldo_inicial': extrato.saldo_inicial, 'saldo_final': extrato.saldo_final,
        'conferido': extrato.conferido, 'avisos': '\n'.join(avisos) or None,
        'qtd_linhas': len(extrato.linhas),
        'enviado_por': usuario.id, 'enviado_por_nome': getattr(usuario, 'nome', None),
    }).scalar_one()
    for ordem, (linha, chave) in enumerate(zip(extrato.linhas, chaves, strict=True), start=1):
        session.execute(SQL_INSERIR_LINHA, {
            'extrato_id': extrato_id, 'ordem': ordem, 'data': linha.data,
            'historico': linha.historico[:300], 'documento': (linha.documento or None),
            'valor': linha.valor, 'chave': chave, 'repetida': chave in existentes,
        })
    session.commit()
    repetidas = sum(1 for chave in chaves if chave in existentes)
    return {
        'id': extrato_id,
        'nome_arquivo': nome,
        'banco': extrato.banco,
        'nome_banco': extrato.nome_banco,
        'agencia': extrato.agencia,
        'conta': extrato.conta,
        'conta_dv': extrato.conta_dv,
        'periodo_inicio': extrato.periodo_inicio,
        'periodo_fim': extrato.periodo_fim,
        'saldo_inicial': extrato.saldo_inicial,
        'saldo_final': extrato.saldo_final,
        'conferido': extrato.conferido,
        'avisos': avisos,
        'qtd_linhas': len(extrato.linhas),
        'linhas_repetidas': repetidas,
        'cd_con_cor': int(conta['cd_con_cor']) if conta else None,
        'ds_con_cor': conta['ds_con_cor'] if conta else None,
        'contas_candidatas': [
            {'cd_con_cor': int(c['cd_con_cor']), 'ds_con_cor': c['ds_con_cor'],
             'cd_multi_empresa': int(c['cd_multi_empresa'])}
            for c in candidatas
        ],
    }


@router.get('')
def listar_extratos(
    _: AcessoRegras,
    session: SessionPostgres,
    oracle: SessionOracle,
    limite: Annotated[int, Query(ge=1, le=500)] = 100,
):
    contas = {int(c['cd_con_cor']): c for c in contas_mv(oracle)}
    linhas = session.execute(SQL_LISTAR_EXTRATOS, {'limite': limite}).mappings().all()
    return {'extratos': [_extrato_publico(linha, contas) for linha in linhas]}


def _extrato_ou_404(session, extrato_id: int) -> dict:
    extrato = session.execute(SQL_EXTRATO, {'id': extrato_id}).mappings().first()
    if extrato is None:
        raise HTTPException(HTTPStatus.NOT_FOUND, 'Extrato não encontrado.')
    return dict(extrato)


@router.get('/{extrato_id}')
def ver_extrato(extrato_id: int, _: AcessoRegras, session: SessionPostgres, oracle: SessionOracle):
    contas = {int(c['cd_con_cor']): c for c in contas_mv(oracle)}
    extrato = _extrato_publico(_extrato_ou_404(session, extrato_id), contas)
    linhas = session.execute(SQL_LINHAS_EXTRATO, {'extrato_id': extrato_id}).mappings().all()
    return {**extrato, 'linhas': [dict(linha) for linha in linhas]}


@router.get('/{extrato_id}/arquivo')
def baixar_arquivo(extrato_id: int, _: AcessoRegras, session: SessionPostgres):
    extrato = _extrato_ou_404(session, extrato_id)
    tipo = 'application/pdf' if bytes(extrato['arquivo'][:4]) == b'%PDF' else (
        'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    )
    nome = re.sub(r'[^\w.\- ]', '_', extrato['nome_arquivo'])
    return Response(
        bytes(extrato['arquivo']), media_type=tipo,
        headers={'Content-Disposition': f'inline; filename="{nome}"'},
    )


class ContaInput(BaseModel):
    cd_con_cor: int | None = Field(default=None, gt=0)


@router.put('/{extrato_id}/conta')
def definir_conta(
    extrato_id: int,
    dados: ContaInput,
    _: AcessoRegras,
    session: SessionPostgres,
    oracle: SessionOracle,
):
    _extrato_ou_404(session, extrato_id)
    empresa = None
    if dados.cd_con_cor is not None:
        conta = next(
            (c for c in contas_mv(oracle) if int(c['cd_con_cor']) == dados.cd_con_cor), None
        )
        if conta is None:
            raise HTTPException(HTTPStatus.UNPROCESSABLE_ENTITY, 'Conta corrente não existe no MV.')
        empresa = int(conta['cd_multi_empresa'])
    session.execute(text("""
        UPDATE api_prontocardio.contas_pagar_extratos
           SET cd_con_cor = :cd_con_cor, cd_multi_empresa = :empresa
         WHERE id = :id
    """), {'cd_con_cor': dados.cd_con_cor, 'empresa': empresa, 'id': extrato_id})
    session.commit()
    return {'id': extrato_id, 'cd_con_cor': dados.cd_con_cor, 'cd_multi_empresa': empresa}


@router.delete('/{extrato_id}')
def excluir_extrato(extrato_id: int, _: AcessoRegras, session: SessionPostgres):
    _extrato_ou_404(session, extrato_id)
    session.execute(
        text('DELETE FROM api_prontocardio.contas_pagar_extratos WHERE id = :id'), {'id': extrato_id}
    )
    session.commit()
    return {'id': extrato_id, 'excluido': True}


# ---------------------------------------------------------------- 5b: baixa


def _saldo_titulo(titulo: dict) -> Decimal:
    return (Decimal(str(titulo['vl_duplicata'])) - Decimal(str(titulo['vl_soma_pago']))).quantize(Decimal('0.01'))


def _data(valor) -> date | None:
    if valor is None:
        return None
    return valor.date() if hasattr(valor, 'date') and callable(valor.date) else valor


def _titulo_publico(titulo: dict, data_debito: date) -> dict:
    vencimento = _data(titulo['dt_vencimento'])
    return {
        'cd_itcon_pag': int(titulo['cd_itcon_pag']),
        'cd_con_pag': int(titulo['cd_con_pag']),
        'nr_parcela': titulo['nr_parcela'],
        'cd_fornecedor': titulo['cd_fornecedor'],
        'nm_fornecedor': titulo['nm_fornecedor'],
        'nr_documento': titulo['nr_documento'],
        'ds_con_pag': titulo['ds_con_pag'],
        'dt_vencimento': vencimento,
        'saldo': _saldo_titulo(titulo),
        'dias': (data_debito - vencimento).days,
    }


def sugerir_baixas(debitos: list[dict], titulos: list[dict]) -> list[dict]:
    """Títulos de mesmo valor com vencimento na janela de cada débito.

    Nada é escolhido sozinho: com mais de um candidato, todos são listados
    (o mais próximo do vencimento primeiro) e o débito fica "ambíguo".
    """
    por_valor: dict[Decimal, list[dict]] = {}
    for titulo in titulos:
        por_valor.setdefault(_saldo_titulo(titulo), []).append(titulo)
    usos: dict[int, int] = {}
    resultado = []
    for debito in debitos:
        valor = -Decimal(str(debito['valor'])).quantize(Decimal('0.01'))
        data_debito = _data(debito['data'])
        candidatos = [
            _titulo_publico(titulo, data_debito)
            for titulo in por_valor.get(valor, [])
            if -DIAS_ANTES_VENCIMENTO <= (data_debito - _data(titulo['dt_vencimento'])).days <= DIAS_DEPOIS_VENCIMENTO
        ]
        candidatos.sort(key=lambda c: (abs(c['dias']), c['cd_itcon_pag']))
        for candidato in candidatos:
            usos[candidato['cd_itcon_pag']] = usos.get(candidato['cd_itcon_pag'], 0) + 1
        resultado.append({
            'linha_id': debito['id'],
            'data': data_debito,
            'historico': debito['historico'],
            'documento': debito['documento'],
            'valor': valor,
            'extrato_id': debito['extrato_id'],
            'nome_arquivo': debito['nome_arquivo'],
            'candidatos': candidatos,
        })
    for item in resultado:
        disputados = [c['cd_itcon_pag'] for c in item['candidatos'] if usos[c['cd_itcon_pag']] > 1]
        item['situacao'] = (
            'sem_titulo' if not item['candidatos']
            else 'ambiguo' if len(item['candidatos']) > 1 or disputados
            else 'unico'
        )
        item['titulos_em_outros_debitos'] = disputados
    return resultado


@router.get('/baixas/sugestoes')
def sugestoes_de_baixa(
    _: AcessoRegras,
    session: SessionPostgres,
    oracle: SessionOracle,
    cd_con_cor: Annotated[int, Query(gt=0)],
    desde: date,
    ate: date,
):
    if ate < desde or (ate - desde).days > MAX_DIAS_CONSULTA:
        raise HTTPException(HTTPStatus.UNPROCESSABLE_ENTITY, f'Informe um período de até {MAX_DIAS_CONSULTA} dias.')
    conta = next((c for c in contas_mv(oracle) if int(c['cd_con_cor']) == cd_con_cor), None)
    if conta is None:
        raise HTTPException(HTTPStatus.NOT_FOUND, 'Conta corrente não encontrada no MV.')
    debitos = [dict(linha) for linha in session.execute(
        SQL_DEBITOS_CONTA, {'cd_con_cor': cd_con_cor, 'desde': desde, 'ate': ate}
    ).mappings().all()]
    titulos = [dict(t) for t in read_payables_rows(oracle, SQL_TITULOS_ABERTOS, {
        'empresa': int(conta['cd_multi_empresa']),
        'inicio': desde - timedelta(days=DIAS_DEPOIS_VENCIMENTO),
        'fim': ate + timedelta(days=DIAS_ANTES_VENCIMENTO),
    })] if debitos else []
    sugestoes = sugerir_baixas(debitos, titulos)
    resumo = {
        situacao: sum(1 for s in sugestoes if s['situacao'] == situacao)
        for situacao in ('unico', 'ambiguo', 'sem_titulo')
    }
    return {
        'cd_con_cor': cd_con_cor,
        'ds_con_cor': conta['ds_con_cor'],
        'cd_multi_empresa': int(conta['cd_multi_empresa']),
        'desde': desde,
        'ate': ate,
        'janela_dias': {'antes': DIAS_ANTES_VENCIMENTO, 'depois': DIAS_DEPOIS_VENCIMENTO},
        'resumo': resumo,
        'sugestoes': sugestoes,
    }


class SimulacaoInput(BaseModel):
    cd_itcon_pag: int = Field(gt=0)


def verificar_baixa(linha: dict, titulo: dict, valor: Decimal, data_debito: date) -> tuple[list[str], list[str]]:
    """Pendências (impedem a baixa) e avisos (conferir) da O_PAG para o par débito x parcela."""
    pendencias: list[str] = []
    avisos: list[str] = []
    saldo = _saldo_titulo(titulo)
    if valor <= 0:
        pendencias.append('A linha do extrato não é um débito.')
    if linha['repetida']:
        pendencias.append('Esta linha já veio em outro extrato; use a linha original.')
    if linha['cd_con_cor'] is None:
        pendencias.append('O extrato não está ligado a uma conta corrente do MV.')
    elif int(titulo['cd_multi_empresa']) != int(linha['cd_multi_empresa']):
        pendencias.append('O título é de outra empresa que não a da conta corrente.')
    if (titulo['tp_quitacao'] or '') == 'Q':
        pendencias.append('A parcela já está quitada no MV.')
    if titulo['cd_itcon_pag_estorno'] is not None:
        pendencias.append('A parcela foi estornada no MV.')
    if saldo != valor:
        pendencias.append(f'O saldo da parcela (R$ {saldo:.2f}) é diferente do débito (R$ {valor:.2f}).')
    if titulo['cd_lote_pgto'] is not None:
        avisos.append(f'A parcela está no lote de pagamento {titulo["cd_lote_pgto"]}; confira antes de baixar.')
    if int(titulo['notas_credito'] or 0):
        avisos.append('O fornecedor tem notas de crédito/débito no MV (a O_PAG oferece abatimento).')
    dias = (data_debito - _data(titulo['dt_vencimento'])).days
    if not -DIAS_ANTES_VENCIMENTO <= dias <= DIAS_DEPOIS_VENCIMENTO:
        avisos.append(f'O débito está {dias} dias distante do vencimento.')
    return pendencias, avisos


@router.post('/linhas/{linha_id}/simulacao')
def simular_baixa(
    linha_id: int,
    dados: SimulacaoInput,
    _: AcessoRegras,
    session: SessionPostgres,
    oracle: SessionOracle,
):
    """O que a baixa gravaria no MV (O_PAG). Só leitura: nada é gravado."""
    linha = session.execute(SQL_LINHA, {'id': linha_id}).mappings().first()
    if linha is None:
        raise HTTPException(HTTPStatus.NOT_FOUND, 'Linha do extrato não encontrada.')
    titulo = next(iter(read_payables_rows(oracle, SQL_TITULO, {'cd_itcon_pag': dados.cd_itcon_pag})), None)
    if titulo is None:
        raise HTTPException(HTTPStatus.NOT_FOUND, 'Parcela não encontrada no MV.')
    titulo = dict(titulo)
    valor = -Decimal(str(linha['valor'])).quantize(Decimal('0.01'))
    data_debito = _data(linha['data'])
    pendencias, avisos = verificar_baixa(linha, titulo, valor, data_debito)

    empresa = int(linha['cd_multi_empresa']) if linha['cd_multi_empresa'] is not None else int(titulo['cd_multi_empresa'])
    lancamento = next(iter(read_payables_rows(oracle, SQL_LANCAMENTO_DEBITO, {'empresa': empresa})), None)
    moeda = next(iter(read_payables_rows(oracle, SQL_MOEDA_EMPRESA, {'empresa': empresa})), None)
    processos = next(iter(read_payables_rows(oracle, SQL_PROCESSOS_CONTA, {
        'conta': linha['cd_con_cor'] or 0, 'dias': DIAS_ATIVIDADE_CONTA,
    })), None) or {}
    if lancamento is None:
        pendencias.append('A empresa não tem lançamento de débito em conta (CONFIG_FINANC).')

    descricao = f'REF. PGTO {titulo["nr_parcela"]} {titulo["nm_fornecedor"] or ""} {titulo["nr_documento"] or ""}'.strip()
    return {
        'linha_id': linha_id,
        'cd_itcon_pag': dados.cd_itcon_pag,
        'pode_baixar': not pendencias,
        'pendencias': pendencias,
        'avisos': avisos,
        'gravacao_habilitada': False,
        'titulo': _titulo_publico(titulo, data_debito),
        'pagcon_pag': {
            'cd_pagcon_pag': 'seq_pagcon_pag.nextval',
            'cd_itcon_pag': dados.cd_itcon_pag,
            'dt_pagamento': data_debito,
            'dt_baixa': data_debito,
            'vl_pago': valor,
            'vl_moeda': valor,
            'cd_moeda': moeda['cd_moeda'] if moeda else None,
            'tp_pagamento': '5',
            'cd_con_cor': linha['cd_con_cor'],
            'cd_processo': processos.get('processo_pagamento'),
            'ds_pagcon_pag': descricao[:250],
            'cd_multi_empresa': empresa,
        },
        'mov_concor': {
            'cd_mov_concor': 'seq_mov_concor.nextval',
            'cd_pagcon_pag': '(o do pagamento acima)',
            'cd_con_cor': linha['cd_con_cor'],
            'cd_lan_concor': lancamento['cd_lan_concor'] if lancamento else None,
            'ds_lan_concor': lancamento['ds_lan_concor'] if lancamento else None,
            'dt_movimentacao': data_debito,
            'vl_movimentacao': valor,
            'nr_documento_identificacao': f'DEB C/C- {dados.cd_itcon_pag}',
            'ds_movimentacao': f'PAGTO DEBITO EM C/C - {titulo["ds_con_pag"] or ""}'[:250],
            'sn_conciliado': 'S',
            'cd_processo': processos.get('processo_movimento'),
        },
        'efeitos_no_mv': [
            'TRG_PAGCON_PAG quita a parcela (ITCON_PAG: VL_SOMA_PAGO, TP_QUITACAO=Q, SN_BAIXADA=S, DT_QUITACAO).',
            'TRG_PAGCON_PAG grava o rateio do pagamento (RAT_PAGCON_PAG, cópia do RATCON_PAG).',
            'TRG_PAGCON_PAG_GRAVA_LOG registra o pagamento em LOG_CON_PAG_CON_REC.',
        ],
    }
