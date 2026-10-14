"""Leituras do SoulMV para a Agenda Médica (somente leitura).

Escalas-modelo, especialidades, conflitos de horário e agendas do prestador.
A gravação no SoulMV não é feita aqui: `plano_geracao` descreve exatamente o
que a rotina oficial da MV (PKG_SCMA_M_GERA_AGENDA) receberia, para revisão.
"""

from datetime import date

from sqlalchemy import text
from sqlalchemy.orm import Session

# TP_DIA_SEMANA do SoulMV: 1 = domingo ... 7 = sábado.
DIAS_SEMANA = {
    1: 'domingo',
    2: 'segunda-feira',
    3: 'terça-feira',
    4: 'quarta-feira',
    5: 'quinta-feira',
    6: 'sexta-feira',
    7: 'sábado',
}

CONSULTA_ESCALA = """
    SELECT e.CD_ESCALA_CENTRAL AS cd_escala_central,
           e.TP_DIA_SEMANA AS tp_dia_semana,
           TO_CHAR(e.HR_INICIO, 'HH24:MI') AS hr_inicio,
           TO_CHAR(e.HR_FIM, 'HH24:MI') AS hr_fim,
           e.QT_ATENDIMENTO AS qt_atendimento,
           e.CD_PRESTADOR AS cd_prestador,
           p.NM_PRESTADOR AS nm_prestador,
           e.CD_UNIDADE_ATENDIMENTO AS cd_unidade_atendimento,
           u.DS_UNIDADE_ATENDIMENTO AS ds_unidade_atendimento,
           e.CD_RECURSO_CENTRAL AS cd_recurso_central,
           e.CD_SETOR AS cd_setor,
           s.NM_SETOR AS nm_setor,
           e.CD_MULTI_EMPRESA AS cd_multi_empresa
      FROM DBAMV.ESCALA_CENTRAL e
      LEFT JOIN DBAMV.PRESTADOR p ON p.CD_PRESTADOR = e.CD_PRESTADOR
      LEFT JOIN DBAMV.UNIDADE_ATENDIMENTO u
        ON u.CD_UNIDADE_ATENDIMENTO = e.CD_UNIDADE_ATENDIMENTO
      LEFT JOIN DBAMV.SETOR s ON s.CD_SETOR = e.CD_SETOR
     WHERE e.TP_ESCALA = 'A'
       AND NVL(e.SN_ATIVO, 'S') = 'S'
"""


def tp_dia_semana_de(data_agenda: date) -> int:
    """Converte a data para o TP_DIA_SEMANA do SoulMV."""
    return (data_agenda.isoweekday() % 7) + 1


def dias_geracao(tp_dia_semana: int) -> list[str]:
    """Valores de P_CHK_SEGUNDA..P_CHK_DOMINGO de FNC_GERA_AGENDA."""
    ordem = (2, 3, 4, 5, 6, 7, 1)
    return [str(dia) if dia == tp_dia_semana else '0' for dia in ordem]


def plano_geracao(
    escala: dict, cd_prestador: int, data_agenda: date, usuario_mv: str
) -> dict:
    """O que seria gravado no SoulMV, na ordem, para revisão humana."""
    return {
        'modo': 'simulacao',
        'passos': [
            {
                'passo': 1,
                'descricao': (
                    'Nova ESCALA_CENTRAL do médico, copiando a configuração '
                    'da escala-modelo (setor, unidade, horários, encaixes).'
                ),
                'escala_modelo': escala['cd_escala_central'],
                'CD_PRESTADOR': cd_prestador,
                'SN_AGENDA_PUBLICA': 'N',
            },
            {
                'passo': 2,
                'descricao': (
                    'Copiar a configuração da escala-modelo: '
                    'ESCALA_CENTRAL_CONVENIO, ESCALA_CENTRAL_ITEM_AGENDA, '
                    'ESCALA_CENTRAL_SER_TIPO, ESCALA_CENTRAL_HORARIO e '
                    'ESCALA_CENTRAL_USUARIO.'
                ),
            },
            {
                'passo': 3,
                'descricao': (
                    'GERACAO_AGENDA + IT_GERACAO_AGENDA e chamada de '
                    'DBAMV.PKG_SCMA_M_GERA_AGENDA.FNC_GERA_AGENDA, como no '
                    'job oficial PRC_SCMA_GERAR_AGENDA (retorno 1 = sucesso).'
                ),
                'parametros': {
                    'P_TP_ESCALA': 'A',
                    'P_CD_PRESTADOR': cd_prestador,
                    'P_CD_UNIDADE_ATENDIMENTO': escala[
                        'cd_unidade_atendimento'
                    ],
                    'P_CD_RECURSO_CENTRAL': escala['cd_recurso_central'],
                    'P_CD_SETOR': escala['cd_setor'],
                    'P_CHK_DIAS': dias_geracao(escala['tp_dia_semana']),
                    'P_CD_MULTIEMPRESA': escala['cd_multi_empresa'],
                    'P_DT_INICIO': data_agenda.isoformat(),
                    'P_DT_FIM': data_agenda.isoformat(),
                    'P_SN_SOBREPOR': 'N',
                    'P_USUARIO': usuario_mv,
                    'P_TP_PERIODO': 'S',
                },
            },
        ],
        'transacao': (
            'Uma única transação; FNC_GERA_AGENDA não faz COMMIT própria. '
            'Rollback em qualquer falha. Nunca alterar IT_AGENDA_CENTRAL '
            'diretamente (12 triggers, incluindo a jornada de WhatsApp).'
        ),
    }


