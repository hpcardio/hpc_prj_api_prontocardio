from collections.abc import Mapping
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session

from app_prontocardio.schemas.precos_rede import (
    DivergenciaPrecoRede,
    PrecoRedeItem,
    PrecoRedeResultado,
)


def _normalizar_linha(linha: Mapping[str, object]) -> dict[str, object]:
    return {str(chave).lower(): valor for chave, valor in linha.items()}


def listar_precos_rede_mv(
    session: Session,
    codigos: list[str],
    codigos_pacote: list[str],
    limit: int,
    cursor: str | None,
) -> PrecoRedeResultado:
    extraido_em = datetime.now().astimezone()
    filtros: list[str] = []
    parametros: dict[str, object] = {'limite': limit + 1}
    expanding: list[str] = []

    if codigos or codigos_pacote:
        tipos: list[str] = []
        if codigos:
            tipos.append(
                "(tipo_chave = 'PROCEDIMENTO' "
                'AND codigo IN :codigos)'
            )
            parametros['codigos'] = list(dict.fromkeys(codigos))
            expanding.append('codigos')
        if codigos_pacote:
            tipos.append(
                "(tipo_chave = 'PACOTE' "
                'AND codigo IN :codigos_pacote)'
            )
            parametros['codigos_pacote'] = list(dict.fromkeys(codigos_pacote))
            expanding.append('codigos_pacote')
        filtros.append(f"({' OR '.join(tipos)})")
    if cursor:
        filtros.append("tipo_chave || ':' || codigo > :cursor")
        parametros['cursor'] = cursor

    where_sql = f"WHERE {' AND '.join(filtros)}" if filtros else ''
    consulta = text(f'''
        WITH precos AS (
            SELECT
                'PROCEDIMENTO' AS tipo_chave,
                827 AS cd_tab_fat,
                TO_CHAR(v.CD_PRO_FAT) AS codigo,
                p.DS_PRO_FAT AS descricao,
                v.VL_TOTAL AS valor_rede,
                v.DT_VIGENCIA AS vigencia_inicio,
                COUNT(*) OVER (
                    PARTITION BY v.CD_TAB_FAT, v.CD_PRO_FAT, v.DT_VIGENCIA
                ) AS total_mesma_vigencia
            FROM DBAMV.VAL_PRO v
            JOIN DBAMV.PRO_FAT p ON p.CD_PRO_FAT = v.CD_PRO_FAT
            WHERE v.CD_TAB_FAT = 827
              AND v.SN_ATIVO = 'S'
              AND v.DT_VIGENCIA = (
                  SELECT MAX(v2.DT_VIGENCIA)
                  FROM DBAMV.VAL_PRO v2
                  WHERE v2.CD_TAB_FAT = v.CD_TAB_FAT
                    AND v2.CD_PRO_FAT = v.CD_PRO_FAT
                    AND v2.SN_ATIVO = 'S'
                    AND v2.DT_VIGENCIA <= SYSDATE
              )
            UNION ALL
            SELECT
                'PACOTE' AS tipo_chave,
                826 AS cd_tab_fat,
                TO_CHAR(v.CD_PRO_FAT) AS codigo,
                p.DS_PRO_FAT AS descricao,
                v.VL_TOTAL AS valor_rede,
                v.DT_VIGENCIA AS vigencia_inicio,
                COUNT(*) OVER (
                    PARTITION BY v.CD_TAB_FAT, v.CD_PRO_FAT, v.DT_VIGENCIA
                ) AS total_mesma_vigencia
            FROM DBAMV.VAL_PRO v
            JOIN DBAMV.PRO_FAT p ON p.CD_PRO_FAT = v.CD_PRO_FAT
            WHERE v.CD_TAB_FAT = 826
              AND v.SN_ATIVO = 'S'
              AND v.DT_VIGENCIA = (
                  SELECT MAX(v2.DT_VIGENCIA)
                  FROM DBAMV.VAL_PRO v2
                  WHERE v2.CD_TAB_FAT = v.CD_TAB_FAT
                    AND v2.CD_PRO_FAT = v.CD_PRO_FAT
                    AND v2.SN_ATIVO = 'S'
                    AND v2.DT_VIGENCIA <= SYSDATE
              )
        )
        SELECT * FROM (
            SELECT precos.*
            FROM precos
            {where_sql}
            ORDER BY tipo_chave, codigo
        ) WHERE ROWNUM <= :limite
    ''')
    for nome in expanding:
        consulta = consulta.bindparams(bindparam(nome, expanding=True))
    linhas_brutas = session.execute(consulta, parametros).mappings().all()
    linhas = [_normalizar_linha(linha) for linha in linhas_brutas]
    tem_proxima = len(linhas) > limit
    linhas = linhas[:limit]

    pacotes = [
        str(linha['codigo'])
        for linha in linhas
        if linha['tipo_chave'] == 'PACOTE'
    ]
    componentes: dict[str, list[str]] = {}
    if pacotes:
        consulta_componentes = text('''
            SELECT DISTINCT
                TO_CHAR(p.CD_PRO_FAT_PACOTE) AS codigo_pacote,
                TO_CHAR(p.CD_PRO_FAT) AS codigo_procedimento
            FROM DBAMV.PACOTE p
            WHERE TO_CHAR(p.CD_PRO_FAT_PACOTE) IN :pacotes
              AND p.DT_VIGENCIA <= SYSDATE
              AND (
                  p.DT_VIGENCIA_FINAL IS NULL
                  OR p.DT_VIGENCIA_FINAL >= TRUNC(SYSDATE)
              )
            ORDER BY codigo_pacote, codigo_procedimento
        ''').bindparams(bindparam('pacotes', expanding=True))
        itens = session.execute(
            consulta_componentes, {'pacotes': pacotes}
        ).mappings().all()
        for item_bruto in itens:
            item = _normalizar_linha(item_bruto)
            componentes.setdefault(str(item['codigo_pacote']), []).append(
                str(item['codigo_procedimento'])
            )

    proximo_cursor = None
    if tem_proxima and linhas:
        ultima = linhas[-1]
        proximo_cursor = f"{ultima['tipo_chave']}:{ultima['codigo']}"
    resultado = montar_resultado_precos(
        linhas, componentes, extraido_em, proximo_cursor
    )

    if not tem_proxima:
        encontrados = {
            f"{linha['tipo_chave']}:{linha['codigo']}" for linha in linhas
        }
        solicitados = {
            *(f'PROCEDIMENTO:{codigo}' for codigo in codigos),
            *(f'PACOTE:{codigo}' for codigo in codigos_pacote),
        }
        resultado.divergencias.extend(
            DivergenciaPrecoRede(
                chave=chave, motivo='Sem preço vigente no MV'
            )
            for chave in sorted(solicitados - encontrados)
        )
    return resultado


