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

SQL_OFICINAS = text("""
SELECT o.cd_oficina AS cd_oficina, o.ds_oficina AS ds_oficina,
       o.cd_setor AS cd_setor, o.cd_multi_empresa AS cd_multi_empresa
  FROM dbamv.oficina o
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
            atual = lancadas.setdefault(chave, {'cd_ent_serv': None, 'cd_con_pag': None})
            atual['cd_ent_serv'] = atual['cd_ent_serv'] or linha['cd_ent_serv']
            atual['cd_con_pag'] = atual['cd_con_pag'] or linha['cd_con_pag']
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

SQL_NFSE_RECEBIDAS = text("""
SELECT prestador_cnpj, prestador_razao_social, numero_nfse,
       codigo_verificacao_nfse, data_hora, competencia_nfse,
       valor_servicos, valor_liquido_nfse, tomador_cnpj,
       tomador_razao_social, discriminacao
  FROM api_prontocardio.nfse_xml_tomador
 WHERE data_hora IS NOT NULL
   AND (data_hora AT TIME ZONE 'America/Sao_Paulo')::date >= CAST(:desde AS date)
   AND coalesce(cancelamento_data_hora, '') = ''
   AND coalesce(motivo_cancelamento_nfse, '') = ''
 ORDER BY data_hora DESC, numero_nfse
""")

SQL_OFICINAS_DO_USUARIO = text("""
SELECT cd_oficina FROM api_prontocardio.contas_pagar_usuarios_oficina
 WHERE usuario_id = :usuario_id
""")


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


@router.get('/recebidas')
def listar_recebidas(
    usuario: AcessoFila,
    session: SessionPostgres,
    oracle: SessionOracle,
    situacao: Literal['pendente', 'a_classificar', 'lancada', 'todas'] = 'pendente',
    cd_oficina: int | None = None,
    desde: Annotated[date | None, Query()] = None,
):
    """NFS-e recebidas por oficina.

    ``pendente``: oficina definida e ainda sem entrada/título no MV.
    ``a_classificar``: fornecedor sem regra possível (só o financeiro vê).
    ``lancada``: já existe ENT_SERV ou CON_PAG no MV para fornecedor+número.
    """
    if not session.execute(SQL_TABELA_NFSE).scalar_one():
        return {
            'aviso': 'A extração de NFS-e recebidas ainda não carregou dados.',
            'oficinas': [],
            'itens': [],
        }

    inicio = desde or _inicio_padrao()
    notas = session.execute(
        SQL_NFSE_RECEBIDAS, {'desde': inicio.isoformat()}
    ).mappings().all()
    regras = _regras(session)
    gestor = _gestor(usuario)
    permitidas: set[int] | None = None
    if not gestor:
        permitidas = {
            int(cd)
            for cd in session.execute(
                SQL_OFICINAS_DO_USUARIO, {'usuario_id': usuario.id}
            ).scalars()
        }

    cnpjs = [somente_digitos(n['prestador_cnpj']) for n in notas if n['prestador_cnpj']]
    lancadas = _lancadas_mv(oracle, cnpjs) if cnpjs else {}
    empresas = _empresas_mv(oracle)

    itens = []
    for nota in notas:
        cnpj = somente_digitos(nota['prestador_cnpj'])
        empresa = empresas.get(somente_digitos(nota['tomador_cnpj']))
        cd_empresa = int(empresa['cd_multi_empresa']) if empresa else None
        regra = regras.get((cnpj, cd_empresa)) if cd_empresa is not None else None
        status_regra, motivo = situacao_da_nota(regra, cd_empresa)
        mv = lancadas.get((cnpj, _chave_documento(nota['numero_nfse'])))
        if mv is not None:
            situacao_nota = 'lancada'
        elif status_regra == STATUS_A_CLASSIFICAR:
            situacao_nota = 'a_classificar'
        else:
            situacao_nota = 'pendente'

        oficina_nota = regra['cd_oficina'] if regra and status_regra == STATUS_DEFINIDA else None
        if permitidas is not None and oficina_nota not in permitidas:
            continue
        if situacao not in ('todas', situacao_nota):
            continue
        if cd_oficina is not None and oficina_nota != cd_oficina:
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
            'discriminacao': discriminacao[:TAMANHO_DISCRIMINACAO],
            'cd_oficina': oficina_nota,
            'ds_oficina': regra['ds_oficina'] if oficina_nota else None,
            'situacao': situacao_nota,
            'motivo': motivo if situacao_nota == 'a_classificar' else None,
            'mv': mv,
        })

    por_oficina: dict[int | None, dict] = {}
    for item in itens:
        grupo = por_oficina.setdefault(
            item['cd_oficina'],
            {'cd_oficina': item['cd_oficina'], 'ds_oficina': item['ds_oficina'] or 'A classificar',
             'quantidade': 0, 'valor': Decimal('0')},
        )
        grupo['quantidade'] += 1
        grupo['valor'] += item['valor_servicos'] or Decimal('0')

    return {
        'desde': inicio,
        'situacao': situacao,
        'oficinas': sorted(por_oficina.values(), key=lambda g: (g['cd_oficina'] is not None, g['ds_oficina'])),
        'itens': itens,
    }


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
