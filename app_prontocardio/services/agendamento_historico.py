"""Leitura paginada do histórico de agendamentos, com MV autoritativo."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import bindparam, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app_prontocardio.models import AuditoriaAgendamento, Usuario
from app_prontocardio.services.agendamento_origens import (
    AgendamentoOrigemService,
    OrigemAgendamento,
)

_SQL_HISTORICO_AGENDAMENTOS = """
    WITH movimentos_paciente AS (
        SELECT im.CD_IT_AGENDA_CENTRAL,
               im.CD_MOVIMENTO_AGENDA_CENTRAL,
               im.CD_IT_MOVIMENTO_AGENDA_CENTRAL,
               im.CD_AGENDA_CENTRAL,
               im.CD_ITEM_AGENDAMENTO,
               im.HR_AGENDA,
               im.TP_STATUS,
               mov.CD_PACIENTE,
               mov.DS_OBSERVACAO_GERAL
          FROM DBAMV.IT_MOVIMENTO_AGENDA_CENTRAL im
          JOIN DBAMV.MOVIMENTO_AGENDA_CENTRAL mov
            ON mov.CD_MOVIMENTO_AGENDA_CENTRAL =
               im.CD_MOVIMENTO_AGENDA_CENTRAL
         WHERE mov.CD_PACIENTE = :cd_paciente
           AND im.HR_AGENDA >= :data_inicio
           AND (:data_fim_exclusiva IS NULL
                OR im.HR_AGENDA < :data_fim_exclusiva)
           AND (:status IS NULL OR im.TP_STATUS = :status)
    ), ativos_paciente_base AS (
        SELECT i.CD_IT_AGENDA_CENTRAL,
               i.CD_PACIENTE,
               i.CD_AGENDA_CENTRAL,
               i.CD_ITEM_AGENDAMENTO,
               i.HR_AGENDA,
               i.TP_SITUACAO,
               i.DS_OBSERVACAO,
               i.DS_OBSERVACAO_GERAL
          FROM DBAMV.IT_AGENDA_CENTRAL i
         WHERE i.CD_PACIENTE = :cd_paciente
           AND i.HR_AGENDA >= :data_inicio
           AND (:data_fim_exclusiva IS NULL
                OR i.HR_AGENDA < :data_fim_exclusiva)
           AND (:status IS NULL OR i.TP_SITUACAO = :status)
           AND NOT EXISTS (
               SELECT 1
                 FROM DBAMV.IT_MOVIMENTO_AGENDA_CENTRAL im_ativo
                 JOIN DBAMV.MOVIMENTO_AGENDA_CENTRAL mov_ativo
                   ON mov_ativo.CD_MOVIMENTO_AGENDA_CENTRAL =
                      im_ativo.CD_MOVIMENTO_AGENDA_CENTRAL
                WHERE im_ativo.CD_IT_AGENDA_CENTRAL =
                      i.CD_IT_AGENDA_CENTRAL
                  AND mov_ativo.CD_PACIENTE = i.CD_PACIENTE
                  AND im_ativo.CD_AGENDA_CENTRAL = i.CD_AGENDA_CENTRAL
                  AND im_ativo.CD_ITEM_AGENDAMENTO = i.CD_ITEM_AGENDAMENTO
                  AND im_ativo.HR_AGENDA = i.HR_AGENDA
                  AND im_ativo.TP_STATUS NOT IN ('E', 'C', 'P', 'T')
           )
    ), slots_paciente_periodo AS (
        SELECT CD_IT_AGENDA_CENTRAL FROM movimentos_paciente
        UNION
        SELECT CD_IT_AGENDA_CENTRAL FROM ativos_paciente_base
    ), movimentos_contagem AS (
        SELECT sp.CD_IT_AGENDA_CENTRAL,
               COUNT(DISTINCT im.CD_MOVIMENTO_AGENDA_CENTRAL)
                   AS total_instancias
          FROM slots_paciente_periodo sp
          JOIN DBAMV.IT_MOVIMENTO_AGENDA_CENTRAL im
            ON im.CD_IT_AGENDA_CENTRAL = sp.CD_IT_AGENDA_CENTRAL
         GROUP BY sp.CD_IT_AGENDA_CENTRAL
    ), slots_ocupacao_nao_repr AS (
        SELECT DISTINCT sp.CD_IT_AGENDA_CENTRAL
          FROM slots_paciente_periodo sp
          JOIN DBAMV.IT_AGENDA_CENTRAL i_atual
            ON i_atual.CD_IT_AGENDA_CENTRAL = sp.CD_IT_AGENDA_CENTRAL
         WHERE NOT EXISTS (
               SELECT 1
                 FROM DBAMV.IT_MOVIMENTO_AGENDA_CENTRAL im_ativo
                 JOIN DBAMV.MOVIMENTO_AGENDA_CENTRAL mov_ativo
                   ON mov_ativo.CD_MOVIMENTO_AGENDA_CENTRAL =
                      im_ativo.CD_MOVIMENTO_AGENDA_CENTRAL
                WHERE im_ativo.CD_IT_AGENDA_CENTRAL =
                      i_atual.CD_IT_AGENDA_CENTRAL
                  AND mov_ativo.CD_PACIENTE = i_atual.CD_PACIENTE
                  AND im_ativo.CD_AGENDA_CENTRAL =
                      i_atual.CD_AGENDA_CENTRAL
                  AND im_ativo.CD_ITEM_AGENDAMENTO =
                      i_atual.CD_ITEM_AGENDAMENTO
                  AND im_ativo.HR_AGENDA = i_atual.HR_AGENDA
                  AND im_ativo.TP_STATUS NOT IN ('E', 'C', 'P', 'T')
         )
    ), historicos_movimento AS (
        SELECT im.CD_IT_AGENDA_CENTRAL AS cd_it_agenda_central,
               im.CD_PACIENTE AS cd_paciente,
               im.CD_AGENDA_CENTRAL AS cd_agenda_central,
               im.CD_ITEM_AGENDAMENTO AS cd_item_agendamento,
               ia.DS_ITEM_AGENDAMENTO AS ds_item_agendamento,
               im.HR_AGENDA AS horario,
               CASE
                 WHEN im.TP_STATUS NOT IN ('E', 'C', 'P', 'T')
                  AND i.CD_PACIENTE = im.CD_PACIENTE
                  AND i.CD_AGENDA_CENTRAL = im.CD_AGENDA_CENTRAL
                  AND i.CD_ITEM_AGENDAMENTO = im.CD_ITEM_AGENDAMENTO
                  AND i.HR_AGENDA = im.HR_AGENDA
                 THEN i.DS_OBSERVACAO
               END AS ds_observacao,
               im.DS_OBSERVACAO_GERAL AS ds_observacao_geral,
               NULL AS cd_usuario_agendou,
               im.CD_MOVIMENTO_AGENDA_CENTRAL AS protocolo,
               im.TP_STATUS AS status_mv,
               NULL AS cd_usuario_ultima_operacao,
               NULL AS dt_ultima_operacao,
               NULL AS tp_ultima_operacao,
               NULL AS ds_ultima_operacao,
               p.NM_PRESTADOR AS nm_prestador,
               ua.DS_UNIDADE_ATENDIMENTO AS ds_unidade_atendimento,
               CASE
                 WHEN mc.total_instancias = 1
                  AND reutilizado.CD_IT_AGENDA_CENTRAL IS NULL
                 THEN 1 ELSE 0
               END AS operacoes_correlacionaveis
          FROM movimentos_paciente im
          JOIN DBAMV.AGENDA_CENTRAL ac
            ON ac.CD_AGENDA_CENTRAL = im.CD_AGENDA_CENTRAL
          JOIN DBAMV.ITEM_AGENDAMENTO ia
            ON ia.CD_ITEM_AGENDAMENTO = im.CD_ITEM_AGENDAMENTO
          JOIN movimentos_contagem mc
            ON mc.CD_IT_AGENDA_CENTRAL = im.CD_IT_AGENDA_CENTRAL
          LEFT JOIN DBAMV.IT_AGENDA_CENTRAL i
            ON i.CD_IT_AGENDA_CENTRAL = im.CD_IT_AGENDA_CENTRAL
          LEFT JOIN slots_ocupacao_nao_repr reutilizado
            ON reutilizado.CD_IT_AGENDA_CENTRAL =
               im.CD_IT_AGENDA_CENTRAL
          LEFT JOIN DBAMV.PRESTADOR p
            ON p.CD_PRESTADOR = ac.CD_PRESTADOR
          LEFT JOIN DBAMV.UNIDADE_ATENDIMENTO ua
            ON ua.CD_UNIDADE_ATENDIMENTO = ac.CD_UNIDADE_ATENDIMENTO
    ), ativos_sem_movimento AS (
        SELECT i.CD_IT_AGENDA_CENTRAL AS cd_it_agenda_central,
               i.CD_PACIENTE AS cd_paciente,
               i.CD_AGENDA_CENTRAL AS cd_agenda_central,
               i.CD_ITEM_AGENDAMENTO AS cd_item_agendamento,
               ia.DS_ITEM_AGENDAMENTO AS ds_item_agendamento,
               i.HR_AGENDA AS horario,
               i.DS_OBSERVACAO AS ds_observacao,
               i.DS_OBSERVACAO_GERAL AS ds_observacao_geral,
               NULL AS cd_usuario_agendou,
               NULL AS protocolo,
               i.TP_SITUACAO AS status_mv,
               NULL AS cd_usuario_ultima_operacao,
               NULL AS dt_ultima_operacao,
               NULL AS tp_ultima_operacao,
               NULL AS ds_ultima_operacao,
               p.NM_PRESTADOR AS nm_prestador,
               ua.DS_UNIDADE_ATENDIMENTO AS ds_unidade_atendimento,
               CASE
                 WHEN mc_ativo.CD_IT_AGENDA_CENTRAL IS NULL
                 THEN 1 ELSE 0
               END AS operacoes_correlacionaveis
          FROM ativos_paciente_base i
          JOIN DBAMV.AGENDA_CENTRAL ac
            ON ac.CD_AGENDA_CENTRAL = i.CD_AGENDA_CENTRAL
          JOIN DBAMV.ITEM_AGENDAMENTO ia
            ON ia.CD_ITEM_AGENDAMENTO = i.CD_ITEM_AGENDAMENTO
          LEFT JOIN movimentos_contagem mc_ativo
            ON mc_ativo.CD_IT_AGENDA_CENTRAL =
               i.CD_IT_AGENDA_CENTRAL
          LEFT JOIN DBAMV.PRESTADOR p
            ON p.CD_PRESTADOR = ac.CD_PRESTADOR
          LEFT JOIN DBAMV.UNIDADE_ATENDIMENTO ua
            ON ua.CD_UNIDADE_ATENDIMENTO = ac.CD_UNIDADE_ATENDIMENTO
    ), registros_base AS (
        SELECT * FROM historicos_movimento
        UNION ALL
        SELECT * FROM ativos_sem_movimento
    ), registros AS (
        SELECT registros_base.*,
               COUNT(*) OVER () AS total_registros
          FROM registros_base
         WHERE (__PREDICADO_SLOTS_ORIGEM__)
    ), ordenados AS (
        SELECT registros.*,
               ROW_NUMBER() OVER (
                   ORDER BY horario DESC,
                            cd_it_agenda_central DESC,
                            protocolo DESC NULLS LAST
               ) AS linha
          FROM registros
    ), totais AS (
        SELECT COUNT(*) AS total_registros FROM registros
    ), pagina AS (
        SELECT ordenados.*
          FROM ordenados
         WHERE linha BETWEEN :linha_inicio AND :linha_fim
    ), slots_operacoes_pagina AS (
        SELECT DISTINCT cd_it_agenda_central
          FROM pagina
         WHERE operacoes_correlacionaveis = 1
    ), operacoes_ranqueadas AS (
        SELECT log.CD_IT_AGENDA_CENTRAL AS cd_it_agenda_central,
               log.CD_LOG_OPERA_AGENDA AS cd_log_opera_agenda,
               log.CD_USUARIO AS cd_usuario_operacao,
               log.DT_OPERA_AGENDA AS dt_operacao,
               log.TP_OPERACAO AS tp_operacao,
               SUBSTR(
                   log.DS_OBS_OPERA_AGD_CENTRAL,
                   1,
                   :limite_descricao_operacao
               ) AS ds_operacao,
               ROW_NUMBER() OVER (
                   PARTITION BY log.CD_IT_AGENDA_CENTRAL
                   ORDER BY log.DT_OPERA_AGENDA DESC NULLS LAST,
                            log.CD_LOG_OPERA_AGENDA DESC
               ) AS posicao_operacao,
               ROW_NUMBER() OVER (
                   PARTITION BY log.CD_IT_AGENDA_CENTRAL,
                                log.TP_OPERACAO
                   ORDER BY log.DT_OPERA_AGENDA NULLS LAST,
                            log.CD_LOG_OPERA_AGENDA
               ) AS posicao_tipo
          FROM DBAMV.LOG_OPERA_AGENDA_CENTRAL log
          JOIN slots_operacoes_pagina sp
            ON sp.cd_it_agenda_central = log.CD_IT_AGENDA_CENTRAL
    ), operacoes_pagina AS (
        SELECT cd_it_agenda_central,
               cd_log_opera_agenda,
               cd_usuario_operacao,
               dt_operacao,
               tp_operacao,
               ds_operacao
          FROM operacoes_ranqueadas
         WHERE posicao_operacao <= :limite_operacoes_historico
            OR (tp_operacao = 'A' AND posicao_tipo = 1)
    )
    SELECT * FROM (
        SELECT pagina.*,
               op.cd_log_opera_agenda,
               op.cd_usuario_operacao,
               op.dt_operacao,
               op.tp_operacao,
               op.ds_operacao,
               0 AS eh_metadado
          FROM pagina
          LEFT JOIN operacoes_pagina op
            ON op.cd_it_agenda_central = pagina.cd_it_agenda_central
        UNION ALL
        SELECT NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL,
               NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL,
               total_registros, NULL,
               NULL, NULL, NULL, NULL, NULL, 1
          FROM totais
    )
    ORDER BY eh_metadado,
             linha,
             dt_operacao NULLS LAST,
             cd_log_opera_agenda NULLS LAST
