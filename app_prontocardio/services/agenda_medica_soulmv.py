"""Leituras do SoulMV para a Agenda Médica (somente leitura).

Escalas-modelo, especialidades, conflitos de horário e agendas do prestador.
A gravação no SoulMV não é feita aqui: `plano_geracao` descreve exatamente o
que a rotina oficial da MV (PKG_SCMA_M_GERA_AGENDA) receberia, para revisão.
"""

import html
import re
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


# ---------------------------------------------------------------- triagem

# Consultas ambulatoriais com paciente nos próximos dias, com o último
# atendimento ambulatorial anterior de cada paciente (base para a triagem dos
# médicos sêniores). Sem telefone, observações livres ou usuário que agendou.
CONSULTA_TRIAGEM = """
    WITH agenda AS (
        SELECT i.CD_IT_AGENDA_CENTRAL, i.CD_AGENDA_CENTRAL, i.HR_AGENDA,
               i.CD_PACIENTE, i.NM_PACIENTE,
               CASE WHEN INSTR(UPPER(NVL(i.DS_OBSERVACAO_GERAL, '')),
                               'CONFIRMADO') > 0
                    THEN 'S' ELSE 'N' END CONFIRMADO,
               c.NM_CONVENIO, ia.DS_ITEM_AGENDAMENTO, tm.DS_TIP_MAR,
               a.CD_PRESTADOR CD_PRESTADOR_AGENDA,
               pr.NM_PRESTADOR NM_PRESTADOR_AGENDA,
               a.CD_SETOR, s.NM_SETOR,
               CASE WHEN a.HR_INICIO IS NULL THEN NULL
                    WHEN TO_NUMBER(TO_CHAR(a.HR_INICIO, 'HH24')) < 12
                         THEN 'MANHA'
                    WHEN TO_NUMBER(TO_CHAR(a.HR_INICIO, 'HH24')) < 18
                         THEN 'TARDE'
                    ELSE 'NOITE' END TURNO
          FROM DBAMV.IT_AGENDA_CENTRAL i
          JOIN DBAMV.AGENDA_CENTRAL a
            ON a.CD_AGENDA_CENTRAL = i.CD_AGENDA_CENTRAL
          LEFT JOIN DBAMV.PRESTADOR pr ON pr.CD_PRESTADOR = a.CD_PRESTADOR
          LEFT JOIN DBAMV.CONVENIO c ON c.CD_CONVENIO = i.CD_CONVENIO
          LEFT JOIN DBAMV.ITEM_AGENDAMENTO ia
            ON ia.CD_ITEM_AGENDAMENTO = i.CD_ITEM_AGENDAMENTO
          LEFT JOIN DBAMV.TIP_MAR tm ON tm.CD_TIP_MAR = i.CD_TIP_MAR
          LEFT JOIN DBAMV.SETOR s ON s.CD_SETOR = a.CD_SETOR
         WHERE i.HR_AGENDA >= TRUNC(SYSDATE)
           AND i.HR_AGENDA < TRUNC(SYSDATE) + :dias + 1
           AND i.CD_PACIENTE IS NOT NULL
           AND NVL(i.SN_BLOQUEADO, 'N') = 'N'
           AND a.TP_AGENDA = 'A'
           AND NVL(a.CD_PRESTADOR, -1) NOT IN (
                SELECT g.CD_PRESTADOR FROM DBAMV.PRESTADOR g
                 WHERE UPPER(g.NM_PRESTADOR) LIKE '%GENERIC%')
           {filtro}
    ),
    ultimo_ambulatorial AS (
        SELECT ag.CD_IT_AGENDA_CENTRAL,
               TRUNC(ant.DT_ATENDIMENTO)
                 + (NVL(ant.HR_ATENDIMENTO, ant.DT_ATENDIMENTO)
                    - TRUNC(NVL(ant.HR_ATENDIMENTO, ant.DT_ATENDIMENTO)))
                 DH_ULT_ATEND_AMB,
               ant.CD_PRESTADOR CD_PRESTADOR_ULT_AMB,
               pr_ant.NM_PRESTADOR NM_PRESTADOR_ULT_AMB,
               ROW_NUMBER() OVER (
                   PARTITION BY ag.CD_IT_AGENDA_CENTRAL
                   ORDER BY TRUNC(ant.DT_ATENDIMENTO)
                              + (NVL(ant.HR_ATENDIMENTO, ant.DT_ATENDIMENTO)
                                 - TRUNC(NVL(ant.HR_ATENDIMENTO,
                                             ant.DT_ATENDIMENTO))) DESC,
                            ant.CD_ATENDIMENTO DESC) RN
          FROM agenda ag
          JOIN DBAMV.ATENDIME ant
            ON ant.CD_PACIENTE = ag.CD_PACIENTE
           AND ant.TP_ATENDIMENTO = 'A'
           AND TRUNC(ant.DT_ATENDIMENTO)
                 + (NVL(ant.HR_ATENDIMENTO, ant.DT_ATENDIMENTO)
                    - TRUNC(NVL(ant.HR_ATENDIMENTO, ant.DT_ATENDIMENTO)))
               < ag.HR_AGENDA
          LEFT JOIN DBAMV.PRESTADOR pr_ant
            ON pr_ant.CD_PRESTADOR = ant.CD_PRESTADOR
    )
    SELECT ag.CD_IT_AGENDA_CENTRAL AS cd_it_agenda_central,
           ag.CD_AGENDA_CENTRAL AS cd_agenda_central,
           TO_CHAR(ag.HR_AGENDA, 'YYYY-MM-DD"T"HH24:MI:SS') AS hr_agenda,
           ag.CD_PACIENTE AS cd_paciente,
           ag.NM_PACIENTE AS nm_paciente,
           ag.CONFIRMADO AS confirmado,
           ag.NM_CONVENIO AS nm_convenio,
           ag.DS_ITEM_AGENDAMENTO AS ds_item_agendamento,
           ag.DS_TIP_MAR AS ds_tip_mar,
           ag.CD_PRESTADOR_AGENDA AS cd_prestador_agenda,
           ag.NM_PRESTADOR_AGENDA AS nm_prestador_agenda,
           ag.CD_SETOR AS cd_setor,
           ag.NM_SETOR AS nm_setor,
           ag.TURNO AS turno,
           TO_CHAR(ua.DH_ULT_ATEND_AMB, 'YYYY-MM-DD"T"HH24:MI:SS')
               AS dh_ult_atend_amb,
           ua.CD_PRESTADOR_ULT_AMB AS cd_prestador_ult_amb,
           ua.NM_PRESTADOR_ULT_AMB AS nm_prestador_ult_amb,
           CAST(ROUND(ag.HR_AGENDA - ua.DH_ULT_ATEND_AMB, 0) AS NUMBER(12))
               AS dias_desde_ult_atend_amb
      FROM agenda ag
      LEFT JOIN ultimo_ambulatorial ua
        ON ua.CD_IT_AGENDA_CENTRAL = ag.CD_IT_AGENDA_CENTRAL AND ua.RN = 1
     ORDER BY ag.HR_AGENDA, ag.NM_PACIENTE
"""

