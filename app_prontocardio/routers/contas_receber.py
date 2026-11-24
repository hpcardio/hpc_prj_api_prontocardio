# ruff: noqa: E501, PLR0913
"""Contas a Receber: acompanhamento remessa -> protocolo -> NFS -> recebimento.

Entrega 1 (só leitura): as remessas fechadas do MV, o protocolo de cada uma
(vínculo gravado ou casamento automático com o portal do convênio) e o funil
com quantidade e valor parados em cada etapa. Os vínculos manuais, a
importação do histórico e o recebimento pelo extrato vêm nas próximas
entregas, sobre as tabelas ``cr_*`` (docs/oracle/contas_receber_fluxo.sql).
"""

from datetime import date, datetime
from http import HTTPStatus
from math import ceil
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text

from app_prontocardio.models import Usuario
from app_prontocardio.panel_cache import CacheBusyError, SingleFlightTTLCache
from app_prontocardio.routers.contas_pagar_nfse import (
    SessionOracle,
    SessionPostgres,
    UsuarioAtual,
)
from app_prontocardio.services.contas_receber_fluxo import (
    CONCLUIDO,
    ETAPAS,
    ProcessoPortal,
    casar_portal,
    dinheiro,
    montar_item,
    resumo_funil,
)

router = APIRouter(
    prefix='/app_glosas/financeiro/contas-a-receber',
    tags=['contas-a-receber'],
)

FUSO_HOSPITAL = ZoneInfo('America/Sao_Paulo')
TELA_FLUXO = 'contas_receber_fluxo'
PERFIS_GESTAO = {'ti', 'administrador'}
COMPETENCIA_INICIAL = date(2025, 1, 1)
# Particular não passa por protocolo nem NFS de convênio.
CONVENIOS_FORA = (3,)
CD_CONVENIO_ISSEC = 46


def acesso_fluxo(usuario: UsuarioAtual) -> Usuario:
    if usuario.perfil in PERFIS_GESTAO or TELA_FLUXO in (
        usuario.telas_permitidas or []
    ):
        return usuario
    raise HTTPException(
        HTTPStatus.FORBIDDEN, 'Sem acesso ao fluxo do Contas a Receber.'
    )


AcessoFluxo = Annotated[Usuario, Depends(acesso_fluxo)]


# ---------------------------------------------------------------- MV

# Valor da remessa = soma das contas (REG_FAT + REG_AMB). O MV não preenche
# REMESSA_FATURA.VL_REMESSA_FNFI nas remessas de convênio.
SQL_REMESSAS_MV = text(f"""
WITH contas AS (
  SELECT cd_remessa, SUM(vl) AS vl, COUNT(*) AS qt FROM (
    SELECT rf.cd_remessa, rf.vl_total_conta AS vl FROM dbamv.reg_fat rf WHERE rf.cd_remessa IS NOT NULL
    UNION ALL
    SELECT rb.cd_remessa, rb.vl_total_conta FROM dbamv.reg_amb rb WHERE rb.cd_remessa IS NOT NULL)
  GROUP BY cd_remessa)
SELECT r.cd_remessa AS cd_remessa, f.cd_convenio AS cd_convenio, c.nm_convenio AS convenio,
       f.dt_competencia AS dt_competencia, f.cd_multi_empresa AS cd_multi_empresa,
       r.dt_fechamento AS dt_fechamento, ct.vl AS valor, ct.qt AS qt_contas,
       r.dt_entrega_da_fatura AS dt_entrega, r.dt_prevista_para_pgto AS dt_prevista_pgto
  FROM dbamv.remessa_fatura r
  JOIN dbamv.fatura f ON f.cd_fatura = r.cd_fatura
  JOIN dbamv.convenio c ON c.cd_convenio = f.cd_convenio
  LEFT JOIN contas ct ON ct.cd_remessa = r.cd_remessa
 WHERE r.sn_fechada = 'S'
   AND f.dt_competencia >= :competencia_inicial
   AND f.cd_convenio NOT IN ({', '.join(str(c) for c in CONVENIOS_FORA)})
""")

_cache_remessas: SingleFlightTTLCache[str, list[dict]] = SingleFlightTTLCache(
    ttl_seconds=300, stale_seconds=1800, wait_seconds=40
)


def _como_data(valor):
    if isinstance(valor, datetime):
        return valor.date()
    return valor