class AgendaMedicaSoulMV:
    """Consultas somente leitura ao SoulMV."""

    def __init__(self, session: Session):
        self.session = session

    def _uma(self, sql: str, binds: dict) -> dict | None:
        row = self.session.execute(text(sql), binds).mappings().first()
        return dict(row) if row else None

    def _varias(self, sql: str, binds: dict) -> list[dict]:
        rows = self.session.execute(text(sql), binds).mappings().all()
        return [dict(row) for row in rows]

    def escala_modelo(self, cd_escala: int) -> dict | None:
        return self._uma(
            f'{CONSULTA_ESCALA} AND e.CD_ESCALA_CENTRAL = :cd',
            {'cd': cd_escala},
        )

    def escalas_modelo(
        self, cd_unidade: int | None, tp_dia_semana: int | None
    ) -> list[dict]:
        filtros = ''
        binds: dict = {}
        if cd_unidade is not None:
            filtros += ' AND e.CD_UNIDADE_ATENDIMENTO = :unidade'
            binds['unidade'] = cd_unidade
        if tp_dia_semana is not None:
            filtros += ' AND e.TP_DIA_SEMANA = :dia'
            binds['dia'] = tp_dia_semana
        return self._varias(
            f'{CONSULTA_ESCALA}{filtros} ORDER BY e.TP_DIA_SEMANA, '
            'e.HR_INICIO',
            binds,
        )

    def prestador(self, cd_prestador: int) -> dict | None:
        return self._uma(
            """
            SELECT CD_PRESTADOR AS cd_prestador,
                   NM_PRESTADOR AS nm_prestador
              FROM DBAMV.PRESTADOR
             WHERE CD_PRESTADOR = :cd
            """,
            {'cd': cd_prestador},
        )

    def especialidades_prestador(self, cd_prestador: int) -> list[str]:
        rows = self._varias(
            """
            SELECT TRIM(e.DS_ESPECIALID) AS especialidade
              FROM DBAMV.ESP_MED em
              JOIN DBAMV.ESPECIALID e ON e.CD_ESPECIALID = em.CD_ESPECIALID
             WHERE em.CD_PRESTADOR = :cd
               AND NVL(e.SN_ATIVO, 'S') = 'S'
            """,
            {'cd': cd_prestador},
        )
        return [row['especialidade'] for row in rows]

    def conflitos(
        self, cd_prestador: int, data_agenda: date, inicio: str, fim: str
    ) -> list[dict]:
        return self._varias(
            """
            SELECT CD_AGENDA_CENTRAL AS cd_agenda_central,
                   TO_CHAR(HR_INICIO, 'HH24:MI') AS hr_inicio,
                   TO_CHAR(HR_FIM, 'HH24:MI') AS hr_fim
              FROM DBAMV.AGENDA_CENTRAL
             WHERE CD_PRESTADOR = :cd
               AND DT_AGENDA = :data
               AND NVL(SN_ATIVO, 'S') = 'S'
               AND TO_CHAR(HR_INICIO, 'HH24:MI') < :fim
               AND TO_CHAR(HR_FIM, 'HH24:MI') > :inicio
            """,
            {
                'cd': cd_prestador,
                'data': data_agenda,
                'inicio': inicio,
                'fim': fim,
            },
        )

    def agenda(self, cd_agenda_central: int) -> dict | None:
        return self._uma(
            """
            SELECT a.CD_AGENDA_CENTRAL AS cd_agenda_central,
                   a.CD_PRESTADOR AS cd_prestador,
                   a.DT_AGENDA AS dt_agenda,
                   TO_CHAR(a.HR_INICIO, 'HH24:MI') AS hr_inicio,
                   TO_CHAR(a.HR_FIM, 'HH24:MI') AS hr_fim,
                   NVL(a.QT_ATENDIMENTO, 0) AS qt_atendimento,
                   NVL(a.QT_MARCADOS, 0) AS qt_marcados,
                   u.DS_UNIDADE_ATENDIMENTO AS ds_unidade_atendimento
              FROM DBAMV.AGENDA_CENTRAL a
              LEFT JOIN DBAMV.UNIDADE_ATENDIMENTO u
                ON u.CD_UNIDADE_ATENDIMENTO = a.CD_UNIDADE_ATENDIMENTO
             WHERE a.CD_AGENDA_CENTRAL = :cd
            """,
            {'cd': cd_agenda_central},
        )