# Evolução médica do PEP: documento tipo 36, texto em PRE_MED.DS_EVOLUCAO.
# Só documentos fechados, não confidenciais e de evolução médica ('M').
CONSULTA_ULTIMAS_EVOLUCOES = """
    SELECT pm.CD_PRE_MED AS cd_pre_med,
           TO_CHAR(NVL(dc.DH_REFERENCIA, dc.DH_CRIACAO),
                   'YYYY-MM-DD"T"HH24:MI') AS data,
           pr.NM_PRESTADOR AS medico,
           a.TP_ATENDIMENTO AS tp_atendimento,
           s.NM_SETOR AS setor
      FROM DBAMV.PW_DOCUMENTO_CLINICO dc
      JOIN DBAMV.PRE_MED pm
        ON pm.CD_DOCUMENTO_CLINICO = dc.CD_DOCUMENTO_CLINICO
      JOIN DBAMV.ATENDIME a ON a.CD_ATENDIMENTO = dc.CD_ATENDIMENTO
      LEFT JOIN DBAMV.PRESTADOR pr ON pr.CD_PRESTADOR = dc.CD_PRESTADOR
      LEFT JOIN DBAMV.SETOR s ON s.CD_SETOR = dc.CD_SETOR
     WHERE dc.CD_PACIENTE = :cd_paciente
       AND dc.CD_TIPO_DOCUMENTO = 36
       AND dc.TP_STATUS = 'FECHADO'
       AND NVL(dc.SN_CONFIDENCIAL, 'N') <> 'S'
       AND pm.TP_PRE_MED = 'M'
     ORDER BY NVL(dc.DH_REFERENCIA, dc.DH_CRIACAO) DESC,
              dc.CD_DOCUMENTO_CLINICO DESC
     FETCH FIRST :quantidade ROWS ONLY
"""
TIPOS_ATENDIMENTO = {
    'A': 'Ambulatorial',
    'I': 'Internação',
    'U': 'Urgência',
    'E': 'Externo',
    'H': 'Home care',
}
LIMITE_TEXTO_EVOLUCAO = 20000
_GRUPOS_RTF_SEM_TEXTO = re.compile(
    r'\{\\(\*|fonttbl|colortbl|stylesheet|info|pict|header|footer)'
)


