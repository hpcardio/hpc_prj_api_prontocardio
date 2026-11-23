from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

UUID_VERSION_4 = 4


class ClosedModel(BaseModel):
    model_config = ConfigDict(extra='forbid')


class AtendimentoTipo(StrEnum):
    AMBULATORIAL = 'AMBULATORIAL'
    URGENCIA = 'URGENCIA'
    INTERNACAO = 'INTERNACAO'
    EXAME = 'EXAME'


class OrigemFila(StrEnum):
    AGENDAMENTO = 'AGENDAMENTO'
    SENHA = 'SENHA'
    AVULSO = 'AVULSO'
    CLASSIFICACAO_RISCO = 'CLASSIFICACAO_RISCO'


class TentativaEstado(StrEnum):
    RECEBIDA = 'RECEBIDA'
    PRE_VALIDADA = 'PRE_VALIDADA'
    BLOQUEADA_ESCRITA = 'BLOQUEADA_ESCRITA'
    CONFLITO = 'CONFLITO'
    FALHA_LEITURA = 'FALHA_LEITURA'


class SourceFreshness(ClosedModel):
    source: OrigemFila
    checked_at: datetime
    stale: bool
    available: bool
    code: str


class QueueItem(ClosedModel):
    source: OrigemFila
    source_code: int | None
    patient_code: int | None
    patient_name: str | None = None
    attendance_type: AtendimentoTipo
    unit_code: int
    scheduled_at: datetime | None
    attendance_code: int | None = None
    attendance_opened_at: datetime | None = None
    attendance_closed_at: datetime | None = None
    item_code: int | None = None
    insurance_code: int | None = None
    insurance_name: str | None = None
    plan_code: int | None = None
    plan_name: str | None = None
    appointment_clinic_name: str | None = None
    mv_origin_clinic_name: str | None = None
    ticket_label: str | None = None
    risk_code: int | None = None


class QueueResponse(ClosedModel):
    items: list[QueueItem]
    freshness: list[SourceFreshness]


class OperatorScopeRequest(ClosedModel):
    unit_code: int | None = None


class OperatorScopeResponse(ClosedModel):
    unit_codes: list[int]
    sector_codes: list[int]
    attendance_types: list[AtendimentoTipo]
    valid_until: datetime
    stale: bool


class RiskPermissionsRequest(ClosedModel):
    unit_code: int


class RiskPermissionsResponse(ClosedModel):
    risk_codes: list[int]
    stale: bool


class PatientSearchRequest(ClosedModel):
    criterion: str = Field(min_length=2, max_length=120)


class PatientReference(ClosedModel):
    patient_code: int
    birth_date: str | None


class PatientSearchResponse(ClosedModel):
    items: list[PatientReference]
    ambiguous: bool


class PatientReviewRequest(ClosedModel):
    patient_code: int


class PatientReviewResponse(ClosedModel):
    patient_code: int
    divergence_codes: list[str]
    stale: bool


class OptionsRequest(ClosedModel):
    patient_code: int
    attendance_type: AtendimentoTipo
    unit_code: int


class OfficialOption(ClosedModel):
    code: int
    label: str


class OptionsResponse(ClosedModel):
    items: list[OfficialOption]
    stale: bool


class CentralCommand(ClosedModel):
    attempt_id: UUID
    patient_code: int
    attendance_type: AtendimentoTipo
    source: OrigemFila
    item_code: int
    insurance_code: int
    plan_code: int | None
    unit_code: int
    sector_code: int
    provider_code: int
    origin_clinic_code: int
    appointment_code: int | None
    ticket_code: int | None
    risk_classification_code: int | None

    @field_validator('attempt_id', mode='before')
    @classmethod
    def validate_canonical_uuid4(cls, value):
        if not isinstance(value, str):
            raise ValueError('attempt_id must be a canonical UUID v4')
        parsed = UUID(value)
        if parsed.version != UUID_VERSION_4 or str(parsed) != value:
            raise ValueError('attempt_id must be a canonical UUID v4')
        return parsed


class PreflightResponse(ClosedModel):
    attempt_id: UUID
    state: TentativaEstado
    rule_version: str
    reason_codes: list[str]


class CommandResponse(ClosedModel):
    attempt_id: UUID
    state: TentativaEstado
    replay: bool
    reason_code: str


class EffectResponse(ClosedModel):
    attempt_id: UUID
    attendance_code: int | None
    account_code: int | None
    order_codes: list[int]
    verified_at: datetime
    stale: bool


class CentralError(ClosedModel):
    code: str
    detail: str