def _carregar_remessas(oracle) -> list[dict]:
    linhas = (
        oracle
        .execute(SQL_REMESSAS_MV, {'competencia_inicial': COMPETENCIA_INICIAL})
        .mappings()
        .all()
    )
    remessas = []
    for linha in linhas:
        competencia = _como_data(linha['dt_competencia'])
        remessas.append({
            'cd_remessa': int(linha['cd_remessa']),
            'cd_convenio': int(linha['cd_convenio']),
            'convenio': (linha['convenio'] or '').strip(),
            'competencia': competencia.replace(day=1) if competencia else None,
            'cd_multi_empresa': linha['cd_multi_empresa'],
            'dt_fechamento': _como_data(linha['dt_fechamento']),
            'dt_entrega': _como_data(linha['dt_entrega']),
            'dt_prevista_pgto': _como_data(linha['dt_prevista_pgto']),
            'valor': dinheiro(linha['valor']),
            'qt_contas': int(linha['qt_contas'] or 0),
        })
    return remessas


def remessas_mv(oracle) -> list[dict]:
    try:
        return _cache_remessas.get_or_load(
            'remessas', lambda: _carregar_remessas(oracle)
        )
    except CacheBusyError as exc:
        raise HTTPException(
            HTTPStatus.SERVICE_UNAVAILABLE,
            'As remessas do MV ainda estão carregando. Tente em instantes.',
        ) from exc


# ---------------------------------------------------------------- Postgres

# Campos do detalhe do processo (DAG grava tudo em campos_detalhe, JSON):
# "Entrada no Protocolo:" (dd/mm/aaaa) é a data do protocolo no convênio.
_PAR = "(CASE WHEN {t}.campos_detalhe IS NOT NULL THEN {t}.campos_detalhe::jsonb -> 'pares' END)"


def _entrada_portal(t: str) -> str:
    par = _PAR.format(t=t)
    return (
        f"CASE WHEN {par} ->> 'Entrada no Protocolo:' ~ '^\\d{{2}}/\\d{{2}}/\\d{{4}}$' "
        f"THEN TO_DATE({par} ->> 'Entrada no Protocolo:', 'DD/MM/YYYY') END"
    )


def _situacao_portal(t: str) -> str:
    return f"NULLIF({_PAR.format(t=t)} ->> 'Situação:', '')"


# Protocolos extraídos dos portais dos convênios, por código do convênio no MV.
SQL_PORTAL = {
    CD_CONVENIO_ISSEC: text(f"""
SELECT DISTINCT ON (processo) processo, mes_producao, valor_cobrado, valor_liberado,
       {_entrada_portal('processos')} AS data_entrada,
       {_situacao_portal('processos')} AS situacao
  FROM raw_issec_portal.processos
 WHERE processo IS NOT NULL AND mes_producao IS NOT NULL
 ORDER BY processo, extraido_em DESC
"""),
}

# Valores do portal lidos na hora (o protocolo gravado guarda só o vínculo):
# o portal é atualizado a cada execução da DAG.
SQL_VINCULOS = text(f"""
SELECT pr.cd_remessa, p.id AS protocolo_id, p.cd_convenio, p.numero, p.origem,
       COALESCE(pt.valor_cobrado, p.valor_cobrado) AS valor_cobrado,
       COALESCE(pt.valor_liberado, p.valor_liberado) AS valor_liberado,
       COALESCE(p.data_protocolo, pt.data_entrada) AS data_protocolo,
       pt.situacao AS situacao_portal,
       p.solicitacao_nota_em, p.solicitacao_nota_obs,
       n.numero_nfse, n.prestador_cnpj, n.data_emissao, n.valor AS valor_nfse,
       n.origem AS origem_nfse, x.valor_liquido AS valor_liquido_nfse
  FROM api_prontocardio.cr_protocolo_remessas pr
  JOIN api_prontocardio.cr_protocolos p ON p.id = pr.protocolo_id
  LEFT JOIN api_prontocardio.cr_protocolo_nfse n ON n.protocolo_id = p.id
  -- Valor líquido da NFS no ISS (quando a nota está na base extraída).
  LEFT JOIN LATERAL (
    SELECT NULLIF(nx.valor_liquido_nfse, '')::numeric AS valor_liquido
      FROM api_prontocardio.nfse_xml nx
     WHERE nx.numero_nfse = n.numero_nfse
       AND nx.prestador_cnpj = n.prestador_cnpj
     ORDER BY nx.data_hora DESC
     LIMIT 1
  ) x ON n.prestador_cnpj IS NOT NULL
  LEFT JOIN LATERAL (
    SELECT ip.valor_cobrado, ip.valor_liberado,
           {_entrada_portal('ip')} AS data_entrada,
           {_situacao_portal('ip')} AS situacao
      FROM raw_issec_portal.processos ip
     WHERE ip.processo = p.numero
     ORDER BY ip.extraido_em DESC
     LIMIT 1
  ) pt ON p.cd_convenio = {CD_CONVENIO_ISSEC}
""")

