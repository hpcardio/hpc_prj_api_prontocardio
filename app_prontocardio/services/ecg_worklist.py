from collections.abc import Callable
from datetime import date, datetime
from threading import BoundedSemaphore
from typing import Protocol

from pydantic import ValidationError
from pydicom.dataset import Dataset
from pydicom.sequence import Sequence
from pynetdicom import AE
from pynetdicom.sop_class import ModalityWorklistInformationFind

from app_prontocardio.ecg_worklist_schema import (
    SearchType,
    WorklistItem,
    WorklistSearchRequest,
)

DICOM_DATE_LENGTH = 8


class MwlError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(f'Falha na comunicação MWL ({code}).')


class MwlSettings(Protocol):
    ECG_MWL_HOST: str
    ECG_MWL_PORT: int
    ECG_MWL_CALLED_AE: str
    ECG_MWL_CALLING_AE: str
    ECG_MWL_ATTENDANCE_TAG: str
    ECG_MWL_CONNECT_TIMEOUT_SECONDS: float
    ECG_MWL_RESPONSE_TIMEOUT_SECONDS: float
    ECG_MWL_MAX_RESULTS: int
    ECG_MWL_MAX_CONCURRENCY: int


def _abrir_associacao(settings: MwlSettings):
    ae = AE(ae_title=settings.ECG_MWL_CALLING_AE)
    ae.add_requested_context(ModalityWorklistInformationFind)
    ae.acse_timeout = settings.ECG_MWL_CONNECT_TIMEOUT_SECONDS
    ae.dimse_timeout = settings.ECG_MWL_RESPONSE_TIMEOUT_SECONDS
    ae.network_timeout = settings.ECG_MWL_RESPONSE_TIMEOUT_SECONDS
    return ae.associate(
        settings.ECG_MWL_HOST,
        settings.ECG_MWL_PORT,
        ae_title=settings.ECG_MWL_CALLED_AE,
    )


