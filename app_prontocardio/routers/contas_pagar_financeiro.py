# ruff: noqa: E501, PLR0913
"""Contas a Pagar: conferência do financeiro e desfazer importação no MV.

- GET  /financeiro/conferencia: todas as NFS-e que chegaram ao financeiro
  (com boleto ou já importadas), com boletos, prazo de 3 dias úteis,
  justificativas e o registro da importação.
- GET  /recebidas/{cnpj}/{numero}/importacao/desfazer: o que seria removido
  do MV e o que impede (só leitura).
- POST /recebidas/{cnpj}/{numero}/importacao/desfazer: remove título,
  impostos e entrada criados pelo Receita Certa; a nota volta para
  "Pronta para o financeiro".
"""

import json
import logging
from datetime import date, datetime
from decimal import Decimal
from http import HTTPStatus
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, Field
from sqlalchemy import text

from app_prontocardio.routers import contas_pagar_boletos as boletos_rota
from app_prontocardio.routers.contas_pagar_nfse import (
    AcessoFila,
    SessionOracle,
    SessionPostgres,
    _decimal,
    _gestor,
    listar_recebidas,
    nfse_cancelada,
)
from app_prontocardio.routers.mv_identidade import Config, _vinculo
from app_prontocardio.services.contas_pagar_desfazer import (
    executar_desfazer,
    planejar_desfazer,
)
from app_prontocardio.services.contas_pagar_importacao import ImportacaoFalhou
from app_prontocardio.services.dias_uteis import prazo_minimo
from app_prontocardio.services.payables_oficinas import somente_digitos

router = APIRouter(
    prefix='/app_glosas/financeiro/contas-a-pagar/nfse',
    tags=['contas-a-pagar-financeiro'],
)
logger = logging.getLogger(__name__)

ETAPAS = ('a_importar', 'pendencia', 'importada', 'lancada_mv', 'cancelada')

SQL_BOLETOS_DAS_NOTAS_DETALHE = text("""
SELECT b.id, b.prestador_cnpj, b.numero_nfse, b.banco, b.valor, b.vencimento, b.linha_digitavel,
       b.origem_leitura, b.divergencias, b.enviado_em, b.prazo_minimo, b.fora_do_prazo, b.justificativa_atraso,
       u.nome AS enviado_por_nome
  FROM api_prontocardio.contas_pagar_boletos b
  LEFT JOIN api_prontocardio.usuarios_api u ON u.id = b.enviado_por
 WHERE b.prestador_cnpj = ANY(:cnpjs)
 ORDER BY b.vencimento, b.id
""")

SQL_REGISTROS_DAS_NOTAS = text("""
SELECT DISTINCT ON (r.prestador_cnpj, r.numero_nfse)
       r.prestador_cnpj, r.numero_nfse, r.status, r.cd_usuario_mv, r.cd_ent_serv, r.cd_con_pag, r.erro,
       r.concluido_em, r.desfeita_em, r.cd_usuario_mv_desfazer, r.motivo_desfazer,
       u.nome AS importado_por_nome, d.nome AS desfeito_por_nome
  FROM api_prontocardio.contas_pagar_importacoes r
  LEFT JOIN api_prontocardio.usuarios_api u ON u.id = r.usuario_id
  LEFT JOIN api_prontocardio.usuarios_api d ON d.id = r.desfeita_por
 WHERE r.prestador_cnpj = ANY(:cnpjs)
 ORDER BY r.prestador_cnpj, r.numero_nfse, r.id DESC
""")

SQL_DESFAZER_REGISTRO = text("""
UPDATE api_prontocardio.contas_pagar_importacoes
   SET status = 'desfeita', desfeita_em = now(), desfeita_por = :usuario_id,
       cd_usuario_mv_desfazer = :login, motivo_desfazer = :motivo, desfazer_detalhe = CAST(:detalhe AS jsonb)
 WHERE id = :id AND status = 'importada'
""")


class DesfazerInput(BaseModel):
    motivo: str = Field(min_length=10, max_length=400)
    cd_con_pag: int = Field(gt=0, description='Número do título conferido pelo operador.')


def _acesso_financeiro(usuario) -> None:
    if not _gestor(usuario):
        raise HTTPException(HTTPStatus.FORBIDDEN, 'Só o financeiro acessa a conferência.')


def _etapa(item: dict, registro: dict | None) -> str:
    if registro and registro['status'] == 'importada':
        return 'importada'
    if item['situacao'] == 'lancada':
        return 'lancada_mv'
    if nfse_cancelada(item.get('status_nfse')) or item['situacao'] == 'cancelada':
        return 'cancelada'
    return 'a_importar' if item.get('pronta') else 'pendencia'


