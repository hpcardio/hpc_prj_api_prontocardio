# ruff: noqa: E501, PLR0913
"""Contas a Pagar: NFS-e recebidas por oficina (setor) e regras fornecedor->oficina.

- As NFS-e recebidas vêm da DAG ``extracao_nfse_tomador`` (tabela
  ``api_prontocardio.nfse_xml_tomador``).
- A oficina de cada nota sai da regra do fornecedor (CNPJ), sugerida pelo
  histórico de ``DBAMV.ENT_SERV``; sem regra possível, a nota fica
  "a classificar" para o financeiro.
- Uma nota deixa a fila quando já existe entrada de serviço ou título no MV
  para o mesmo fornecedor e número.
"""

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from http import HTTPStatus
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session

from app_prontocardio.database import get_session_oracle, get_session_postgres
from app_prontocardio.models import Usuario
from app_prontocardio.security import valida_token_usuario_atual
from app_prontocardio.services.nfse_manuais import FONTE_NFSE_RECEBIDAS
from app_prontocardio.services.nfse_setor_escopo import (
    EscopoFila,
    lancadores_por_fornecedor,
    oficinas_por_login,
)
from app_prontocardio.services.payables_mv import read_payables_rows
from app_prontocardio.services.payables_oficinas import (
    MESES_HISTORICO,
    STATUS_A_CLASSIFICAR,
    STATUS_DEFINIDA,
    situacao_da_nota,
    somente_digitos,
    sugerir_regras,
)

router = APIRouter(
    prefix='/app_glosas/financeiro/contas-a-pagar/nfse',
    tags=['contas-a-pagar-nfse'],
)
SessionPostgres = Annotated[Session, Depends(get_session_postgres)]
SessionOracle = Annotated[Session, Depends(get_session_oracle)]
UsuarioAtual = Annotated[Usuario, Depends(valida_token_usuario_atual)]

FUSO_HOSPITAL = ZoneInfo('America/Sao_Paulo')
PERFIS_GESTAO = {'ti', 'administrador'}
TELA_SETOR = 'contas_pagar_nfse_setor'
TELA_REGRAS = 'contas_pagar_nfse_regras'
LOTE_IN_ORACLE = 900
MESES_BUSCA_LANCAMENTO = 6
TAMANHO_DISCRIMINACAO = 300


# ---------------------------------------------------------------- acesso


def _gestor(usuario: Usuario) -> bool:
    return usuario.perfil in PERFIS_GESTAO or TELA_REGRAS in (
        usuario.telas_permitidas or []
    )


def acesso_regras(usuario: UsuarioAtual) -> Usuario:
    if not _gestor(usuario):
        raise HTTPException(
            HTTPStatus.FORBIDDEN,
            'Acesso restrito ao financeiro (regras de oficina).',
        )
    return usuario


def acesso_fila(usuario: UsuarioAtual) -> Usuario:
    if _gestor(usuario) or TELA_SETOR in (usuario.telas_permitidas or []):
        return usuario
    raise HTTPException(
        HTTPStatus.FORBIDDEN, 'Sem acesso às NFS-e recebidas por setor.'
    )


AcessoRegras = Annotated[Usuario, Depends(acesso_regras)]
AcessoFila = Annotated[Usuario, Depends(acesso_fila)]


# ---------------------------------------------------------------- MV

# O setor da NFS-e é o setor do MV da oficina (OFICINA.CD_SETOR -> SETOR).
SQL_OFICINAS = text("""
SELECT o.cd_oficina AS cd_oficina, o.ds_oficina AS ds_oficina,
       o.cd_setor AS cd_setor, s.nm_setor AS nm_setor,
       o.cd_multi_empresa AS cd_multi_empresa
  FROM dbamv.oficina o
  LEFT JOIN dbamv.setor s ON s.cd_setor = o.cd_setor
 ORDER BY o.cd_multi_empresa, o.ds_oficina
""")

SQL_EMPRESAS = text("""
SELECT m.cd_multi_empresa AS cd_multi_empresa, m.ds_multi_empresa AS ds_multi_empresa,
       m.cd_cgc AS cnpj
  FROM dbamv.multi_empresas m
""")

SQL_HISTORICO = text("""
SELECT f.nr_cgc_cpf AS cnpj,
       f.cd_fornecedor AS cd_fornecedor,
       f.nm_fornecedor AS nm_fornecedor,
       o.cd_multi_empresa AS cd_multi_empresa,
       e.cd_oficina AS cd_oficina,
       o.ds_oficina AS ds_oficina,
       COUNT(*) AS qtd
  FROM dbamv.ent_serv e
  JOIN dbamv.fornecedor f ON f.cd_fornecedor = e.cd_fornecedor
  JOIN dbamv.oficina o ON o.cd_oficina = e.cd_oficina
 WHERE e.dt_entrada >= ADD_MONTHS(TRUNC(SYSDATE), -:meses)
 GROUP BY f.nr_cgc_cpf, f.cd_fornecedor, f.nm_fornecedor, o.cd_multi_empresa,
          e.cd_oficina, o.ds_oficina
""")

# Entradas de serviço e títulos já lançados para os fornecedores da fila.
SQL_LANCADAS = text("""
SELECT REGEXP_REPLACE(f.nr_cgc_cpf, '[^0-9]', '') AS cnpj,
       e.nr_documento AS nr_documento,
       e.cd_ent_serv AS cd_ent_serv,
       e.cd_con_pag AS cd_con_pag,
       e.cd_usuario AS cd_usuario,
       'ENT_SERV' AS origem
  FROM dbamv.ent_serv e
  JOIN dbamv.fornecedor f ON f.cd_fornecedor = e.cd_fornecedor
 WHERE e.dt_entrada >= ADD_MONTHS(TRUNC(SYSDATE), -:meses)
   AND REGEXP_REPLACE(f.nr_cgc_cpf, '[^0-9]', '') IN :cnpjs
UNION ALL
SELECT REGEXP_REPLACE(f.nr_cgc_cpf, '[^0-9]', '') AS cnpj,
       c.nr_documento AS nr_documento,
       NULL AS cd_ent_serv,
       c.cd_con_pag AS cd_con_pag,
       c.cd_usuario_ins AS cd_usuario,
       'CON_PAG' AS origem
  FROM dbamv.con_pag c
  JOIN dbamv.fornecedor f ON f.cd_fornecedor = c.cd_fornecedor
 WHERE c.dt_lancamento >= ADD_MONTHS(TRUNC(SYSDATE), -:meses)
   AND REGEXP_REPLACE(f.nr_cgc_cpf, '[^0-9]', '') IN :cnpjs
""").bindparams(bindparam('cnpjs', expanding=True))


