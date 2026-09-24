from datetime import datetime

from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError, TimeoutError

from app_prontocardio.ecg_worklist_schema import (
    SearchType,
    WorklistItem,
    WorklistOrderItem,
    WorklistOrdersRequest,
    WorklistSearchRequest,
    WorklistSector,
)

MAX_RESULTS = 10
MAX_ORDER_RESULTS = 500


class OracleWorklistError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(f'Falha na consulta ECG ao MV ({code}).')


class OracleWorklistClient:
    def __init__(self, engine: Engine, procedure_code: int):
        self.engine = engine
        self.procedure_code = procedure_code

    def search(self, request: WorklistSearchRequest) -> list[WorklistItem]:
        if not request.identifier.isdecimal():
            return []

        filter_column = (
            'p.cd_ped_rx'
            if request.search_type is SearchType.ORDER
            else 'p.cd_atendimento'
        )
        statement = text(
            f"""
            SELECT *
              FROM (
                    SELECT TO_CHAR(p.cd_ped_rx) AS accession_number,
                           TO_CHAR(a.cd_paciente) AS patient_id,
                           NVL(
                               NULLIF(TRIM(pa.nm_social_paciente), ''),
                               TRIM(pa.nm_paciente)
                           ) AS patient_name,
                           NVL(p.hr_pedido, p.dt_pedido) AS scheduled_at,
                           pa.dt_nascimento AS patient_birth_date,
                           pa.tp_sexo AS patient_sex,
                           TO_CHAR(i.cd_exa_rx) AS procedure_code,
                           TRIM(e.ds_exa_rx) AS procedure_description,
                           TO_CHAR(p.cd_atendimento)
                               AS requested_procedure_id,
                           TO_CHAR(i.cd_itped_rx)
                               AS scheduled_procedure_step_id
                      FROM dbamv.ped_rx p
                      JOIN dbamv.atendime a
                        ON a.cd_atendimento = p.cd_atendimento
                      JOIN dbamv.paciente pa
                        ON pa.cd_paciente = a.cd_paciente
                      JOIN dbamv.itped_rx i
                        ON i.cd_ped_rx = p.cd_ped_rx
                     JOIN dbamv.exa_rx e
                        ON e.cd_exa_rx = i.cd_exa_rx
                     WHERE {filter_column} = :identifier
                       AND i.cd_exa_rx = :procedure_code
                       AND NVL(i.sn_realizado, 'N') <> 'S'
                       AND i.dt_realizado IS NULL
                     ORDER BY p.cd_ped_rx DESC, i.cd_itped_rx
                   )
             WHERE ROWNUM <= :max_results
            """
        )
        try:
            with self.engine.connect() as connection:
                rows = (
                    connection
                    .execute(
                        statement,
                        {
                            'identifier': int(request.identifier),
                            'max_results': MAX_RESULTS,
                            'procedure_code': self.procedure_code,
                        },
                    )
                    .mappings()
                    .all()
                )
        except TimeoutError as exc:
            raise OracleWorklistError('MV_TIMEOUT') from exc
        except SQLAlchemyError as exc:
            raise OracleWorklistError('MV_UNAVAILABLE') from exc

        return [item for row in rows if (item := self._normalizar(row))]

    def health(self) -> None:
        try:
            with self.engine.connect() as connection:
                connection.execute(text('SELECT 1 FROM dual'))
        except TimeoutError as exc:
            raise OracleWorklistError('MV_TIMEOUT') from exc
        except SQLAlchemyError as exc:
            raise OracleWorklistError('MV_UNAVAILABLE') from exc

    def list_orders(
        self, request: WorklistOrdersRequest
    ) -> list[WorklistOrderItem]:
        statement = text(
            """
            SELECT *
              FROM (
                    SELECT TO_CHAR(p.cd_ped_rx) AS accession_number,
                           TO_CHAR(p.cd_atendimento) AS attendance_number,
                           TO_CHAR(a.cd_paciente) AS patient_id,
                           NVL(
                               NULLIF(TRIM(pa.nm_social_paciente), ''),
                               TRIM(pa.nm_paciente)
                           ) AS patient_name,
                           pa.dt_nascimento AS patient_birth_date,
                           NVL(p.hr_pedido, p.dt_pedido) AS requested_at,
                           TO_CHAR(i.cd_exa_rx) AS procedure_code,
                           TO_CHAR(i.cd_itped_rx)
                               AS scheduled_procedure_step_id,
                           TRIM(e.ds_exa_rx) AS procedure_description,
                           TO_CHAR(NVL(ui.cd_setor, p.cd_setor))
                               AS current_sector_code,
                           TRIM(s.nm_setor) AS current_sector_name,
                           CASE
                               WHEN NVL(i.sn_realizado, 'N') = 'S'
                                 OR i.dt_realizado IS NOT NULL
                               THEN 1 ELSE 0
                           END AS mv_realized,
                           i.dt_realizado AS mv_realized_at
                      FROM dbamv.ped_rx p
                      JOIN dbamv.atendime a
                        ON a.cd_atendimento = p.cd_atendimento
                      JOIN dbamv.paciente pa
                        ON pa.cd_paciente = a.cd_paciente
                      JOIN dbamv.itped_rx i
                        ON i.cd_ped_rx = p.cd_ped_rx
                      JOIN dbamv.exa_rx e
                        ON e.cd_exa_rx = i.cd_exa_rx
                      LEFT JOIN dbamv.leito l
                        ON l.cd_leito = a.cd_leito
                      LEFT JOIN dbamv.unid_int ui
                        ON ui.cd_unid_int = l.cd_unid_int
                      LEFT JOIN dbamv.setor s
                        ON s.cd_setor = NVL(ui.cd_setor, p.cd_setor)
                     WHERE p.dt_pedido >= :date_from
                       AND p.dt_pedido < :date_to_exclusive
                       AND i.cd_exa_rx = :procedure_code
                     ORDER BY p.dt_pedido,
                              p.cd_ped_rx,
                              i.cd_itped_rx
                   )
             WHERE ROWNUM <= :max_results
            """
        )
        try:
            with self.engine.connect() as connection:
                rows = (
                    connection.execute(
                        statement,
                        {
                            'date_from': request.date_from,
                            'date_to_exclusive': request.date_to_exclusive,
                            'procedure_code': self.procedure_code,
                            'max_results': MAX_ORDER_RESULTS + 1,
                        },
                    )
                    .mappings()
                    .all()
                )
        except TimeoutError as exc:
            raise OracleWorklistError('MV_TIMEOUT') from exc
        except SQLAlchemyError as exc:
            raise OracleWorklistError('MV_UNAVAILABLE') from exc

        if len(rows) > MAX_ORDER_RESULTS:
            raise OracleWorklistError('MV_RESULT_LIMIT')
        return [item for row in rows if (item := self._normalizar_pedido(row))]

    def list_sectors(self) -> list[WorklistSector]:
        statement = text(
            """
            SELECT TO_CHAR(cd_setor) AS sector_code,
                   TRIM(nm_setor) AS sector_name
              FROM dbamv.setor
             WHERE nm_setor IS NOT NULL
             ORDER BY UPPER(TRIM(nm_setor)), cd_setor
            """
        )
        try:
            with self.engine.connect() as connection:
                rows = connection.execute(statement).mappings().all()
        except TimeoutError as exc:
            raise OracleWorklistError('MV_TIMEOUT') from exc
        except SQLAlchemyError as exc:
            raise OracleWorklistError('MV_UNAVAILABLE') from exc

        by_code: dict[str, WorklistSector] = {}
        for row in rows:
            code = str(row.get('sector_code') or '').strip()
            name = str(row.get('sector_name') or '').strip()
            if code and name:
                by_code[code] = WorklistSector(code=code, name=name)
        return sorted(
            by_code.values(),
            key=lambda item: (item.name.casefold(), item.code),
        )

    @staticmethod
    def _normalizar(row) -> WorklistItem | None:
        birth_date = row['patient_birth_date']
        if isinstance(birth_date, datetime):
            birth_date = birth_date.date()
        try:
            return WorklistItem(
                accessionNumber=row['accession_number'],
                patientId=row['patient_id'],
                patientName=row['patient_name'],
                scheduledAt=row['scheduled_at'],
                patientBirthDate=birth_date,
                patientSex=row['patient_sex'],
                procedureCode=row['procedure_code'],
                procedureDescription=row['procedure_description'],
                requestedProcedureId=row['requested_procedure_id'],
                scheduledProcedureStepId=(row['scheduled_procedure_step_id']),
                attendanceNumber=row['requested_procedure_id'],
            )
        except ValidationError:
            return None

    @staticmethod
    def _normalizar_pedido(row) -> WorklistOrderItem | None:
        birth_date = row['patient_birth_date']
        if isinstance(birth_date, datetime):
            birth_date = birth_date.date()
        try:
            return WorklistOrderItem(
                accessionNumber=row['accession_number'],
                attendanceNumber=row['attendance_number'],
                patientId=row['patient_id'],
                patientName=row['patient_name'],
                patientBirthDate=birth_date,
                requestedAt=row['requested_at'],
                procedureCode=row['procedure_code'],
                scheduledProcedureStepId=(
                    row['scheduled_procedure_step_id']
                ),
                procedureDescription=row['procedure_description'],
                currentSectorCode=row['current_sector_code'],
                currentSectorName=row['current_sector_name'],
                mvRealized=bool(row['mv_realized']),
                mvRealizedAt=row['mv_realized_at'],
            )
        except ValidationError:
            return None