"""


LIMITE_OPERACOES_HISTORICO = 100
LIMITE_DESCRICAO_OPERACAO = 2000


def consulta_historico_agendamentos(
    quantidade_slots: int,
    *,
    excluir_instancias: bool = False,
):
    """Cria binds estruturais em blocos Oracle de até mil slots."""
    if quantidade_slots <= 0:
        predicado = '1 = 1'
        binds = []
    else:
        grupos = (quantidade_slots + 999) // 1000
        nomes = [f'slots_origem_{indice}' for indice in range(grupos)]
        predicados = [
            (
                "TO_CHAR(cd_it_agenda_central) || ':' || "
                f"TO_CHAR(protocolo) IN :{nome}"
            )
            for nome in nomes
        ]
        inclusao = ' OR '.join(predicados)
        predicado = (
            f'(protocolo IS NULL OR NOT ({inclusao}))'
            if excluir_instancias
            else inclusao
        )
        binds = [bindparam(nome, expanding=True) for nome in nomes]
    return text(
        _SQL_HISTORICO_AGENDAMENTOS.replace(
            '__PREDICADO_SLOTS_ORIGEM__', predicado
        )
    ).bindparams(*binds)


EVIDENCIAS_CONFIRMADAS = {
    'GRAVACAO_DIRETA',
    'RECONCILIACAO_SLOT',
    'RECONCILIACAO_PROTOCOLO',
}
TIPO_OPERACAO_AGENDAMENTO = 'A'


def _descricao_status(status: object) -> str:
    """Preserva o código do MV sem fabricar descrição não autoritativa."""
    return str(status).strip() if status is not None else 'Não informado'


def _chave_ordenacao_operacao(linha: Any) -> tuple[bool, datetime, int]:
    data_hora = linha.get('dt_operacao')
    log_id = linha.get('cd_log_opera_agenda')
    return (
        data_hora is None,
        data_hora if isinstance(data_hora, datetime) else datetime.max,
        int(log_id) if log_id is not None else 0,
    )


def _chave_instancia_historico(linha: dict[str, Any]) -> tuple[Any, ...]:
    return (
        linha.get('cd_it_agenda_central'),
        linha.get('protocolo'),
        linha.get('cd_agenda_central'),
        linha.get('cd_item_agendamento'),
        linha.get('horario'),
    )


def normalizar_item_historico(linha: dict[str, Any]) -> dict[str, Any]:
    """Normaliza os dados do MV sem interpretar ou registrar observações."""
    correlacionavel = bool(linha.get('operacoes_correlacionaveis'))
    usuario_agendou = linha.get('cd_usuario_agendou')
    usuario_status = (
        'confirmado'
        if usuario_agendou is not None
        else ('ausente' if correlacionavel else 'ambiguo')
    )

    return {
        'cd_it_agenda_central': int(linha['cd_it_agenda_central']),
        'cd_paciente': linha.get('cd_paciente'),
        'cd_agenda_central': linha.get('cd_agenda_central'),
        'protocolo': linha.get('protocolo'),
        'horario': linha['horario'],
        'cd_item_agendamento': int(linha['cd_item_agendamento']),
        'ds_item_agendamento': str(linha['ds_item_agendamento']),
        'nm_prestador': linha.get('nm_prestador'),
        'ds_unidade_atendimento': linha.get('ds_unidade_atendimento'),
        'status_mv': linha.get('status_mv'),
        'status_descricao': _descricao_status(linha.get('status_mv')),
        'observacao': linha.get('ds_observacao'),
        'observacao_geral': linha.get('ds_observacao_geral'),
        'usuario_agendou': usuario_agendou,
        'usuario_agendou_status': usuario_status,
        'ultima_operacao': None,
        'historico_operacoes': [],
        'historico_operacoes_status': (
            'ausente' if correlacionavel else 'ambiguo'
        ),
        'usuario_responsavel': usuario_agendou,
        'origem': None,
        'origem_status': 'temporariamente_indisponivel',
        'evidencia_origem': None,
        'cd_it_agenda_central_anterior': None,
        'eventos': [],
        '_operacoes_correlacionaveis': correlacionavel,
    }


def montar_itens_historico(
    linhas: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Agrupa a página e sua trilha, já retornadas no mesmo snapshot Oracle."""
    agrupados: dict[tuple[Any, ...], dict[str, Any]] = {}
    operacoes: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
    for linha in linhas:
        chave = _chave_instancia_historico(linha)
        item = agrupados.get(chave)
        if item is None:
            item = normalizar_item_historico(linha)
            agrupados[chave] = item
            operacoes[chave] = []
        if not item.get('_operacoes_correlacionaveis'):
            continue
        if (
            linha.get('tp_operacao') is None
            or linha.get('cd_log_opera_agenda') is None
        ):
            continue
        operacoes[chave].append(linha)

    for chave, item in agrupados.items():
        correlacionavel = bool(
            item.pop('_operacoes_correlacionaveis', False)
        )
        if not correlacionavel:
            continue
        eventos = [
            {
                'tipo': str(linha['tp_operacao']),
                'data_hora': linha.get('dt_operacao'),
                'usuario': linha.get('cd_usuario_operacao'),
                'descricao': linha.get('ds_operacao'),
            }
            for linha in sorted(
                operacoes[chave], key=_chave_ordenacao_operacao
            )
        ]
        item['historico_operacoes'] = eventos
        item['ultima_operacao'] = eventos[-1] if eventos else None
        item['eventos'] = [eventos[-1]] if eventos else []
        item['historico_operacoes_status'] = (
            'disponivel' if eventos else 'ausente'
        )
        agendamento = next(
            (
                evento
                for evento in eventos
                if evento['tipo'] == TIPO_OPERACAO_AGENDAMENTO
            ),
            None,
        )
        if agendamento is not None and agendamento['usuario']:
            item['usuario_agendou'] = agendamento['usuario']
            item['usuario_responsavel'] = agendamento['usuario']
            item['usuario_agendou_status'] = 'confirmado'
    return list(agrupados.values())