def _pendencias_da_nota(item: dict, boletos: list[dict]) -> list[str]:
    problemas = []
    for b in boletos:
        problemas += list(b['divergencias'])
        if b['fora_do_prazo'] and not b['justificativa_atraso']:
            problemas.append(f'Boleto de {b["vencimento"]:%d/%m} fora do prazo sem justificativa.')
    soma = sum((_decimal(b['valor']) or Decimal('0') for b in boletos), Decimal('0'))
    liquido = _decimal(item.get('valor_liquido')) or Decimal('0')
    if boletos and abs(soma - liquido) > Decimal('0.01'):
        problemas.append(f'Boletos somam R$ {soma:.2f}; líquido da nota R$ {liquido:.2f}.')
    if item['situacao'] == 'a_classificar':
        problemas.append('Fornecedor sem setor definido.')
    return list(dict.fromkeys(problemas))


@router.get('/financeiro/conferencia')
def conferencia_financeiro(
    usuario: AcessoFila,
    session: SessionPostgres,
    oracle: SessionOracle,
    etapa: Literal['todas', 'a_importar', 'pendencia', 'importada', 'lancada_mv', 'cancelada'] = 'todas',
    desde: Annotated[date | None, Query()] = None,
    cd_multi_empresa: int | None = None,
):
    """NFS-e que chegaram ao financeiro: com boleto anexado ou importadas pelo Receita Certa."""
    _acesso_financeiro(usuario)
    fila = listar_recebidas(usuario, session, oracle, situacao='todas', cd_oficina=None, desde=desde)
    itens = fila.get('itens', [])
    cnpjs = sorted({i['prestador_cnpj'] for i in itens})
    boletos: dict[tuple, list[dict]] = {}
    registros: dict[tuple, dict] = {}
    if cnpjs:
        for b in session.execute(SQL_BOLETOS_DAS_NOTAS_DETALHE, {'cnpjs': cnpjs}).mappings():
            linha = dict(b)
            linha['divergencias'] = json.loads(linha.get('divergencias') or '[]')
            boletos.setdefault((linha['prestador_cnpj'], linha['numero_nfse']), []).append(linha)
        if session.execute(boletos_rota.SQL_TABELA_IMPORTACOES).scalar_one():
            for r in session.execute(SQL_REGISTROS_DAS_NOTAS, {'cnpjs': cnpjs}).mappings():
                registros[(r['prestador_cnpj'], r['numero_nfse'])] = dict(r)

    hoje_prazo = prazo_minimo(boletos_rota._agora_local())
    resultado, resumo = [], {e: {'quantidade': 0, 'valor_liquido': Decimal('0')} for e in ETAPAS}
    for item in itens:
        chave = (item['prestador_cnpj'], item['numero_nfse'])
        bols, registro = boletos.get(chave, []), registros.get(chave)
        if not bols and not registro:
            continue
        if cd_multi_empresa is not None and item.get('cd_multi_empresa') != cd_multi_empresa:
            continue
        fase = _etapa(item, registro)
        liquido = _decimal(item.get('valor_liquido')) or Decimal('0')
        servicos = _decimal(item.get('valor_servicos')) or Decimal('0')
        resumo[fase]['quantidade'] += 1
        resumo[fase]['valor_liquido'] += liquido
        if etapa not in ('todas', fase):
            continue
        vencimentos = [b['vencimento'] for b in bols]
        resultado.append({
            **{k: item.get(k) for k in ('chave', 'prestador_cnpj', 'prestador', 'numero_nfse', 'codigo_verificacao',
                                        'data_emissao', 'empresa', 'cd_multi_empresa', 'ds_oficina', 'cd_setor', 'nm_setor',
                                        'status_nfse', 'mv')},
            'etapa': fase,
            'valor_servicos': servicos,
            'valor_retido': servicos - liquido,
            'valor_liquido': liquido,
            'primeiro_vencimento': min(vencimentos) if vencimentos else None,
            'fora_do_prazo': any(b['fora_do_prazo'] for b in bols),
            'boletos': bols,
            'pendencias': _pendencias_da_nota(item, bols) if fase == 'pendencia' else [],
            'importacao': registro,
            'pode_desfazer': bool(registro and registro['status'] == 'importada'),
        })
    resultado.sort(key=lambda i: (i['primeiro_vencimento'] or date.max, i['data_emissao'] or date.min))
    return {
        'desde': fila.get('desde'),
        'prazo_minimo_hoje': hoje_prazo,
        'resumo': resumo,
        'fora_do_prazo': sum(1 for i in resultado if i['fora_do_prazo']),
        'itens': resultado,
    }


