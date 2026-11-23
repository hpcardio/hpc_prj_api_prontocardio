import re
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import text

from app_prontocardio.central_atendimento_schema import (
    OrigemFila,
    PatientReference,
    PatientSearchResponse,
    QueueItem,
    QueueResponse,
    SourceFreshness,
)

FORBIDDEN_SQL = re.compile(
    r'\b(INSERT|UPDATE|DELETE|MERGE|ALTER|DROP|CREATE|TRUNCATE|BEGIN|CALL)\b',
    re.IGNORECASE,
)
PATIENT_SEARCH_SQL = """
SELECT
    cd_paciente AS patient_code,
    dt_nascimento AS birth_date
FROM DBAMV.PACIENTE
WHERE UPPER(nm_paciente) LIKE :criterion
FETCH FIRST 20 ROWS ONLY
"""

APPOINTMENTS_SQL = """
SELECT /* CENTRAL_AGENDAMENTOS */
       i.CD_IT_AGENDA_CENTRAL AS source_code,
       i.CD_PACIENTE AS patient_code,
       NVL(p.NM_PACIENTE, i.NM_PACIENTE) AS patient_name,
       CASE WHEN ac.CD_PRESTADOR IS NULL THEN 'EXAME'
            ELSE 'AMBULATORIAL' END AS attendance_type,
       ac.CD_UNIDADE_ATENDIMENTO AS unit_code,
       i.HR_AGENDA AS scheduled_at,
       i.CD_ATENDIMENTO AS attendance_code,
       CASE WHEN a.CD_ATENDIMENTO IS NOT NULL
            THEN a.DT_ATENDIMENTO
               + (a.HR_ATENDIMENTO - TRUNC(a.HR_ATENDIMENTO))
       END AS attendance_opened_at,
       CASE WHEN a.DT_ALTA IS NOT NULL
            THEN a.DT_ALTA
               + (NVL(a.HR_ALTA, a.DT_ALTA)
                  - TRUNC(NVL(a.HR_ALTA, a.DT_ALTA)))
       END AS attendance_closed_at,
       i.CD_ITEM_AGENDAMENTO AS item_code,
       NVL(a.CD_CONVENIO, i.CD_CONVENIO) AS insurance_code,
       c.NM_CONVENIO AS insurance_name,
       NVL(a.CD_CON_PLA, i.CD_CON_PLA) AS plan_code,
       cp.DS_CON_PLA AS plan_name,
       NVL(i.DS_OBSERVACAO, i.DS_OBSERVACAO_GERAL) AS appointment_observation,
       o.DS_ORI_ATE AS mv_origin_name
  FROM DBAMV.IT_AGENDA_CENTRAL i
  JOIN DBAMV.AGENDA_CENTRAL ac
    ON ac.CD_AGENDA_CENTRAL = i.CD_AGENDA_CENTRAL
  LEFT JOIN DBAMV.PACIENTE p
    ON p.CD_PACIENTE = i.CD_PACIENTE
  LEFT JOIN DBAMV.ATENDIME a
    ON a.CD_ATENDIMENTO = i.CD_ATENDIMENTO
  LEFT JOIN DBAMV.CONVENIO c
    ON c.CD_CONVENIO = NVL(a.CD_CONVENIO, i.CD_CONVENIO)
  LEFT JOIN DBAMV.CON_PLA cp
    ON cp.CD_CONVENIO = NVL(a.CD_CONVENIO, i.CD_CONVENIO)
   AND cp.CD_CON_PLA = NVL(a.CD_CON_PLA, i.CD_CON_PLA)
  LEFT JOIN DBAMV.ORI_ATE o
    ON o.CD_ORI_ATE = a.CD_ORI_ATE
 WHERE i.HR_AGENDA >= :start_at
   AND i.HR_AGENDA < :end_at
   AND i.CD_PACIENTE IS NOT NULL
   AND i.TP_SITUACAO IN ('A', 'E')
   AND ac.CD_UNIDADE_ATENDIMENTO IS NOT NULL
 ORDER BY i.HR_AGENDA, i.CD_IT_AGENDA_CENTRAL
 FETCH FIRST 1000 ROWS ONLY
"""