def mesclar_origens(
    itens: list[dict[str, Any]],
    *,
    origens: dict[tuple[int, int], Any],
    disponivel: bool,
) -> list[dict[str, Any]]:
    """Mescla somente classificações persistidas, sem inferir ausências."""
    for item in itens:
        if not disponivel:
            item['origem'] = None
            item['origem_status'] = 'temporariamente_indisponivel'
            item['evidencia_origem'] = None
            continue

        protocolo = item.get('protocolo')
        chave = (
            (item['cd_it_agenda_central'], int(protocolo))
            if protocolo is not None
            else None
        )
        registro = origens.get(chave) if chave is not None else None
        if (
            registro is None
            or registro.origem == OrigemAgendamento.NAO_IDENTIFICADA.value
        ):
            item['origem'] = OrigemAgendamento.NAO_IDENTIFICADA.value
            item['origem_status'] = 'nao_identificada'
            item['evidencia_origem'] = (
                getattr(registro, 'evidencia', None) if registro else None
            )
            continue

        confianca = _confianca_origem(registro, origens, set())
        if confianca is None:
            item['origem'] = OrigemAgendamento.NAO_IDENTIFICADA.value
            item['origem_status'] = 'nao_identificada'
            item['evidencia_origem'] = None
            continue
        item['origem'] = registro.origem
        item['origem_status'] = confianca
        item['evidencia_origem'] = registro.evidencia
        item['cd_it_agenda_central_anterior'] = getattr(
            registro, 'slot_origem_anterior', None
        )
    return itens