class MwlClient:
    def __init__(
        self,
        settings: MwlSettings,
        association_factory: Callable[[MwlSettings], object] | None = None,
    ):
        self.settings = settings
        self._association_factory = association_factory or _abrir_associacao
        self._semaphore = BoundedSemaphore(
            settings.ECG_MWL_MAX_CONCURRENCY
        )

    def search(self, request: WorklistSearchRequest) -> list[WorklistItem]:
        if not self._semaphore.acquire(blocking=False):
            raise MwlError('MWL_BUSY')

        association = None
        try:
            association = self._association_factory(self.settings)
            if not association.is_established:
                raise MwlError('MWL_ASSOCIATION_REJECTED')

            return self._consultar_associacao(association, request)
        except MwlError:
            raise
        except TimeoutError as exc:
            raise MwlError('MWL_TIMEOUT') from exc
        except (ConnectionError, OSError) as exc:
            raise MwlError('MWL_UNAVAILABLE') from exc
        except Exception as exc:
            raise MwlError('MWL_UNAVAILABLE') from exc
        finally:
            if association is not None and association.is_established:
                association.release()
            self._semaphore.release()

    def health(self) -> None:
        if not self._semaphore.acquire(blocking=False):
            raise MwlError('MWL_BUSY')

        association = None
        try:
            association = self._association_factory(self.settings)
            if not association.is_established:
                raise MwlError('MWL_ASSOCIATION_REJECTED')
        except MwlError:
            raise
        except TimeoutError as exc:
            raise MwlError('MWL_TIMEOUT') from exc
        except (ConnectionError, OSError) as exc:
            raise MwlError('MWL_UNAVAILABLE') from exc
        except Exception as exc:
            raise MwlError('MWL_UNAVAILABLE') from exc
        finally:
            if association is not None and association.is_established:
                association.release()
            self._semaphore.release()

    def _montar_query(self, request: WorklistSearchRequest) -> Dataset:
        query = Dataset()
        query.AccessionNumber = (
            request.identifier
            if request.search_type is SearchType.ORDER
            else ''
        )
        query.PatientID = ''
        query.PatientName = ''
        query.PatientBirthDate = ''
        query.PatientSex = ''
        query.RequestedProcedureDescription = ''
        query.RequestedProcedureID = (
            request.identifier
            if request.search_type is SearchType.ATTENDANCE
            and self.settings.ECG_MWL_ATTENDANCE_TAG == '0040,1001'
            else ''
        )

        procedure_code = Dataset()
        procedure_code.CodeValue = ''
        procedure_code.CodeMeaning = ''
        query.RequestedProcedureCodeSequence = Sequence([procedure_code])

        step = Dataset()
        step.ScheduledProcedureStepID = (
            request.identifier
            if request.search_type is SearchType.ATTENDANCE
            and self.settings.ECG_MWL_ATTENDANCE_TAG == '0040,0009'
            else ''
        )
        step.ScheduledProcedureStepStartDate = ''
        step.ScheduledProcedureStepStartTime = ''
        step.ScheduledProcedureStepDescription = ''
        scheduled_code = Dataset()
        scheduled_code.CodeValue = ''
        scheduled_code.CodeMeaning = ''
        step.ScheduledProtocolCodeSequence = Sequence([scheduled_code])
        query.ScheduledProcedureStepSequence = Sequence([step])
        return query

    def _consultar_associacao(
        self, association, request: WorklistSearchRequest
    ) -> list[WorklistItem]:
        query = self._montar_query(request)
        items: list[WorklistItem] = []
        responses = association.send_c_find(
            query, ModalityWorklistInformationFind
        )
        for status, identifier in responses:
            if status is None or not hasattr(status, 'Status'):
                raise MwlError('MWL_TIMEOUT')
            if status.Status == 0x0000:
                break
            if status.Status not in {0xFF00, 0xFF01}:
                raise MwlError('MWL_INVALID_RESPONSE')
            if identifier is None or not self._corresponde(
                identifier, request
            ):
                continue
            item = self._normalizar_item(identifier)
            if item is not None:
                items.append(item)
            if len(items) >= self.settings.ECG_MWL_MAX_RESULTS:
                break
        return items

    def _corresponde(
        self, dataset: Dataset, request: WorklistSearchRequest
    ) -> bool:
        if request.search_type is SearchType.ORDER:
            encontrado = str(getattr(dataset, 'AccessionNumber', ''))
        elif self.settings.ECG_MWL_ATTENDANCE_TAG == '0040,1001':
            encontrado = str(getattr(dataset, 'RequestedProcedureID', ''))
        else:
            step = self._primeiro_step(dataset)
            encontrado = str(
                getattr(step, 'ScheduledProcedureStepID', '')
            )
        return encontrado == request.identifier

    def _normalizar_item(self, dataset: Dataset) -> WorklistItem | None:
        step = self._primeiro_step(dataset)
        scheduled_at = self._data_hora_programada(step)
        birth_date = self._data_dicom(
            str(getattr(dataset, 'PatientBirthDate', ''))
        )
        procedure_code = self._codigo_procedimento(dataset, step)
        procedure_description = str(
            getattr(dataset, 'RequestedProcedureDescription', '')
            or getattr(step, 'ScheduledProcedureStepDescription', '')
        ) or None
        try:
            return WorklistItem(
                accessionNumber=str(
                    getattr(dataset, 'AccessionNumber', '')
                ),
                patientId=str(getattr(dataset, 'PatientID', '')),
                patientName=str(getattr(dataset, 'PatientName', '')),
                scheduledAt=scheduled_at,
                patientBirthDate=birth_date,
                patientSex=(
                    str(getattr(dataset, 'PatientSex', '')) or None
                ),
                procedureCode=procedure_code,
                procedureDescription=procedure_description,
                requestedProcedureId=(
                    str(getattr(dataset, 'RequestedProcedureID', '')) or None
                ),
                scheduledProcedureStepId=(
                    str(getattr(step, 'ScheduledProcedureStepID', '')) or None
                ),
                attendanceNumber=self._numero_atendimento(dataset),
            )
        except ValidationError:
            return None

    @staticmethod
    def _primeiro_step(dataset: Dataset) -> Dataset:
        sequence = getattr(dataset, 'ScheduledProcedureStepSequence', None)
        return sequence[0] if sequence else Dataset()

    def _numero_atendimento(self, dataset: Dataset) -> str | None:
        if self.settings.ECG_MWL_ATTENDANCE_TAG == '0040,1001':
            value = str(getattr(dataset, 'RequestedProcedureID', ''))
        else:
            value = str(
                getattr(
                    self._primeiro_step(dataset),
                    'ScheduledProcedureStepID',
                    '',
                )
            )
        return value or None

    @staticmethod
    def _data_hora_programada(step: Dataset) -> datetime | None:
        data = str(
            getattr(step, 'ScheduledProcedureStepStartDate', '')
        )
        hora = str(
            getattr(step, 'ScheduledProcedureStepStartTime', '')
        ).split('.')[0]
        if len(data) != DICOM_DATE_LENGTH or not hora:
            return None
        try:
            return datetime.strptime(data + hora.ljust(6, '0'), '%Y%m%d%H%M%S')
        except ValueError:
            return None

    @staticmethod
    def _data_dicom(valor: str) -> date | None:
        if not valor:
            return None
        try:
            return datetime.strptime(valor, '%Y%m%d').date()
        except ValueError:
            return None

    @staticmethod
    def _codigo_procedimento(
        dataset: Dataset, step: Dataset
    ) -> str | None:
        sequence = getattr(dataset, 'RequestedProcedureCodeSequence', None)
        if not sequence:
            sequence = getattr(step, 'ScheduledProtocolCodeSequence', None)
        if not sequence:
            return None
        return str(getattr(sequence[0], 'CodeValue', '')) or None