def _oficinas_mv(oracle: Session) -> dict[int, dict]:
    linhas = read_payables_rows(oracle, SQL_OFICINAS, {})
    return {int(linha['cd_oficina']): dict(linha) for linha in linhas}


def _empresas_mv(oracle: Session) -> dict[str, dict]:
    """CNPJ (só dígitos) -> empresa do MV; identifica a empresa tomadora da nota."""
    linhas = read_payables_rows(oracle, SQL_EMPRESAS, {})
    return {somente_digitos(linha['cnpj']): dict(linha) for linha in linhas if linha['cnpj']}


def _chave_documento(valor) -> str:
    return somente_digitos(valor).lstrip('0')


def _lancadas_mv(oracle: Session, cnpjs: list[str]) -> dict[tuple[str, str], dict]:
    lancadas: dict[tuple[str, str], dict] = {}
    unicos = sorted(set(cnpjs))
    for inicio in range(0, len(unicos), LOTE_IN_ORACLE):
        lote = unicos[inicio:inicio + LOTE_IN_ORACLE]
        linhas = read_payables_rows(
            oracle,
            SQL_LANCADAS,
            {'cnpjs': lote, 'meses': MESES_BUSCA_LANCAMENTO},
        )
        for linha in linhas:
            chave = (linha['cnpj'], _chave_documento(linha['nr_documento']))
            atual = lancadas.setdefault(
                chave, {'cd_ent_serv': None, 'cd_con_pag': None, 'lancado_por': []}
            )
            atual['cd_ent_serv'] = atual['cd_ent_serv'] or linha['cd_ent_serv']
            atual['cd_con_pag'] = atual['cd_con_pag'] or linha['cd_con_pag']
            quem = (linha.get('cd_usuario') or '').strip().upper()
            if quem and quem not in atual['lancado_por']:
                atual['lancado_por'].append(quem)
    return lancadas


# ---------------------------------------------------------------- PostgreSQL

SQL_REGRAS = text("""
SELECT cnpj, cd_multi_empresa, cd_fornecedor, nm_fornecedor, cd_oficina, ds_oficina, status,
       origem, qtd_entradas, participacao, motivo, usuario_id, atualizado_em
  FROM api_prontocardio.contas_pagar_regras_oficina
""")

SQL_UPSERT_HISTORICO = text("""
INSERT INTO api_prontocardio.contas_pagar_regras_oficina
    (cnpj, cd_multi_empresa, cd_fornecedor, nm_fornecedor, cd_oficina, ds_oficina, status,
     origem, qtd_entradas, participacao, motivo, usuario_id, atualizado_em)
VALUES (:cnpj, :cd_multi_empresa, :cd_fornecedor, :nm_fornecedor, :cd_oficina, :ds_oficina,
        :status, 'historico', :qtd_entradas, :participacao, :motivo,
        :usuario_id, timezone('America/Sao_Paulo', now()))
ON CONFLICT (cnpj, cd_multi_empresa) DO UPDATE SET
    cd_fornecedor = EXCLUDED.cd_fornecedor,
    nm_fornecedor = EXCLUDED.nm_fornecedor,
    cd_oficina = EXCLUDED.cd_oficina,
    ds_oficina = EXCLUDED.ds_oficina,
    status = EXCLUDED.status,
    qtd_entradas = EXCLUDED.qtd_entradas,
    participacao = EXCLUDED.participacao,
    motivo = EXCLUDED.motivo,
    usuario_id = EXCLUDED.usuario_id,
    atualizado_em = EXCLUDED.atualizado_em
WHERE api_prontocardio.contas_pagar_regras_oficina.origem <> 'manual'
""")

SQL_UPSERT_MANUAL = text("""
INSERT INTO api_prontocardio.contas_pagar_regras_oficina
    (cnpj, cd_multi_empresa, cd_fornecedor, nm_fornecedor, cd_oficina, ds_oficina, status,
     origem, qtd_entradas, participacao, motivo, usuario_id, atualizado_em)
VALUES (:cnpj, :cd_multi_empresa, :cd_fornecedor, :nm_fornecedor, :cd_oficina, :ds_oficina,
        'definida', 'manual', 0, NULL, :motivo, :usuario_id,
        timezone('America/Sao_Paulo', now()))
ON CONFLICT (cnpj, cd_multi_empresa) DO UPDATE SET
    cd_oficina = EXCLUDED.cd_oficina,
    ds_oficina = EXCLUDED.ds_oficina,
    status = 'definida',
    origem = 'manual',
    motivo = EXCLUDED.motivo,
    usuario_id = EXCLUDED.usuario_id,
    atualizado_em = EXCLUDED.atualizado_em,
    nm_fornecedor = COALESCE(api_prontocardio.contas_pagar_regras_oficina.nm_fornecedor, EXCLUDED.nm_fornecedor)
""")

SQL_TABELA_NFSE = text(
    "SELECT to_regclass('api_prontocardio.nfse_xml_tomador') IS NOT NULL AS existe"
)

SQL_TABELA_SITUACAO = text(
    "SELECT to_regclass('api_prontocardio.nfse_xml_tomador_situacao') IS NOT NULL AS existe"
)