def _confianca_origem(
    registro: Any,
    origens: dict[tuple[int, int], Any],
    visitados: set[tuple[int, int]],
) -> str | None:
    evidencia = getattr(registro, 'evidencia', None)
    if evidencia in EVIDENCIAS_CONFIRMADAS:
        return 'confirmada'
    if evidencia == 'MARCADOR_EXPLICITO':
        return 'provavel'
    if evidencia != 'HERDADA_REMARCACAO':
        return None
    slot_anterior = getattr(registro, 'slot_origem_anterior', None)
    protocolo_anterior = getattr(
        registro, 'protocolo_origem_anterior', None
    )
    chave_anterior = (slot_anterior, protocolo_anterior)
    if (
        slot_anterior is None
        or protocolo_anterior is None
        or chave_anterior in visitados
    ):
        return None
    anterior = origens.get(chave_anterior)
    if anterior is None:
        return None
    visitados.add(chave_anterior)
    return _confianca_origem(anterior, origens, visitados)


def _subtrair_anos(data_referencia: date, anos: int) -> date:
    try:
        return data_referencia.replace(year=data_referencia.year - anos)
    except ValueError:
        return data_referencia.replace(
            year=data_referencia.year - anos, month=2, day=28
        )


def _somar_anos(data_referencia: date, anos: int) -> date:
    try:
        return data_referencia.replace(year=data_referencia.year + anos)
    except ValueError:
        return data_referencia.replace(
            year=data_referencia.year + anos, month=2, day=28
        )