TICKETS_SQL = """
SELECT /* CENTRAL_SENHAS */
       t.CD_TRIAGEM_ATENDIMENTO AS source_code,
       NVL(t.CD_PACIENTE, a.CD_PACIENTE) AS patient_code,
       NVL(t.NM_PACIENTE, p.NM_PACIENTE) AS patient_name,
       'URGENCIA' AS attendance_type,
       t.CD_MULTI_EMPRESA AS unit_code,
       t.DH_PRE_ATENDIMENTO AS scheduled_at,
       t.CD_ATENDIMENTO AS attendance_code,
       CASE WHEN a.CD_ATENDIMENTO IS NOT NULL
            THEN a.DT_ATENDIMENTO
               + (a.HR_ATENDIMENTO - TRUNC(a.HR_ATENDIMENTO))
       END AS attendance_opened_at,
       CASE WHEN a.DT_ALTA IS NOT NULL
            THEN a.DT_ALTA
               + (NVL(a.HR_ALTA, a.DT_ALTA)
                  - TRUNC(NVL(a.HR_ALTA, a.DT_ALTA)))
       END AS attendance_closed_at,
       a.CD_CONVENIO AS insurance_code,
       c.NM_CONVENIO AS insurance_name,
       a.CD_CON_PLA AS plan_code,
       cp.DS_CON_PLA AS plan_name,
       o.DS_ORI_ATE AS mv_origin_name,
       t.DS_SENHA AS ticket_label,
       t.CD_CLASSIFICACAO AS risk_code
  FROM DBAMV.TRIAGEM_ATENDIMENTO t
  LEFT JOIN DBAMV.ATENDIME a
    ON a.CD_ATENDIMENTO = t.CD_ATENDIMENTO
  LEFT JOIN DBAMV.PACIENTE p
    ON p.CD_PACIENTE = NVL(t.CD_PACIENTE, a.CD_PACIENTE)
  LEFT JOIN DBAMV.CONVENIO c
    ON c.CD_CONVENIO = a.CD_CONVENIO
  LEFT JOIN DBAMV.CON_PLA cp
    ON cp.CD_CONVENIO = a.CD_CONVENIO
   AND cp.CD_CON_PLA = a.CD_CON_PLA
  LEFT JOIN DBAMV.ORI_ATE o
    ON o.CD_ORI_ATE = a.CD_ORI_ATE
 WHERE t.DH_PRE_ATENDIMENTO >= :start_at
   AND t.DH_PRE_ATENDIMENTO < :end_at
   AND t.CD_MULTI_EMPRESA IS NOT NULL
 ORDER BY t.DH_PRE_ATENDIMENTO, t.CD_TRIAGEM_ATENDIMENTO
 FETCH FIRST 1000 ROWS ONLY
"""


class ReadOnlyViolation(RuntimeError):
    pass