_SQL_NFSE_RECEBIDAS = """
SELECT n.prestador_cnpj, n.prestador_razao_social, n.numero_nfse,
       n.codigo_verificacao_nfse, n.data_hora, n.competencia_nfse,
       n.valor_servicos, n.valor_liquido_nfse, n.tomador_cnpj,
       n.tomador_razao_social, n.discriminacao, n.origem_nfse, n.nfse_manual_id,
       -- O XML só traz o cancelamento se a nota já estava cancelada ao ser baixada.
       CASE WHEN coalesce(n.cancelamento_data_hora, '') <> ''
              OR coalesce(n.motivo_cancelamento_nfse, '') <> ''
            THEN 'CANCELADA' END AS situacao_xml,
       {situacao_portal}
  FROM {fonte} n
  {join_situacao}
 WHERE n.data_hora IS NOT NULL
   AND (n.data_hora AT TIME ZONE 'America/Sao_Paulo')::date >= CAST(:desde AS date)
 ORDER BY n.data_hora DESC, n.numero_nfse
"""

# Situação lida na lista de resultados do portal ISS (DAG extracao_nfse_tomador).
SQL_NFSE_RECEBIDAS = text(_SQL_NFSE_RECEBIDAS.format(
    fonte=FONTE_NFSE_RECEBIDAS,
    situacao_portal='s.situacao AS situacao_portal, s.atualizado_em AS situacao_consultada_em',
    join_situacao="""LEFT JOIN api_prontocardio.nfse_xml_tomador_situacao s
         ON s.prestador_cnpj = regexp_replace(n.prestador_cnpj, '\\D', '', 'g')
        AND s.numero_nfse = n.numero_nfse""",
))

# Enquanto a DAG nova não criar a tabela de situação.
SQL_NFSE_RECEBIDAS_SEM_SITUACAO = text(_SQL_NFSE_RECEBIDAS.format(
    fonte=FONTE_NFSE_RECEBIDAS,
    situacao_portal='NULL AS situacao_portal, NULL AS situacao_consultada_em',
    join_situacao='',
))

SITUACAO_NFSE_NORMAL = 'NORMAL'


def status_nfse(situacao_xml: str | None, situacao_portal: str | None) -> str | None:
    """Situação da NFS-e no ISS: o cancelamento no XML vale mais que a lista."""
    if situacao_xml:
        return situacao_xml
    return (situacao_portal or '').strip().upper() or None


def nfse_cancelada(status: str | None) -> bool:
    return status is not None and status != SITUACAO_NFSE_NORMAL

SQL_OFICINAS_DO_USUARIO = text("""
SELECT cd_oficina FROM api_prontocardio.contas_pagar_usuarios_oficina
 WHERE usuario_id = :usuario_id
""")

SQL_LOGIN_MV_DO_USUARIO = text("""
SELECT cd_usuario_mv FROM api_prontocardio.usuarios_mv WHERE usuario_id = :usuario_id
""")

SQL_NOTAS_ENVIADAS_PELO_USUARIO = text("""
SELECT DISTINCT prestador_cnpj, numero_nfse
  FROM api_prontocardio.contas_pagar_boletos
 WHERE enviado_por = :usuario_id
""")

# Regra usuário -> oficinas do MV: a mesma que o LOV de oficina da
# M_ENTRADA_SERV usa (USUARIOS_OFICINA_FUNCAO, com
# CONFIG_ACMA.SN_USUARIO_OFICINA_FUNCAO = 'S'). TP_FUNCAO 'SS' é o pacote
# padrão de abertura de OS, não a designação para lançar NFS.
SQL_OFICINAS_REGRA_MV = text("""
SELECT DISTINCT o.cd_oficina AS cd_oficina
  FROM dbamv.usuarios_oficina_funcao uof
  JOIN dbamv.oficina o ON o.cd_oficina = uof.cd_oficina
  JOIN dbamv.setor s
    ON s.cd_setor = o.cd_setor AND s.cd_multi_empresa = o.cd_multi_empresa
  JOIN dbamv.config_acma ca
    ON ca.cd_multi_empresa = o.cd_multi_empresa
   AND ca.sn_usuario_oficina_funcao = 'S'
  JOIN dbasgu.usuarios u ON u.cd_usuario = uof.cd_usuario AND u.sn_ativo = 'S'
 WHERE uof.tp_funcao <> 'SS'
   AND uof.cd_usuario = :cd_usuario
""")


# Regra usuário -> oficina do MV de todos os usuários ativos.
SQL_REGRA_MV_TODOS = text("""
SELECT DISTINCT uof.cd_usuario AS cd_usuario, o.cd_oficina AS cd_oficina
  FROM dbamv.usuarios_oficina_funcao uof
  JOIN dbamv.oficina o ON o.cd_oficina = uof.cd_oficina
  JOIN dbamv.setor s
    ON s.cd_setor = o.cd_setor AND s.cd_multi_empresa = o.cd_multi_empresa
  JOIN dbamv.config_acma ca
    ON ca.cd_multi_empresa = o.cd_multi_empresa
   AND ca.sn_usuario_oficina_funcao = 'S'
  JOIN dbasgu.usuarios u ON u.cd_usuario = uof.cd_usuario AND u.sn_ativo = 'S'
 WHERE uof.tp_funcao <> 'SS'
""")

# Histórico de NFS lançadas no MV (entradas de serviço), com quem lançou.
MESES_HISTORICO_SETOR = 24
_SQL_LANCAMENTOS_ENT_SERV = """
SELECT REGEXP_REPLACE(f.nr_cgc_cpf, '[^0-9]', '') AS cnpj,
       f.nm_fornecedor AS nm_fornecedor,
       e.cd_ent_serv AS cd_ent_serv, e.cd_con_pag AS cd_con_pag,
       e.nr_documento AS nr_documento, e.dt_emissao AS dt_emissao,
       e.dt_entrada AS dt_entrada, e.vl_total AS vl_total,
       e.cd_usuario AS cd_usuario, u.nm_usuario AS nm_usuario,
       o.cd_multi_empresa AS cd_multi_empresa
  FROM dbamv.ent_serv e
  JOIN dbamv.fornecedor f ON f.cd_fornecedor = e.cd_fornecedor
  LEFT JOIN dbamv.oficina o ON o.cd_oficina = e.cd_oficina
  LEFT JOIN dbasgu.usuarios u ON u.cd_usuario = e.cd_usuario
 WHERE e.dt_entrada >= ADD_MONTHS(TRUNC(SYSDATE), -:meses)
"""
SQL_LANCAMENTOS_ENT_SERV = text(_SQL_LANCAMENTOS_ENT_SERV)
SQL_LANCAMENTOS_ENT_SERV_DO_FORNECEDOR = text(
    _SQL_LANCAMENTOS_ENT_SERV
    + "   AND REGEXP_REPLACE(f.nr_cgc_cpf, '[^0-9]', '') = :cnpj\n"
)