SQL_RECEBIMENTOS = text("""
SELECT rn.numero_nfse, rn.prestador_cnpj, rn.valor_alocado,
       r.id, r.data, r.valor, r.historico, r.origem
  FROM api_prontocardio.cr_recebimento_nfse rn
  JOIN api_prontocardio.cr_recebimentos r ON r.id = rn.recebimento_id
""")


def processos_portal(postgres) -> dict[int, list[ProcessoPortal]]:
    resultado = {}
    for cd_convenio, sql in SQL_PORTAL.items():
        resultado[cd_convenio] = [
            ProcessoPortal(
                numero=str(linha['processo']).strip(),
                competencia=_como_data(linha['mes_producao']).replace(day=1),
                valor_cobrado=dinheiro(linha['valor_cobrado']),
                valor_liberado=(
                    dinheiro(linha['valor_liberado'])
                    if linha['valor_liberado'] is not None
                    else None
                ),
                data_entrada=_como_data(linha['data_entrada']),
                situacao=linha['situacao'],
            )
            for linha in postgres.execute(sql).mappings().all()
        ]
    return resultado


def vinculos_gravados(postgres) -> dict[int, dict]:
    recebimentos = {}
    for linha in postgres.execute(SQL_RECEBIMENTOS).mappings().all():
        chave = (linha['numero_nfse'], linha['prestador_cnpj'] or '')
        recebimentos.setdefault(chave, []).append({
            'id': linha['id'],
            'data': linha['data'],
            'valor': dinheiro(linha['valor']),
            'valor_alocado': (
                dinheiro(linha['valor_alocado'])
                if linha['valor_alocado'] is not None
                else None
            ),
            'historico': linha['historico'],
            'origem': linha['origem'],
        })
    vinculos = {}
    for linha in postgres.execute(SQL_VINCULOS).mappings().all():
        nfse = None
        if linha['numero_nfse']:
            nfse = {
                'numero': linha['numero_nfse'],
                'prestador_cnpj': linha['prestador_cnpj'],
                'data_emissao': linha['data_emissao'],
                'valor': (
                    dinheiro(linha['valor_nfse'])
                    if linha['valor_nfse'] is not None
                    else None
                ),
                'origem': linha['origem_nfse'],
                'valor_liquido': (
                    dinheiro(linha['valor_liquido_nfse'])
                    if linha['valor_liquido_nfse'] is not None
                    else None
                ),
            }
        vinculos[int(linha['cd_remessa'])] = {
            'protocolo': {
                'id': linha['protocolo_id'],
                'numero': linha['numero'],
                'origem': linha['origem'],
                'situacao': 'vinculado',
                'valor_cobrado': linha['valor_cobrado'],
                'valor_liberado': linha['valor_liberado'],
                'data_protocolo': _como_data(linha['data_protocolo']),
                'situacao_portal': linha['situacao_portal'],
                'solicitacao_nota_em': linha['solicitacao_nota_em'],
                'solicitacao_nota_obs': linha['solicitacao_nota_obs'],
            },
            'nfse': nfse,
            'recebimentos': (
                recebimentos.get(
                    (linha['numero_nfse'], linha['prestador_cnpj'] or ''), []
                )
                if nfse
                else []
            ),
        }
    return vinculos


# ---------------------------------------------------------------- fluxo


def _hoje() -> date:
    return datetime.now(FUSO_HOSPITAL).date()


def _competencia(valor: str | None, campo: str) -> date | None:
    if not valor:
        return None
    try:
        return datetime.strptime(valor[:7], '%Y-%m').date()
    except ValueError as exc:
        raise HTTPException(
            HTTPStatus.UNPROCESSABLE_ENTITY, f'{campo} inválida (use AAAA-MM).'
        ) from exc


def montar_fluxo(remessas, portal, vinculos, hoje):
    """Todos os itens do fluxo (sem filtro)."""
    numeros_vinculados = {
        (v['protocolo']['numero']) for v in vinculos.values()
    }
    sugestoes = {}
    for cd_convenio, processos in portal.items():
        sugestoes.update(
            casar_portal(
                [r for r in remessas if r['cd_convenio'] == cd_convenio],
                processos,
                set(vinculos),
                numeros_vinculados,
            )
        )
    return [
        montar_item(
            r,
            vinculos.get(r['cd_remessa']),
            sugestoes.get(r['cd_remessa']),
            hoje,
        )
        for r in remessas
    ]