class OracleCentralReadProvider:
    def __init__(self, readonly_engine):
        self._engine = readonly_engine

    def execute_read(self, sql: str, parameters: dict) -> list[dict]:
        normalized = sql.lstrip()
        first_token = normalized.partition(' ')[0].partition('\n')[0].upper()
        if first_token not in {'SELECT', 'WITH'} or FORBIDDEN_SQL.search(sql):
            raise ReadOnlyViolation('Only read-only SELECT statements allowed')
        with self._engine.connect() as connection:
            result = connection.execute(text(sql), parameters)
            return [dict(row) for row in result.mappings().all()]

    def search_patients(self, criterion: str) -> PatientSearchResponse:
        rows = self.execute_read(
            PATIENT_SEARCH_SQL,
            {'criterion': f'%{criterion.upper()}%'},
        )
        items = [
            PatientReference(
                patient_code=row['patient_code'],
                birth_date=self._date_text(row.get('birth_date')),
            )
            for row in rows
        ]
        return PatientSearchResponse(
            items=items,
            ambiguous=len(items) > 1,
        )

    def queue(self) -> QueueResponse:
        now = datetime.now(UTC)
        local_now = now.astimezone(ZoneInfo('America/Sao_Paulo'))
        start_at = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        parameters = {
            'start_at': start_at.replace(tzinfo=None),
            'end_at': (start_at + timedelta(days=1)).replace(tzinfo=None),
        }
        items: list[QueueItem] = []
        freshness: list[SourceFreshness] = []
        for source, sql in (
            (OrigemFila.AGENDAMENTO, APPOINTMENTS_SQL),
            (OrigemFila.SENHA, TICKETS_SQL),
        ):
            checked_at = datetime.now(UTC)
            try:
                rows = self.execute_read(sql, parameters)
                items.extend(self._queue_item(source, row) for row in rows)
                freshness.append(
                    SourceFreshness(
                        source=source,
                        checked_at=checked_at,
                        stale=False,
                        available=True,
                        code='OK',
                    )
                )
            except Exception:
                freshness.append(
                    SourceFreshness(
                        source=source,
                        checked_at=checked_at,
                        stale=True,
                        available=False,
                        code='QUERY_FAILED',
                    )
                )
        for source in (OrigemFila.AVULSO, OrigemFila.CLASSIFICACAO_RISCO):
            freshness.append(
                SourceFreshness(
                    source=source,
                    checked_at=datetime.now(UTC),
                    stale=True,
                    available=False,
                    code='SOURCE_NOT_MAPPED',
                )
            )
        return QueueResponse(items=items, freshness=freshness)

    @classmethod
    def _queue_item(cls, source: OrigemFila, row: dict) -> QueueItem:
        def value(name: str):
            return row.get(name, row.get(name.upper()))

        return QueueItem(
            source=source,
            source_code=value('source_code'),
            patient_code=value('patient_code'),
            patient_name=value('patient_name'),
            attendance_type=value('attendance_type'),
            unit_code=value('unit_code'),
            scheduled_at=value('scheduled_at'),
            attendance_code=value('attendance_code'),
            attendance_opened_at=value('attendance_opened_at'),
            attendance_closed_at=value('attendance_closed_at'),
            item_code=value('item_code'),
            insurance_code=value('insurance_code'),
            insurance_name=value('insurance_name'),
            plan_code=value('plan_code'),
            plan_name=value('plan_name'),
            appointment_clinic_name=cls._appointment_clinic(
                value('appointment_observation')
            ),
            mv_origin_clinic_name=cls._mv_origin_clinic(
                value('mv_origin_name')
            ),
            ticket_label=value('ticket_label'),
            risk_code=value('risk_code'),
        )

    @staticmethod
    def _appointment_clinic(observation: str | None) -> str | None:
        text_value = str(observation or '').strip()
        if not text_value:
            return None
        match = re.search(
            r'cl[ií]nica\s+parceira\s*:\s*([^|\r\n]+)',
            text_value,
            re.IGNORECASE,
        )
        if match is None:
            match = re.search(
                r'prontocardio\s+rede\s*-\s*([^|\r\n]+)',
                text_value,
                re.IGNORECASE,
            )
        return ' '.join(match.group(1).split()) if match else None

    @staticmethod
    def _mv_origin_clinic(origin_name: str | None) -> str | None:
        text_value = ' '.join(str(origin_name or '').split())
        match = re.match(
            r'^P\.?RED(?:E)?\s*-\s*(.+?)\s*-\s*'
            r'(?:EXA|AMB|INT|URG)\s*$',
            text_value,
            re.IGNORECASE,
        )
        return match.group(1).strip() if match else None

    @staticmethod
    def _date_text(value: date | datetime | str | None) -> str | None:
        if value is None or isinstance(value, str):
            return value
        return value.isoformat()
