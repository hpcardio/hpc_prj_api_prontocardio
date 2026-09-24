from datetime import date, datetime
from enum import StrEnum

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    computed_field,
    field_validator,
    model_validator,
)

MAX_ORDER_RANGE_DAYS = 7


class SearchType(StrEnum):
    ATTENDANCE = 'ATTENDANCE'
    ORDER = 'ORDER'


class WorklistSearchRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    search_type: SearchType = Field(alias='searchType')
    identifier: str = Field(
        min_length=1,
        max_length=30,
        pattern=r'^[A-Za-z0-9./-]+$',
    )

    @field_validator('identifier', mode='before')
    @classmethod
    def normalizar_identificador(cls, valor: object) -> object:
        return valor.strip() if isinstance(valor, str) else valor


class WorklistItem(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    accession_number: str = Field(alias='accessionNumber', min_length=1)
    patient_id: str = Field(alias='patientId', min_length=1)
    patient_name: str = Field(alias='patientName', min_length=1)
    scheduled_at: datetime = Field(alias='scheduledAt')
    patient_birth_date: date | None = Field(
        default=None, alias='patientBirthDate'
    )
    patient_sex: str | None = Field(default=None, alias='patientSex')
    procedure_code: str | None = Field(default=None, alias='procedureCode')
    procedure_description: str | None = Field(
        default=None, alias='procedureDescription'
    )
    requested_procedure_id: str | None = Field(
        default=None, alias='requestedProcedureId'
    )
    scheduled_procedure_step_id: str | None = Field(
        default=None, alias='scheduledProcedureStepId'
    )
    attendance_number: str | None = Field(
        default=None, alias='attendanceNumber'
    )


class WorklistSearchResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    search_type: SearchType = Field(alias='searchType')
    identifier: str
    items: list[WorklistItem]

    @computed_field
    @property
    def count(self) -> int:
        return len(self.items)


class WorklistOrdersRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    date_from: date = Field(alias='dateFrom')
    date_to_exclusive: date = Field(alias='dateToExclusive')

    @model_validator(mode='after')
    def validar_periodo(self):
        days = (self.date_to_exclusive - self.date_from).days
        if days < 1 or days > MAX_ORDER_RANGE_DAYS:
            raise ValueError('O período deve ter entre um e sete dias.')
        return self


class WorklistOrderItem(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    accession_number: str = Field(alias='accessionNumber', min_length=1)
    attendance_number: str = Field(alias='attendanceNumber', min_length=1)
    patient_id: str = Field(alias='patientId', min_length=1)
    patient_name: str = Field(alias='patientName', min_length=1)
    patient_birth_date: date | None = Field(alias='patientBirthDate')
    requested_at: datetime = Field(alias='requestedAt')
    procedure_code: str = Field(alias='procedureCode', min_length=1)
    scheduled_procedure_step_id: str = Field(
        alias='scheduledProcedureStepId', min_length=1
    )
    procedure_description: str | None = Field(alias='procedureDescription')
    current_sector_code: str | None = Field(alias='currentSectorCode')
    current_sector_name: str | None = Field(alias='currentSectorName')
    mv_realized: bool = Field(alias='mvRealized')
    mv_realized_at: datetime | None = Field(alias='mvRealizedAt')


class WorklistOrdersResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    date_from: date = Field(alias='dateFrom')
    date_to_exclusive: date = Field(alias='dateToExclusive')
    items: list[WorklistOrderItem]

    @computed_field
    @property
    def count(self) -> int:
        return len(self.items)


class WorklistSector(BaseModel):
    code: str = Field(min_length=1)
    name: str = Field(min_length=1)


class WorklistSectorsResponse(BaseModel):
    items: list[WorklistSector]

    @computed_field
    @property
    def count(self) -> int:
        return len(self.items)