def escopo_da_fila(session: Session, oracle: Session, usuario) -> EscopoFila | None:
    """Oficinas que o usuário vê na fila (ver services/nfse_setor_escopo).

    None = vê tudo: gestor sem oficina na regra do MV. Quem tem oficina na
    regra do MV vê só ela, qualquer que seja o perfil.
    """
    gestor = _gestor(usuario)
    concedidas: set[int] = set()
    if not gestor:
        concedidas = {
            int(cd)
            for cd in session.execute(
                SQL_OFICINAS_DO_USUARIO, {'usuario_id': usuario.id}
            ).scalars()
        }
    login = session.execute(
        SQL_LOGIN_MV_DO_USUARIO, {'usuario_id': usuario.id}
    ).scalar_one_or_none()
    login = login.strip().upper() if isinstance(login, str) and login.strip() else None
    oficinas_mv: set[int] = set()
    if login:
        oficinas_mv = {
            int(linha['cd_oficina'])
            for linha in read_payables_rows(
                oracle, SQL_OFICINAS_REGRA_MV, {'cd_usuario': login}
            )
        }
    if gestor and not oficinas_mv:
        return None
    regra = (
        oficinas_por_login(read_payables_rows(oracle, SQL_REGRA_MV_TODOS, {}))
        if oficinas_mv
        else {}
    )
    return EscopoFila(frozenset(oficinas_mv | concedidas), regra)


def _historico_mv(oracle: Session) -> list[dict]:
    return [
        dict(linha)
        for linha in read_payables_rows(
            oracle, SQL_LANCAMENTOS_ENT_SERV, {'meses': MESES_HISTORICO_SETOR}
        )
    ]


def _data_iso(valor) -> str | None:
    if valor is None:
        return None
    return (valor.date() if isinstance(valor, datetime) else valor).isoformat()


def _info_lancamento(lancamento: dict | None) -> dict:
    if not lancamento:
        return {}
    return {
        'lancado_por_nome': lancamento.get('nm_usuario') or lancamento.get('cd_usuario'),
        'lancado_em': _data_iso(lancamento.get('dt_entrada')),
    }


def _regras(session: Session) -> dict[tuple[str, int], dict]:
    linhas = session.execute(SQL_REGRAS).mappings().all()
    return {(linha['cnpj'], int(linha['cd_multi_empresa'])): dict(linha) for linha in linhas}


def _decimal(valor) -> Decimal | None:
    if valor in (None, ''):
        return None
    try:
        return Decimal(str(valor).replace(',', '.'))
    except InvalidOperation:
        return None


def _data_emissao(valor) -> str | None:
    """``data_hora`` vem do Postgres como timestamptz; o texto é aceito por segurança."""
    if valor is None or valor == '':
        return None
    if isinstance(valor, datetime):
        if valor.tzinfo is not None:
            valor = valor.astimezone(FUSO_HOSPITAL)
        return valor.date().isoformat()
    return str(valor)[:10] or None


def _inicio_padrao() -> date:
    hoje = datetime.now(FUSO_HOSPITAL).date()
    mes, ano = (hoje.month - 1, hoje.year) if hoje.month > 1 else (12, hoje.year - 1)
    return date(ano, mes, 1)


# ---------------------------------------------------------------- endpoints


@router.get('/oficinas')
def listar_oficinas(_: AcessoFila, oracle: SessionOracle):
    return {'oficinas': list(_oficinas_mv(oracle).values())}


@router.get('/regras')
def listar_regras(
    _: AcessoRegras,
    session: SessionPostgres,
    status: Literal['definida', 'a_classificar', 'todas'] = 'todas',
):
    todas = sorted(
        _regras(session).values(),
        key=lambda r: (r['status'] != STATUS_A_CLASSIFICAR, r['nm_fornecedor'] or ''),
    )
    resumo = {
        STATUS_DEFINIDA: sum(1 for r in todas if r['status'] == STATUS_DEFINIDA),
        STATUS_A_CLASSIFICAR: sum(1 for r in todas if r['status'] == STATUS_A_CLASSIFICAR),
    }
    regras = todas if status == 'todas' else [r for r in todas if r['status'] == status]
    return {'resumo': resumo, 'regras': regras}


@router.post('/regras/recalcular')
def recalcular_regras(
    usuario: AcessoRegras,
    session: SessionPostgres,
    oracle: SessionOracle,
):
    """Recalcula as regras pelo histórico do MV. Regras manuais são mantidas."""
    historico = read_payables_rows(oracle, SQL_HISTORICO, {'meses': MESES_HISTORICO})
    sugeridas = sugerir_regras(historico)
    for regra in sugeridas:
        session.execute(
            SQL_UPSERT_HISTORICO, {**regra.as_dict(), 'usuario_id': usuario.id}
        )
    session.commit()
    return {
        'fornecedores': len(sugeridas),
        STATUS_DEFINIDA: sum(1 for r in sugeridas if r.status == STATUS_DEFINIDA),
        STATUS_A_CLASSIFICAR: sum(1 for r in sugeridas if r.status == STATUS_A_CLASSIFICAR),
        'meses_historico': MESES_HISTORICO,
    }