class RepositorioHistoricoAgendamento:
    def __init__(self, oracle: Session, postgres: Session):
        self.oracle = oracle
        self.postgres = postgres

    def listar(  # noqa: PLR0912, PLR0913 - mirrors public query contract.
        self,
        *,
        cd_paciente: int,
        data_inicio: date | None = None,
        data_fim: date | None = None,
        pagina: int = 1,
        limite: int = 20,
        status: str | None = None,
        origem: OrigemAgendamento | None = None,
        operador: Usuario | None = None,
    ) -> dict[str, Any]:
        hoje = date.today()
        if (data_inicio is None) != (data_fim is None):
            raise ValueError(
                'Informe data_inicio e data_fim juntas para filtrar '
                'o histórico.'
            )
        inicio = data_inicio or _subtrair_anos(hoje, 2)
        if data_fim is not None and data_fim < inicio:
            raise ValueError(
                'A data final deve ser igual ou posterior à inicial.'
            )
        if (
            data_inicio is not None
            and data_fim is not None
            and data_fim > _somar_anos(data_inicio, 5)
        ):
            raise ValueError('O período máximo permitido é de cinco anos.')

        origem_disponivel = True
        instancias_origem: list[tuple[int, int]] = []
        excluir_instancias = False
        service_origem = AgendamentoOrigemService(self.postgres)
        if origem is not None:
            try:
                if origem == OrigemAgendamento.NAO_IDENTIFICADA:
                    instancias_origem = (
                        service_origem.listar_instancias_identificadas(
                            cd_paciente=cd_paciente,
                        )
                    )
                    excluir_instancias = True
                else:
                    instancias_origem = (
                        service_origem.listar_instancias_por_origem(
                            cd_paciente=cd_paciente,
                            origem=origem,
                        )
                    )
            except SQLAlchemyError:
                origem_disponivel = False
                self.postgres.rollback()
            else:
                if (
                    origem != OrigemAgendamento.NAO_IDENTIFICADA
                    and not instancias_origem
                ):
                    return {
                        'agendamentos': [],
                        'total': 0,
                        'pagina': pagina,
                        'limite': limite,
                    }

        parametros = {
            'cd_paciente': cd_paciente,
            'data_inicio': inicio,
            'data_fim_exclusiva': (
                data_fim + timedelta(days=1) if data_fim is not None else None
            ),
            'status': status.strip() if status else None,
            'linha_inicio': (pagina - 1) * limite + 1,
            'linha_fim': pagina * limite,
            'limite_operacoes_historico': LIMITE_OPERACOES_HISTORICO,
            'limite_descricao_operacao': LIMITE_DESCRICAO_OPERACAO,
        }
        instancias_filtro = (
            instancias_origem
            if origem is not None and origem_disponivel
            else []
        )
        chaves_filtro = [
            f'{slot}:{protocolo}' for slot, protocolo in instancias_filtro
        ]
        for indice in range(0, len(chaves_filtro), 1000):
            parametros[f'slots_origem_{indice // 1000}'] = chaves_filtro[
                indice : indice + 1000
            ]
        rows = self.oracle.execute(
            consulta_historico_agendamentos(
                len(chaves_filtro),
                excluir_instancias=(
                    excluir_instancias and origem_disponivel
                ),
            ),
            parametros,
        ).mappings().all()
        linhas_mv = [dict(row) for row in rows if not row.get('eh_metadado')]
        itens = montar_itens_historico(linhas_mv)
        for item in itens:
            item['cd_paciente'] = cd_paciente
        total = int(rows[0]['total_registros']) if rows else 0

        if origem_disponivel:
            try:
                origens = self._buscar_origens_com_ancestrais(
                    service_origem,
                    [
                        (item['cd_it_agenda_central'], int(item['protocolo']))
                        for item in itens
                        if item.get('protocolo') is not None
                    ],
                )
            except SQLAlchemyError:
                origem_disponivel = False
                origens = {}
                self.postgres.rollback()
        else:
            origens = {}
        if operador is not None and itens:
            self._auditar_leitura(itens, operador)
        return {
            'agendamentos': mesclar_origens(
                itens, origens=origens, disponivel=origem_disponivel
            ),
            'total': total,
            'pagina': pagina,
            'limite': limite,
        }

    @staticmethod
    def _buscar_origens_com_ancestrais(
        service_origem: AgendamentoOrigemService,
        instancias: list[tuple[int, int]],
    ) -> dict[tuple[int, int], Any]:
        origens = service_origem.buscar_por_instancias(instancias)
        pendentes = {
            (
                registro.slot_origem_anterior,
                registro.protocolo_origem_anterior,
            )
            for registro in origens.values()
            if registro.evidencia == 'HERDADA_REMARCACAO'
            and registro.slot_origem_anterior is not None
            and registro.protocolo_origem_anterior is not None
        }
        consultados = set(origens)
        while pendentes:
            novos_slots = pendentes - consultados
            if not novos_slots:
                break
            consultados.update(novos_slots)
            novos = service_origem.buscar_por_instancias(list(novos_slots))
            origens.update(novos)
            pendentes = {
                (
                    registro.slot_origem_anterior,
                    registro.protocolo_origem_anterior,
                )
                for registro in novos.values()
                if registro.evidencia == 'HERDADA_REMARCACAO'
                and registro.slot_origem_anterior is not None
                and registro.protocolo_origem_anterior is not None
            }
        return origens

    def _auditar_leitura(
        self,
        itens: list[dict[str, Any]],
        operador: object,
    ) -> None:
        for item in itens:
            self.postgres.add(
                AuditoriaAgendamento(
                    operador_id=getattr(operador, 'id', None),
                    operador_nome=getattr(
                        operador, 'nome', 'ACESSO_INTERNO'
                    ),
                    origem='HISTORICO_INTERNO',
                    cd_paciente=item.get('cd_paciente', 0),
                    cd_item_agendamento=item['cd_item_agendamento'],
                    cd_it_agenda_central=item['cd_it_agenda_central'],
                    cd_agenda_central=item.get('cd_agenda_central', 0),
                    cd_tip_mar=None,
                    protocolo_mv=item.get('protocolo'),
                    status='historico_consultado',
                    chave_efeito_lote=None,
                )
            )
        self.postgres.commit()
