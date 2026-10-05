from __future__ import annotations

import json
from decimal import Decimal
from hashlib import sha256
from typing import Any

from sqlalchemy import text


class CancelamentoBloqueado(ValueError):
    """O título existe, mas o estado financeiro impede o cancelamento."""


class ConcorrenciaFinanceira(RuntimeError):
    """O título mudou durante a tentativa de cancelamento."""


class TituloNaoEncontrado(LookupError):
    """O título solicitado não existe entre os repasses do MV."""


TITULO_PARCELAS_QUERY = text("""
SELECT cp.cd_con_pag,
       cp.nr_documento,
       cp.dt_lancamento,
       cp.dt_emissao,
       cp.vl_bruto_conta,
       cp.cd_fornecedor,
       cp.ds_fornecedor,
       cp.cd_multi_empresa,
       cp.cd_usuario_ins AS usuario_mv,
       vinculo.cd_repasse,
       vinculo.qtd_repasses,
       vinculo.dt_competencia,
       i.cd_itcon_pag,
       i.nr_parcela,
       i.dt_vencimento,
       i.vl_duplicata,
       i.tp_quitacao,
       i.sn_baixada,
       NVL(i.vl_soma_pago, 0) AS vl_pago,
       COALESCE(i.cd_itcon_pag_agrup, i.cd_con_pag_agrup)
           AS cd_agrupamento,
       (
           SELECT COUNT(*)
             FROM dbamv.pagcon_pag p
            WHERE p.cd_itcon_pag = i.cd_itcon_pag
              AND NVL(p.sn_estorno, 'N') <> 'S'
       ) AS qtd_pagamentos
  FROM dbamv.con_pag cp
  JOIN dbamv.itcon_pag i ON i.cd_con_pag = cp.cd_con_pag
  LEFT JOIN (
        SELECT rpc.cd_con_pag,
               MIN(rpc.cd_repasse) AS cd_repasse,
               COUNT(DISTINCT rpc.cd_repasse) AS qtd_repasses,
               MIN(r.dt_competencia) AS dt_competencia
          FROM dbamv.repasse_prestador_con_pag rpc
          LEFT JOIN dbamv.repasse r ON r.cd_repasse = rpc.cd_repasse
         GROUP BY rpc.cd_con_pag
  ) vinculo ON vinculo.cd_con_pag = cp.cd_con_pag
 WHERE cp.cd_con_pag = :cd_con_pag
 ORDER BY i.nr_parcela, i.cd_itcon_pag
""")


TITULO_PARCELAS_FOR_UPDATE_QUERY = text(
    str(TITULO_PARCELAS_QUERY) + '\n FOR UPDATE OF i.tp_quitacao'
)


CANCELAR_PARCELAS_QUERY = text("""
UPDATE dbamv.itcon_pag
   SET tp_quitacao = 'C'
 WHERE cd_con_pag = :cd_con_pag
   AND tp_quitacao IN ('P', 'N')
   AND NVL(sn_baixada, 'N') = 'N'
   AND NVL(vl_soma_pago, 0) = 0
   AND cd_itcon_pag_agrup IS NULL
   AND cd_con_pag_agrup IS NULL
   AND NOT EXISTS (
       SELECT 1
         FROM dbamv.pagcon_pag p
        WHERE p.cd_itcon_pag = dbamv.itcon_pag.cd_itcon_pag
          AND NVL(p.sn_estorno, 'N') <> 'S'
   )
""")