# --------------------------------------------------------------- desfazer


def _preparar_desfazer(cnpj: str, numero: str, usuario, session, oracle) -> tuple[dict, dict, dict]:
    _acesso_financeiro(usuario)
    nota = boletos_rota._nota_ou_404(session, oracle, usuario, cnpj, numero)
    registro = boletos_rota._importacao_registrada(session, cnpj, numero)
    if registro is None or registro['status'] != 'importada':
        raise HTTPException(HTTPStatus.CONFLICT, 'Só é possível desfazer importações feitas pelo Receita Certa.')
    vinculo = _vinculo(session, usuario.id)
    if not vinculo['pode_gravar_no_mv']:
        raise HTTPException(HTTPStatus.PRECONDITION_FAILED, 'Entre com o seu login MV antes.')
    registro = {**registro, 'cd_multi_empresa': registro.get('cd_multi_empresa') or nota.get('cd_multi_empresa')}
    return nota, registro, vinculo


@router.get('/recebidas/{cnpj}/{numero}/importacao/desfazer')
def previa_desfazer(cnpj: str, numero: str, usuario: AcessoFila, session: SessionPostgres,
                    oracle: SessionOracle, config: Config):
    """O que será removido do MV e o que impede. Não grava nada."""
    cnpj = somente_digitos(cnpj)
    nota, registro, vinculo = _preparar_desfazer(cnpj, numero, usuario, session, oracle)
    with boletos_rota._sessao(vinculo, usuario, config) as conexao:
        try:
            plano = planejar_desfazer(conexao.cursor(), registro=registro)
        finally:
            conexao.rollback()
    return {'nota': boletos_rota._resumo_nota(nota), 'importacao': registro, 'usuario_mv': vinculo['cd_usuario_mv'],
            'gravacao_habilitada': bool(config.CP_IMPORTACAO_MV_HABILITADA), **plano}


@router.post('/recebidas/{cnpj}/{numero}/importacao/desfazer')
def desfazer_importacao(cnpj: str, numero: str, dados: DesfazerInput, usuario: AcessoFila,
                        session: SessionPostgres, oracle: SessionOracle, config: Config):
    """Remove do MV o título, os títulos de imposto e a entrada criados pelo Receita Certa.

    Uma transação na sessão MV do operador; qualquer erro desfaz tudo. A nota
    volta para "Pronta para o financeiro" e pode ser importada de novo.
    """
    if not config.CP_IMPORTACAO_MV_HABILITADA:
        raise HTTPException(HTTPStatus.SERVICE_UNAVAILABLE, 'A gravação no MV está desligada (CP_IMPORTACAO_MV_HABILITADA).')
    cnpj = somente_digitos(cnpj)
    _, registro, vinculo = _preparar_desfazer(cnpj, numero, usuario, session, oracle)
    if int(registro['cd_con_pag'] or 0) != dados.cd_con_pag:
        raise HTTPException(HTTPStatus.CONFLICT, 'O título informado não é o desta importação. Atualize a tela e confira.')
    with boletos_rota._sessao(vinculo, usuario, config) as conexao:
        try:
            cursor = conexao.cursor()
            plano = planejar_desfazer(cursor, registro=registro)
            if not plano['pode_desfazer']:
                raise HTTPException(HTTPStatus.CONFLICT, 'Não é possível desfazer: ' + ' '.join(plano['pendencias']))
            removido = executar_desfazer(cursor, plano)
            conexao.commit()
        except HTTPException:
            conexao.rollback()
            raise
        except Exception as exc:
            conexao.rollback()
            erro = str(exc) if isinstance(exc, ImportacaoFalhou) else boletos_rota.mensagem_oracle(exc)
            logger.warning('Desfazer importação da NFS-e %s/%s recusado: %s', cnpj, numero, erro)
            raise HTTPException(HTTPStatus.UNPROCESSABLE_ENTITY, f'O MV recusou; nada foi alterado. {erro}') from exc

    session.execute(SQL_DESFAZER_REGISTRO, {
        'id': registro['id'], 'usuario_id': usuario.id, 'login': vinculo['cd_usuario_mv'],
        'motivo': ' '.join(dados.motivo.split()),
        'detalhe': json.dumps(jsonable_encoder({'removido': removido, 'plano': plano, 'em': datetime.now().isoformat()})),
    })
    session.commit()
    return {'desfeita': True, 'cd_usuario_mv': vinculo['cd_usuario_mv'], **removido}