class ClassificacaoInput(BaseModel):
    cd_oficina: int = Field(gt=0)
    nm_fornecedor: str | None = Field(default=None, max_length=200)
    observacao: str | None = Field(default=None, max_length=500)


@router.put('/regras/{cd_multi_empresa}/{cnpj}')
def classificar_fornecedor(
    cd_multi_empresa: int,
    cnpj: str,
    dados: ClassificacaoInput,
    usuario: AcessoRegras,
    session: SessionPostgres,
    oracle: SessionOracle,
):
    cnpj = somente_digitos(cnpj)
    if len(cnpj) != 14:  # noqa: PLR2004
        raise HTTPException(HTTPStatus.UNPROCESSABLE_ENTITY, 'CNPJ inválido.')
    oficina = _oficinas_mv(oracle).get(dados.cd_oficina)
    if oficina is None:
        raise HTTPException(HTTPStatus.UNPROCESSABLE_ENTITY, 'Oficina não existe no MV.')
    if int(oficina['cd_multi_empresa'] or 0) != cd_multi_empresa:
        raise HTTPException(
            HTTPStatus.UNPROCESSABLE_ENTITY,
            'A oficina escolhida pertence a outra empresa.',
        )
    motivo = f'Classificado manualmente por {usuario.nome}'
    if dados.observacao:
        motivo += f': {dados.observacao}'
    session.execute(
        SQL_UPSERT_MANUAL,
        {
            'cnpj': cnpj,
            'cd_multi_empresa': cd_multi_empresa,
            'cd_fornecedor': None,
            'nm_fornecedor': dados.nm_fornecedor,
            'cd_oficina': dados.cd_oficina,
            'ds_oficina': oficina['ds_oficina'],
            'motivo': motivo,
            'usuario_id': usuario.id,
        },
    )
    session.commit()
    return _regras(session)[(cnpj, cd_multi_empresa)]


def _notas_recebidas(session: Session, inicio: date) -> list:
    sql_notas = (
        SQL_NFSE_RECEBIDAS
        if session.execute(SQL_TABELA_SITUACAO).scalar_one()
        else SQL_NFSE_RECEBIDAS_SEM_SITUACAO
    )
    return session.execute(sql_notas, {'desde': inicio.isoformat()}).mappings().all()


def _situacao_na_fila(mv: dict | None, status: str | None, status_regra: str) -> str:
    """Lançada no MV vale mais que o status do ISS (a tela mostra os dois)."""
    if mv is not None:
        return 'lancada'
    if nfse_cancelada(status):
        return 'cancelada'
    if status_regra == STATUS_A_CLASSIFICAR:
        return 'a_classificar'
    return 'pendente'