LISTAR_TITULOS_QUERY = text("""
WITH vinculo AS (
    SELECT rpc.cd_con_pag,
           MIN(rpc.cd_repasse) AS cd_repasse,
           COUNT(DISTINCT rpc.cd_repasse) AS qtd_repasses,
           MIN(r.dt_competencia) AS dt_competencia
      FROM dbamv.repasse_prestador_con_pag rpc
      LEFT JOIN dbamv.repasse r ON r.cd_repasse = rpc.cd_repasse
     GROUP BY rpc.cd_con_pag
), parcelas AS (
    SELECT i.cd_con_pag,
           COUNT(*) AS qtd_parcelas,
           SUM(NVL(i.vl_duplicata, 0)) AS valor_parcelas,
           SUM(NVL(i.vl_soma_pago, 0)) AS valor_pago,
           MAX(CASE WHEN NVL(i.sn_baixada, 'N') = 'S' THEN 1 ELSE 0 END)
               AS possui_baixa,
           MAX(CASE WHEN i.cd_itcon_pag_agrup IS NOT NULL
                      OR i.cd_con_pag_agrup IS NOT NULL THEN 1 ELSE 0 END)
               AS possui_agrupamento,
           MAX(CASE WHEN i.tp_quitacao = 'C' THEN 1 ELSE 0 END)
               AS possui_cancelada,
           MIN(CASE WHEN i.tp_quitacao = 'C' THEN 1 ELSE 0 END)
               AS todas_canceladas,
           SUM((SELECT COUNT(*) FROM dbamv.pagcon_pag p
                 WHERE p.cd_itcon_pag = i.cd_itcon_pag
                   AND NVL(p.sn_estorno, 'N') <> 'S')) AS qtd_pagamentos
      FROM dbamv.itcon_pag i
     GROUP BY i.cd_con_pag
), base AS (
    SELECT cp.cd_con_pag,
           cp.nr_documento,
           cp.dt_lancamento,
           cp.dt_emissao,
           cp.vl_bruto_conta,
           cp.cd_fornecedor,
           cp.ds_fornecedor AS fornecedor,
           cp.cd_multi_empresa,
           cp.cd_usuario_ins AS usuario_mv,
           v.cd_repasse,
           v.qtd_repasses,
           v.dt_competencia AS competencia,
           p.qtd_parcelas,
           p.valor_parcelas,
           p.valor_pago,
           CASE
             WHEN NVL(p.valor_pago, 0) > 0 OR NVL(p.qtd_pagamentos, 0) > 0
               THEN 'pago'
             WHEN p.possui_baixa = 1 THEN 'baixado'
             WHEN p.possui_agrupamento = 1 THEN 'agrupado'
             WHEN p.todas_canceladas = 1 THEN 'cancelado'
             WHEN p.possui_cancelada = 1 THEN 'inconsistente'
             WHEN v.qtd_repasses = 1 THEN 'previsto'
             ELSE 'inconsistente'
           END AS situacao
      FROM dbamv.con_pag cp
      JOIN parcelas p ON p.cd_con_pag = cp.cd_con_pag
      JOIN vinculo v ON v.cd_con_pag = cp.cd_con_pag
)
SELECT base.*, COUNT(*) OVER() AS total_registros
  FROM base
 WHERE (:competencia IS NULL OR
        (base.competencia >= :competencia
         AND base.competencia < ADD_MONTHS(:competencia, 1)))
   AND (:fornecedor IS NULL OR
        UPPER(base.fornecedor) LIKE UPPER(:fornecedor))
   AND (:cd_con_pag IS NULL OR base.cd_con_pag = :cd_con_pag)
   AND (:nr_documento IS NULL OR
        UPPER(base.nr_documento) LIKE UPPER(:nr_documento))
   AND (:cd_repasse IS NULL OR base.cd_repasse = :cd_repasse)
   AND (:situacao IS NULL OR base.situacao = :situacao)
 ORDER BY base.dt_lancamento DESC, base.cd_con_pag DESC
 OFFSET :offset ROWS FETCH NEXT :limite ROWS ONLY
""")


def _decimal(valor: Any) -> Decimal:
    return Decimal(str(valor or 0))


def classificar_titulo(titulo: dict[str, Any]) -> str:  # noqa: PLR0911
    parcelas = titulo.get('parcelas') or []
    if not parcelas:
        return 'inconsistente'
    if any(
        _decimal(parcela.get('vl_pago')) > 0
        or int(parcela.get('qtd_pagamentos') or 0) > 0
        for parcela in parcelas
    ):
        return 'pago'
    if any(
        str(parcela.get('sn_baixada') or 'N').upper() == 'S'
        for parcela in parcelas
    ):
        return 'baixado'
    if any(parcela.get('cd_agrupamento') is not None for parcela in parcelas):
        return 'agrupado'
    situacoes = {
        str(parcela.get('tp_quitacao') or '').upper()
        for parcela in parcelas
    }
    if situacoes == {'C'}:
        return 'cancelado'
    if situacoes.issubset({'P', 'N'}) and situacoes:
        return 'previsto'
    return 'inconsistente'


def validar_cancelamento(
    titulo: dict[str, Any], codigo_repasse_esperado: int
) -> bool:
    if int(titulo.get('qtd_repasses') or 1) != 1:
        raise CancelamentoBloqueado(
            'O título possui mais de um repasse vinculado.'
        )
    if int(titulo.get('cd_repasse') or 0) != codigo_repasse_esperado:
        raise CancelamentoBloqueado(
            'O título não pertence ao repasse informado.'
        )
    estado = classificar_titulo(titulo)
    if estado == 'cancelado':
        return False
    if estado != 'previsto':
        raise CancelamentoBloqueado(
            f'O título está {estado} e não pode ser cancelado.'
        )
    return True


def token_cancelamento(titulo: dict[str, Any]) -> str:
    snapshot = {
        'cd_con_pag': titulo.get('cd_con_pag'),
        'cd_repasse': titulo.get('cd_repasse'),
        'parcelas': [
            {
                'cd_itcon_pag': parcela.get('cd_itcon_pag'),
                'tp_quitacao': parcela.get('tp_quitacao'),
                'sn_baixada': parcela.get('sn_baixada'),
                'vl_pago': str(_decimal(parcela.get('vl_pago'))),
                'qtd_pagamentos': int(
                    parcela.get('qtd_pagamentos') or 0
                ),
                'cd_agrupamento': parcela.get('cd_agrupamento'),
            }
            for parcela in titulo.get('parcelas') or []
        ],
    }
    payload = json.dumps(
        snapshot, ensure_ascii=True, sort_keys=True, separators=(',', ':')
    )
    return sha256(payload.encode()).hexdigest()


