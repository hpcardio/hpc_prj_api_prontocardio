from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

NumericIdentifier = Annotated[
    str, Field(min_length=1, max_length=30, pattern=r'^[0-9]+$')
]


class WorklistItemStatusRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    order_number: NumericIdentifier = Field(alias='orderNumber')
    order_item_id: NumericIdentifier = Field(alias='orderItemId')
    attendance_number: NumericIdentifier = Field(alias='attendanceNumber')


class WorklistItemStatus(WorklistItemStatusRequest):
    patient_id: NumericIdentifier = Field(alias='patientId')
    procedure_code: Literal['597'] = Field(alias='procedureCode')
    pending: bool
    realized: bool
    realized_at: datetime | None = Field(alias='realizedAt')