def _bate_busca(item, termo: str) -> bool:
    termo = termo.strip().lower()
    alvos = [str(item['cd_remessa']), item['convenio'].lower()]
    if item['protocolo']:
        alvos.append(str(item['protocolo']['numero']).lower())
    alvos += [str(c['numero']) for c in item['protocolo_candidatos']]
    if item['nfse']:
        alvos.append(str(item['nfse']['numero']))
    return any(termo in alvo for alvo in alvos)


def filtrar(
    itens,
    *,
    convenio=None,
    competencia_de=None,
    competencia_ate=None,
    q=None,
    a_confirmar=False,
):
    resultado = []
    for item in itens:
        if convenio and item['cd_convenio'] != convenio:
            continue
        if (
            competencia_de
            and (item['competencia'] or date.min) < competencia_de
        ):
            continue
        if (
            competencia_ate
            and (item['competencia'] or date.max) > competencia_ate
        ):
            continue
        if a_confirmar and not item['protocolo_candidatos']:
            continue
        if q and not _bate_busca(item, q):
            continue
        resultado.append(item)
    return resultado


def ordenar(itens, criterio: str):
    if criterio == 'competencia':
        return sorted(
            itens,
            key=lambda i: (i['competencia'] or date.min, i['cd_remessa']),
            reverse=True,
        )
    if criterio == 'valor':
        return sorted(
            itens, key=lambda i: (i['valor'], i['cd_remessa']), reverse=True
        )
    # Padrão: mais tempo parado primeiro; concluídos por último.
    return sorted(
        itens,
        key=lambda i: (
            i['etapa_atual'] == CONCLUIDO,
            -(i['dias_na_etapa'] if i['dias_na_etapa'] is not None else -1),
            -i['cd_remessa'],
        ),
    )


def _opcoes_convenios(itens):
    contagem = {}
    for item in itens:
        atual = contagem.setdefault(
            item['cd_convenio'],
            {
                'codigo': item['cd_convenio'],
                'nome': item['convenio'],
                'remessas': 0,
            },
        )
        atual['remessas'] += 1
    return sorted(contagem.values(), key=lambda c: c['nome'])


@router.get('/fluxo')
def fluxo(
    oracle: SessionOracle,
    postgres: SessionPostgres,
    _usuario: AcessoFluxo,
    convenio: int | None = None,
    competencia_de: str | None = None,
    competencia_ate: str | None = None,
    etapa: Literal['protocolo', 'nfse', 'recebimento', 'concluido']
    | None = None,
    q: Annotated[str | None, Query(max_length=60)] = None,
    a_confirmar: bool = False,
    ordenar_por: Literal['parado', 'competencia', 'valor'] = 'parado',
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 10,
    exportar: bool = False,
):
    """Remessas fechadas e a etapa de cada uma, com o funil por etapa.

    O funil considera os filtros da tela, exceto a etapa (que é o próprio
    card selecionado). ``exportar`` devolve todas as remessas filtradas,
    sem paginação (planilha da tela).
    """
    de = _competencia(competencia_de, 'Competência inicial')
    ate = _competencia(competencia_ate, 'Competência final')
    todos = montar_fluxo(
        remessas_mv(oracle),
        processos_portal(postgres),
        vinculos_gravados(postgres),
        _hoje(),
    )
    filtrados = filtrar(
        todos,
        convenio=convenio,
        competencia_de=de,
        competencia_ate=ate,
        q=q,
        a_confirmar=a_confirmar,
    )
    resumo = resumo_funil(filtrados)
    if etapa:
        filtrados = [i for i in filtrados if i['etapa_atual'] == etapa]
    filtrados = ordenar(filtrados, ordenar_por)
    total = len(filtrados)
    total_pages = max(ceil(total / page_size), 1)
    inicio = (page - 1) * page_size
    return {
        'resumo': resumo,
        'etapas': [{'chave': k, 'rotulo': r} for k, r in ETAPAS],
        'itens': (
            filtrados if exportar else filtrados[inicio : inicio + page_size]
        ),
        'total': total,
        'valor_total_filtrado': sum(
            (i['valor'] for i in filtrados), dinheiro(0)
        ),
        'page': page,
        'page_size': page_size,
        'total_pages': total_pages,
        'convenios': _opcoes_convenios(todos),
        'atualizado_em': datetime.now(FUSO_HOSPITAL).isoformat(
            timespec='seconds'
        ),
    }