@router.get('/recebidas')
def listar_recebidas(  # noqa: PLR0912, PLR0915 -- fila + escopo da oficina; histórico em _itens_do_historico
    usuario: AcessoFila,
    session: SessionPostgres,
    oracle: SessionOracle,
    situacao: Literal['pendente', 'pronta', 'a_classificar', 'lancada', 'cancelada', 'todas'] = 'pendente',
    cd_oficina: int | None = None,
    desde: Annotated[date | None, Query()] = None,
    cd_setor: int | None = None,
):
    """NFS-e recebidas por setor do MV (setor da oficina definida na regra).

    ``pendente``: oficina definida, ainda sem entrada/título no MV e ainda
    não enviada ao financeiro (sem boleto ou com boleto divergente).
    ``a_classificar``: fornecedor sem regra possível (só o financeiro vê).
    ``lancada``: já existe ENT_SERV ou CON_PAG no MV para fornecedor+número.
    ``pronta``: pendente com boleto(s) anexado(s) e sem divergência — os
    títulos que o financeiro pode importar.
    ``cancelada``: cancelada (ou substituída) no ISS e ainda não lançada no MV.

    Quem tem oficina na regra do MV (ou é usuário de setor) vê só as
    oficinas dele (``escopo_da_fila``), com o nome da oficina da regra do
    usuário, e também o histórico de NFS lançadas no MV nos últimos 24 meses
    (``origem_nfse='mv'``), com quem lançou.
    """
    if not session.execute(SQL_TABELA_NFSE).scalar_one():
        return {
            'aviso': 'A extração de NFS-e recebidas ainda não carregou dados.',
            'oficinas': [],
            'itens': [],
        }

    inicio = desde or _inicio_padrao()
    notas = _notas_recebidas(session, inicio)
    regras = _regras(session)
    escopo = escopo_da_fila(session, oracle, usuario)

    cnpjs = [somente_digitos(n['prestador_cnpj']) for n in notas if n['prestador_cnpj']]
    lancadas = _lancadas_mv(oracle, cnpjs) if cnpjs else {}
    empresas = _empresas_mv(oracle)
    oficinas_mv = _oficinas_mv(oracle)
    nomes_empresa = {
        int(e['cd_multi_empresa']): e['ds_multi_empresa'] for e in empresas.values()
    }
    # Import local: contas_pagar_boletos importa este módulo.
    from app_prontocardio.routers.contas_pagar_boletos import (  # noqa: PLC0415
        boletos_por_nota,
    )
    boletos = boletos_por_nota(session, cnpjs)
    importadas_rc = _importadas_pelo_receita_certa(session)
    historico = _historico_mv(oracle)
    lancadores = lancadores_por_fornecedor(historico)
    lancamento_do_documento = {
        (linha['cnpj'], _chave_documento(linha['nr_documento'])): linha for linha in historico
    }
    documentos_da_base = {
        (somente_digitos(n['prestador_cnpj']), _chave_documento(n['numero_nfse'])) for n in notas
    }

    def nomes_da_oficina(oficina: int | None, regra: dict | None):
        """(ds_oficina, cd_setor, nm_setor) exibidos no card."""
        oficina_mv = oficinas_mv.get(oficina) if oficina is not None else None
        if oficina is None:
            return None, None, None
        if escopo is not None:
            # Nome da oficina da regra do usuário (não a do lançamento antigo).
            nome = (oficina_mv or {}).get('ds_oficina') or (regra or {}).get('ds_oficina')
            return nome, (oficina_mv or {}).get('cd_setor'), nome
        ds = (regra or {}).get('ds_oficina')
        return ds, (oficina_mv or {}).get('cd_setor'), ((oficina_mv.get('nm_setor') or ds) if oficina_mv else None)

    itens = []
    for nota in notas:
        cnpj = somente_digitos(nota['prestador_cnpj'])
        empresa = empresas.get(somente_digitos(nota['tomador_cnpj']))
        cd_empresa = int(empresa['cd_multi_empresa']) if empresa else None
        regra = regras.get((cnpj, cd_empresa)) if cd_empresa is not None else None
        status_regra, motivo = situacao_da_nota(regra, cd_empresa)
        mv = lancadas.get((cnpj, _chave_documento(nota['numero_nfse'])))
        status = status_nfse(nota['situacao_xml'], nota['situacao_portal'])
        situacao_nota = _situacao_na_fila(mv, status, status_regra)

        oficina_nota = regra['cd_oficina'] if regra and status_regra == STATUS_DEFINIDA else None
        if escopo is not None:
            oficina_nota = escopo.oficina_do_fornecedor(oficina_nota, lancadores.get(cnpj, ()))
            if oficina_nota is None:
                continue
            if situacao_nota == 'a_classificar':
                situacao_nota = 'pendente'
        if mv is not None:
            mv = {**mv, **_info_lancamento(
                lancamento_do_documento.get((cnpj, _chave_documento(nota['numero_nfse'])))
            )}
        resumo_boletos = boletos.get((cnpj, nota['numero_nfse']))
        pronta = (
            situacao_nota == 'pendente'
            and resumo_boletos is not None
            and not resumo_boletos['com_divergencia']
        )
        if situacao == 'pronta':
            if not pronta:
                continue
        elif situacao not in ('todas', situacao_nota) or (situacao == 'pendente' and pronta):
            # Enviada ao financeiro (boleto sem divergência) sai de "A lançar":
            # fica só em "Prontas para o financeiro".
            continue
        if cd_oficina is not None and oficina_nota != cd_oficina:
            continue
        ds_oficina, setor_nota, nm_setor = nomes_da_oficina(oficina_nota, regra)
        if cd_setor is not None and setor_nota != cd_setor:
            continue

        discriminacao = (nota['discriminacao'] or '').strip()
        itens.append({
            'chave': f'{cnpj}-{nota["numero_nfse"]}',
            'prestador_cnpj': cnpj,
            'prestador': nota['prestador_razao_social'],
            'numero_nfse': nota['numero_nfse'],
            'codigo_verificacao': nota['codigo_verificacao_nfse'],
            'data_emissao': _data_emissao(nota['data_hora']),
            'competencia': nota['competencia_nfse'],
            'valor_servicos': _decimal(nota['valor_servicos']),
            'valor_liquido': _decimal(nota['valor_liquido_nfse']),
            'tomador_cnpj': somente_digitos(nota['tomador_cnpj']),
            'tomador': nota['tomador_razao_social'],
            'cd_multi_empresa': cd_empresa,
            'empresa': nomes_empresa.get(cd_empresa),
            'discriminacao': discriminacao[:TAMANHO_DISCRIMINACAO],
            'cd_oficina': oficina_nota,
            'ds_oficina': ds_oficina,
            'cd_setor': setor_nota,
            'nm_setor': nm_setor,
            'situacao': situacao_nota,
            'status_nfse': status,
            'origem_nfse': nota.get('origem_nfse') or 'iss',
            'nfse_manual_id': nota.get('nfse_manual_id'),
            'status_nfse_consultado_em': nota['situacao_consultada_em'],
            'motivo': motivo if situacao_nota == 'a_classificar' else None,
            'mv': mv,
            'boletos': {
                'quantidade': int(resumo_boletos['quantidade']),
                'soma': resumo_boletos['soma'],
                'primeiro_vencimento': resumo_boletos['primeiro_vencimento'],
                'com_divergencia': bool(resumo_boletos['com_divergencia']),
                'fora_do_prazo': bool(resumo_boletos.get('fora_do_prazo')),
                'ids': list(resumo_boletos.get('ids') or []),
                'enviado_por': [u for u in (resumo_boletos.get('enviado_por') or []) if u is not None],
            } if resumo_boletos else None,
            'pronta': pronta,
            'lancada_pelo_receita_certa': (cnpj, nota['numero_nfse']) in importadas_rc,
        })

    if escopo is not None and situacao in ('lancada', 'todas'):
        itens.extend(_itens_do_historico(
            historico, documentos_da_base, escopo, regras, lancadores, nomes_da_oficina,
            nomes_empresa, FiltroFila(desde, cd_oficina, cd_setor),
        ))
    # Mais recentes primeiro e, acima de tudo, o que o setor ainda precisa
    # fazer (falta boleto). sort é estável: a 2ª ordenação mantém a 1ª.
    itens.sort(key=lambda item: item['data_emissao'] or '', reverse=True)
    itens.sort(key=prioridade_na_fila)

    por_oficina: dict[tuple, dict] = {}
    for item in itens:
        por_oficina.setdefault(_chave_grupo(item), _novo_grupo(item))
        grupo = por_oficina[_chave_grupo(item)]
        grupo['quantidade'] += 1
        grupo['valor'] += item['valor_servicos'] or Decimal('0')
        if item['ds_oficina']:
            grupo['oficinas'].add(item['ds_oficina'])

    return {
        'desde': inicio,
        'situacao': situacao,
        'oficinas': sorted(
            (
                {**grupo, 'oficinas': sorted(grupo['oficinas'])}
                for grupo in por_oficina.values()
            ),
            key=lambda g: (g['cd_oficina'] is not None, g['nm_setor'], g['empresa'] or ''),
        ),
        'itens': itens,
    }