def _sem_grupos_rtf(rtf: str) -> str:
    """Remove do RTF os grupos que não são texto (fontes, cores, estilos)."""
    saida, i = [], 0
    while i < len(rtf):
        if _GRUPOS_RTF_SEM_TEXTO.match(rtf, i):
            profundidade = 0
            while i < len(rtf):
                if rtf[i] == '\\':
                    i += 2
                    continue
                profundidade += {'{': 1, '}': -1}.get(rtf[i], 0)
                i += 1
                if profundidade == 0:
                    break
            continue
        saida.append(rtf[i])
        i += 1
    return ''.join(saida)


def texto_evolucao(bruto: str | None) -> str:
    """Converte o texto da evolução (RTF, HTML ou simples) em texto simples."""
    texto = bruto or ''
    if texto.lstrip().startswith('{\\rtf'):
        texto = re.sub(
            r"\\'([0-9a-fA-F]{2})",
            lambda m: bytes.fromhex(m.group(1)).decode('cp1252', 'replace'),
            texto,
        )
        texto = _sem_grupos_rtf(texto)
        texto = re.sub(r'\\(par|line)\b ?', '\n', texto)
        texto = re.sub(r'\\tab\b ?', '\t', texto)
        texto = re.sub(r'\\[a-zA-Z]+-?\d* ?', '', texto)
        texto = (
            texto.replace('\\{', '{').replace('\\}', '}').replace('\\\\', '\\')
        )
        texto = re.sub(r'[{}]', '', texto)
    elif re.search(r'<(p|br|div|span|b|strong|table|li)\b', texto, re.I):
        texto = re.sub(
            r'<\s*(br|/p|/div|/li|/tr)\s*/?>', '\n', texto, flags=re.I
        )
        texto = re.sub(r'<[^>]+>', '', texto)
        texto = html.unescape(texto)
    texto = texto.replace('\r\n', '\n').replace('\r', '\n')
    texto = re.sub(r'[ \t]+\n', '\n', texto)
    texto = re.sub(r'\n{3,}', '\n\n', texto).strip()
    return texto[:LIMITE_TEXTO_EVOLUCAO]


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

    def triagem_pacientes(self, dias: int) -> list[dict]:
        return self._varias(CONSULTA_TRIAGEM.format(filtro=''), {'dias': dias})

    def item_triagem(
        self, cd_it_agenda_central: int, dias: int
    ) -> dict | None:
        itens = self._varias(
            CONSULTA_TRIAGEM.format(
                filtro='AND i.CD_IT_AGENDA_CENTRAL = :cd_it'
            ),
            {'dias': dias, 'cd_it': cd_it_agenda_central},
        )
        return itens[0] if itens else None

    def paciente_na_triagem(self, cd_paciente: int, dias: int) -> bool:
        return bool(
            self._uma(
                """
                SELECT 1 AS ok
                  FROM DBAMV.IT_AGENDA_CENTRAL i
                  JOIN DBAMV.AGENDA_CENTRAL a
                    ON a.CD_AGENDA_CENTRAL = i.CD_AGENDA_CENTRAL
                 WHERE i.CD_PACIENTE = :cd_paciente
                   AND i.HR_AGENDA >= TRUNC(SYSDATE)
                   AND i.HR_AGENDA < TRUNC(SYSDATE) + :dias + 1
                   AND NVL(i.SN_BLOQUEADO, 'N') = 'N'
                   AND a.TP_AGENDA = 'A'
                 FETCH FIRST 1 ROWS ONLY
                """,
                {'cd_paciente': cd_paciente, 'dias': dias},
            )
        )

    def evolucoes_paciente(
        self, cd_paciente: int, quantidade: int = 2
    ) -> list:
        linhas = self._varias(
            CONSULTA_ULTIMAS_EVOLUCOES,
            {'cd_paciente': cd_paciente, 'quantidade': quantidade},
        )
        evolucoes = []
        for linha in linhas:
            # DS_EVOLUCAO é LONG: lido em separado, fora da ordenação/limite.
            registro = self._uma(
                'SELECT DS_EVOLUCAO AS texto FROM DBAMV.PRE_MED '
                'WHERE CD_PRE_MED = :cd',
                {'cd': linha['cd_pre_med']},
            )
            evolucoes.append({
                'data': linha['data'],
                'medico': linha['medico'],
                'atendimento': TIPOS_ATENDIMENTO.get(
                    linha['tp_atendimento'], linha['tp_atendimento']
                ),
                'setor': linha['setor'],
                'texto': texto_evolucao(
                    registro['texto'] if registro else None
                ),
            })
        return evolucoes