def _origem_id(tab: int, codigo: str, vigencia: date) -> str:
    return f'VAL_PRO:{tab}:{codigo}:{vigencia:%Y%m%d}'


def montar_resultado_precos(
    linhas: list[Mapping[str, object]],
    componentes: Mapping[str, list[str]],
    extraido_em: datetime,
    next_cursor: str | None = None,
) -> PrecoRedeResultado:
    results: list[PrecoRedeItem] = []
    divergencias: list[DivergenciaPrecoRede] = []
    for linha in linhas:
        tipo = str(linha['tipo_chave'])
        codigo = str(linha['codigo'])
        chave = f'{tipo}:{codigo}'
        valor = Decimal(str(linha['valor_rede'] or 0))
        if int(linha['total_mesma_vigencia']) != 1:
            divergencias.append(
                DivergenciaPrecoRede(
                    chave=chave,
                    motivo='Mais de um preço na vigência atual',
                )
            )
            continue
        if valor <= 0:
            divergencias.append(
                DivergenciaPrecoRede(
                    chave=chave,
                    motivo='Valor da Rede ausente ou inválido',
                )
            )
            continue
        vigencia = linha['vigencia_inicio']
        if not isinstance(vigencia, date):
            raise ValueError('Vigência inválida retornada pelo MV.')
        codigos = (
            list(dict.fromkeys(componentes.get(codigo, [])))
            if tipo == 'PACOTE'
            else [codigo]
        )
        codigo_procedimento = codigos[0] if codigos else codigo
        results.append(
            PrecoRedeItem(
                codigo_procedimento=codigo_procedimento,
                codigos_procedimento=codigos or [codigo],
                codigo_pacote=codigo if tipo == 'PACOTE' else None,
                descricao=str(linha['descricao']).strip(),
                valor_rede=valor,
                vigencia_inicio=vigencia,
                origem_id=_origem_id(
                    int(linha['cd_tab_fat']), codigo, vigencia
                ),
                extraido_em=extraido_em,
            )
        )
    return PrecoRedeResultado(
        count=len(results),
        results=results,
        divergencias=divergencias,
        next_cursor=next_cursor,
        extraido_em=extraido_em,
    )