def prioridade_na_fila(item: dict) -> int:
    """0 = a lançar · falta boleto; 1 = boleto divergente; 2 = pronta para o
    financeiro; 3 = sem setor/cancelada; 4 = já lançada no MV."""
    boletos = item.get('boletos') or {}
    if item['situacao'] == 'pendente':
        if not boletos:
            return 0
        return 2 if item.get('pronta') else 1
    if item['situacao'] in ('a_classificar', 'cancelada'):
        return 3
    return 4


@dataclass(frozen=True)
class FiltroFila:
    desde: date | None
    cd_oficina: int | None
    cd_setor: int | None


SQL_IMPORTADAS_RC = text("""
SELECT prestador_cnpj, numero_nfse
  FROM api_prontocardio.contas_pagar_importacoes
 WHERE status = 'importada'
""")


def _importadas_pelo_receita_certa(session: Session) -> set[tuple[str, str]]:
    """Notas lançadas no MV pela importação do Receita Certa (não direto no MV).

    Só elas mostram PDF da NFS-e e boleto entre as lançadas.
    """
    existe = session.execute(
        text("SELECT to_regclass('api_prontocardio.contas_pagar_importacoes') IS NOT NULL")
    ).scalar_one_or_none()
    if not existe:
        return set()
    return {
        (linha['prestador_cnpj'], linha['numero_nfse'])
        for linha in session.execute(SQL_IMPORTADAS_RC).mappings().all()
    }


def _itens_do_historico(  # noqa: PLR0913
    historico, documentos_da_base, escopo, regras, lancadores, nomes_da_oficina,
    nomes_empresa, filtro: FiltroFila,
) -> list[dict]:
    """NFS lançadas no MV (24 meses) dos fornecedores da oficina do usuário,
    que não estão na base de NFS-e recebidas."""
    itens = []
    ja_listados = set(documentos_da_base)
    for linha in historico:
        documento = (linha['cnpj'], _chave_documento(linha['nr_documento']))
        entrada = _data_iso(linha['dt_entrada'])
        if documento in ja_listados or (filtro.desde and entrada and entrada < filtro.desde.isoformat()):
            continue
        cd_empresa = int(linha['cd_multi_empresa']) if linha['cd_multi_empresa'] is not None else None
        regra = regras.get((linha['cnpj'], cd_empresa)) if cd_empresa is not None else None
        oficina_regra = regra['cd_oficina'] if regra and regra['status'] == STATUS_DEFINIDA else None
        oficina = escopo.oficina_do_fornecedor(oficina_regra, lancadores.get(linha['cnpj'], ()))
        if oficina is None or filtro.cd_oficina not in (None, oficina):
            continue
        ds_oficina, setor, nm_setor = nomes_da_oficina(oficina, regra)
        if filtro.cd_setor not in (None, setor):
            continue
        ja_listados.add(documento)
        itens.append(_item_historico_mv(
            linha, oficina, ds_oficina, setor, nm_setor, cd_empresa, nomes_empresa.get(cd_empresa)
        ))
    return itens


def _item_historico_mv(linha, oficina, ds_oficina, cd_setor, nm_setor, cd_empresa, empresa) -> dict:
    """NFS lançada no MV antes da extração das NFS-e recebidas (só ENT_SERV)."""
    numero = (linha['nr_documento'] or '').strip()
    return {
        'chave': f"{linha['cnpj']}-{numero}",
        'prestador_cnpj': linha['cnpj'],
        'prestador': linha['nm_fornecedor'],
        'numero_nfse': numero,
        'codigo_verificacao': None,
        'data_emissao': _data_iso(linha['dt_emissao'] or linha['dt_entrada']),
        'competencia': None,
        'valor_servicos': _decimal(linha['vl_total']),
        'valor_liquido': None,
        'tomador_cnpj': None,
        'tomador': None,
        'cd_multi_empresa': cd_empresa,
        'empresa': empresa,
        'discriminacao': '',
        'cd_oficina': oficina,
        'ds_oficina': ds_oficina,
        'cd_setor': cd_setor,
        'nm_setor': nm_setor,
        'situacao': 'lancada',
        'status_nfse': None,
        'origem_nfse': 'mv',
        'nfse_manual_id': None,
        'status_nfse_consultado_em': None,
        'motivo': None,
        'mv': {
            'cd_ent_serv': linha['cd_ent_serv'],
            'cd_con_pag': linha['cd_con_pag'],
            'lancado_por': [(linha['cd_usuario'] or '').strip().upper()] if linha['cd_usuario'] else [],
            **_info_lancamento(linha),
        },
        'boletos': None,
        'pronta': False,
        'lancada_pelo_receita_certa': False,
    }


GRUPO_A_CLASSIFICAR = 'A classificar'
GRUPO_SEM_SETOR_LANCADA = 'Sem setor (já lançada no MV)'
GRUPO_SEM_SETOR_CANCELADA = 'Sem setor (cancelada no ISS)'
_ROTULO_SEM_SETOR = {
    'a_classificar': GRUPO_A_CLASSIFICAR,
    'cancelada': GRUPO_SEM_SETOR_CANCELADA,
}


def _chave_grupo(item: dict) -> tuple:
    if item['cd_setor'] is not None:
        return ('setor', item['cd_setor'])
    if item['cd_oficina'] is not None:
        return ('oficina', item['cd_oficina'])
    return ('sem_setor', _ROTULO_SEM_SETOR.get(item['situacao'], GRUPO_SEM_SETOR_LANCADA))


def _novo_grupo(item: dict) -> dict:
    """Grupo por setor do MV; o setor já é de uma empresa (cada empresa tem os próprios setores).

    Oficinas do mesmo setor ficam no mesmo grupo (ex.: Obras e Manutenção e
    Infraestrutura em Manutenção Predial). ``cd_oficina``/``ds_oficina`` são
    mantidos para clientes antigos: a primeira oficina do grupo e o nome do setor.
    """
    if item['cd_oficina'] is None:
        rotulo = _ROTULO_SEM_SETOR.get(item['situacao'], GRUPO_SEM_SETOR_LANCADA)
        return {'cd_setor': None, 'nm_setor': rotulo, 'cd_oficina': None, 'ds_oficina': rotulo,
                'oficinas': set(), 'cd_multi_empresa': None, 'empresa': None,
                'quantidade': 0, 'valor': Decimal('0')}
    nome = item['nm_setor'] or item['ds_oficina']
    return {'cd_setor': item['cd_setor'], 'nm_setor': nome,
            'cd_oficina': item['cd_oficina'], 'ds_oficina': nome, 'oficinas': set(),
            'cd_multi_empresa': item['cd_multi_empresa'], 'empresa': item['empresa'],
            'quantidade': 0, 'valor': Decimal('0')}