def _montar_titulo(linhas: list[dict[str, Any]]) -> dict[str, Any]:
    if not linhas:
        raise TituloNaoEncontrado('Título de repasse não encontrado no MV.')
    primeira = linhas[0]
    return {
        'cd_con_pag': primeira.get('cd_con_pag'),
        'nr_documento': primeira.get('nr_documento'),
        'dt_lancamento': primeira.get('dt_lancamento'),
        'dt_emissao': primeira.get('dt_emissao'),
        'vl_bruto_conta': primeira.get('vl_bruto_conta'),
        'cd_fornecedor': primeira.get('cd_fornecedor'),
        'fornecedor': primeira.get('ds_fornecedor'),
        'cd_multi_empresa': primeira.get('cd_multi_empresa'),
        'usuario_mv': primeira.get('usuario_mv'),
        'cd_repasse': primeira.get('cd_repasse'),
        'qtd_repasses': int(primeira.get('qtd_repasses') or 0),
        'competencia': primeira.get('dt_competencia'),
        'parcelas': [
            {
                'cd_itcon_pag': linha.get('cd_itcon_pag'),
                'nr_parcela': linha.get('nr_parcela'),
                'dt_vencimento': linha.get('dt_vencimento'),
                'vl_duplicata': linha.get('vl_duplicata'),
                'tp_quitacao': linha.get('tp_quitacao'),
                'sn_baixada': linha.get('sn_baixada'),
                'vl_pago': linha.get('vl_pago'),
                'qtd_pagamentos': int(
                    linha.get('qtd_pagamentos') or 0
                ),
                'cd_agrupamento': linha.get('cd_agrupamento'),
            }
            for linha in linhas
        ],
    }


def carregar_titulo(
    session, cd_con_pag: int, *, bloquear: bool = False
) -> dict[str, Any]:
    query = (
        TITULO_PARCELAS_FOR_UPDATE_QUERY
        if bloquear
        else TITULO_PARCELAS_QUERY
    )
    linhas = [
        dict(row)
        for row in session.execute(
            query, {'cd_con_pag': cd_con_pag}
        ).mappings().all()
    ]
    return _montar_titulo(linhas)


def cancelar_titulo(
    session,
    *,
    cd_con_pag: int,
    codigo_repasse_esperado: int,
    token_esperado: str,
) -> dict[str, Any]:
    try:
        titulo = carregar_titulo(session, cd_con_pag, bloquear=True)
        deve_atualizar = validar_cancelamento(
            titulo, codigo_repasse_esperado
        )
        if not deve_atualizar:
            session.rollback()
            return {
                'estado': 'cancelado',
                'idempotente': True,
                'titulo': titulo,
            }
        if token_cancelamento(titulo) != token_esperado:
            raise ConcorrenciaFinanceira(
                'O título mudou desde a pré-validação. Atualize os dados.'
            )
        esperado = len(titulo['parcelas'])
        atualizado = session.execute(
            CANCELAR_PARCELAS_QUERY, {'cd_con_pag': cd_con_pag}
        ).rowcount
        if atualizado != esperado:
            raise ConcorrenciaFinanceira(
                'O estado financeiro mudou durante o cancelamento.'
            )
        confirmado = carregar_titulo(session, cd_con_pag, bloquear=False)
        if classificar_titulo(confirmado) != 'cancelado':
            raise ConcorrenciaFinanceira(
                'O MV não confirmou todas as parcelas como canceladas.'
            )
        session.commit()
        return {
            'estado': 'cancelado',
            'idempotente': False,
            'titulo': confirmado,
            'estado_anterior': titulo,
        }
    except Exception:
        session.rollback()
        raise


def listar_titulos(  # noqa: PLR0913
    session,
    *,
    competencia=None,
    fornecedor: str | None = None,
    cd_con_pag: int | None = None,
    nr_documento: str | None = None,
    cd_repasse: int | None = None,
    situacao: str | None = None,
    pagina: int = 1,
    limite: int = 50,
) -> dict[str, Any]:
    params = {
        'competencia': competencia,
        'fornecedor': f'%{fornecedor.strip()}%' if fornecedor else None,
        'cd_con_pag': cd_con_pag,
        'nr_documento': (
            f'%{nr_documento.strip()}%' if nr_documento else None
        ),
        'cd_repasse': cd_repasse,
        'situacao': situacao,
        'offset': (pagina - 1) * limite,
        'limite': limite,
    }
    rows = [
        dict(row)
        for row in session.execute(
            LISTAR_TITULOS_QUERY, params
        ).mappings().all()
    ]
    total = int(rows[0].pop('total_registros')) if rows else 0
    for row in rows[1:]:
        row.pop('total_registros', None)
    return {
        'itens': rows,
        'pagina': pagina,
        'limite': limite,
        'total': total,
    }