# ---------------------------------------------------------------- vínculos


class OficinasUsuarioInput(BaseModel):
    oficinas: list[int] = Field(default_factory=list, max_length=50)


@router.get('/usuarios/{usuario_id}/oficinas')
def oficinas_do_usuario(usuario_id: int, _: AcessoRegras, session: SessionPostgres):
    linhas = session.execute(
        text("""
        SELECT cd_oficina, ds_oficina, criado_em
          FROM api_prontocardio.contas_pagar_usuarios_oficina
         WHERE usuario_id = :usuario_id ORDER BY ds_oficina
        """),
        {'usuario_id': usuario_id},
    ).mappings().all()
    return {'usuario_id': usuario_id, 'oficinas': [dict(linha) for linha in linhas]}


@router.put('/usuarios/{usuario_id}/oficinas')
def definir_oficinas_do_usuario(
    usuario_id: int,
    dados: OficinasUsuarioInput,
    gestor: AcessoRegras,
    session: SessionPostgres,
    oracle: SessionOracle,
):
    oficinas = _oficinas_mv(oracle)
    invalidas = sorted(set(dados.oficinas) - set(oficinas))
    if invalidas:
        raise HTTPException(
            HTTPStatus.UNPROCESSABLE_ENTITY,
            f'Oficinas inexistentes no MV: {invalidas}',
        )
    session.execute(
        text('DELETE FROM api_prontocardio.contas_pagar_usuarios_oficina WHERE usuario_id = :usuario_id'),
        {'usuario_id': usuario_id},
    )
    for cd in sorted(set(dados.oficinas)):
        session.execute(
            text("""
            INSERT INTO api_prontocardio.contas_pagar_usuarios_oficina
                (usuario_id, cd_oficina, ds_oficina, concedido_por)
            VALUES (:usuario_id, :cd_oficina, :ds_oficina, :concedido_por)
            """),
            {
                'usuario_id': usuario_id,
                'cd_oficina': cd,
                'ds_oficina': oficinas[cd]['ds_oficina'],
                'concedido_por': gestor.id,
            },
        )
    session.commit()
    return oficinas_do_usuario(usuario_id, gestor, session)


class UsuarioMvInput(BaseModel):
    cd_usuario_mv: str = Field(min_length=2, max_length=30, pattern=r'^[A-Za-z0-9_$#]+$')


SQL_USUARIO_MV = text("""
SELECT u.cd_usuario AS cd_usuario, u.nm_usuario AS nm_usuario, u.sn_ativo AS sn_ativo
  FROM dbasgu.usuarios u
 WHERE u.cd_usuario = :cd_usuario
""")


@router.get('/usuarios/{usuario_id}/mv')
def usuario_mv(usuario_id: int, _: AcessoRegras, session: SessionPostgres):
    linha = session.execute(
        text("""
        SELECT usuario_id, cd_usuario_mv, nm_usuario_mv, vinculado_por, vinculado_em
          FROM api_prontocardio.usuarios_mv WHERE usuario_id = :usuario_id
        """),
        {'usuario_id': usuario_id},
    ).mappings().first()
    return dict(linha) if linha else {'usuario_id': usuario_id, 'cd_usuario_mv': None}


@router.put('/usuarios/{usuario_id}/mv')
def vincular_usuario_mv(
    usuario_id: int,
    dados: UsuarioMvInput,
    gestor: AcessoRegras,
    session: SessionPostgres,
    oracle: SessionOracle,
):
    """Vincula o usuário do Receita Certa ao seu login MV (= usuário Oracle).

    As gravações no MV feitas por esse usuário serão registradas com o login MV.
    """
    cd_usuario = dados.cd_usuario_mv.upper()
    mv = read_payables_rows(oracle, SQL_USUARIO_MV, {'cd_usuario': cd_usuario})
    if not mv:
        raise HTTPException(HTTPStatus.UNPROCESSABLE_ENTITY, 'Usuário não existe no MV.')
    if (mv[0]['sn_ativo'] or 'S') != 'S':
        raise HTTPException(HTTPStatus.UNPROCESSABLE_ENTITY, 'Usuário MV inativo.')
    em_uso = session.execute(
        text("""
        SELECT usuario_id FROM api_prontocardio.usuarios_mv
         WHERE cd_usuario_mv = :cd AND usuario_id <> :usuario_id
        """),
        {'cd': cd_usuario, 'usuario_id': usuario_id},
    ).scalar_one_or_none()
    if em_uso is not None:
        raise HTTPException(
            HTTPStatus.CONFLICT,
            f'Login MV {cd_usuario} já vinculado a outro usuário do Receita Certa.',
        )
    session.execute(
        text("""
        INSERT INTO api_prontocardio.usuarios_mv
            (usuario_id, cd_usuario_mv, nm_usuario_mv, vinculado_por)
        VALUES (:usuario_id, :cd, :nome, :gestor)
        ON CONFLICT (usuario_id) DO UPDATE SET
            cd_usuario_mv = EXCLUDED.cd_usuario_mv,
            nm_usuario_mv = EXCLUDED.nm_usuario_mv,
            vinculado_por = EXCLUDED.vinculado_por,
            vinculado_em = timezone('America/Sao_Paulo', now())
        """),
        {'usuario_id': usuario_id, 'cd': cd_usuario, 'nome': mv[0]['nm_usuario'], 'gestor': gestor.id},
    )
    session.commit()
    return usuario_mv(usuario_id, gestor, session)
