from datetime import date, datetime, time
from decimal import Decimal

from pydantic import (
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    field_validator,
)


class UserSchema(BaseModel):
    nome: str
    email: EmailStr
    senha: str


class UserPublic(BaseModel):
    id: int
    nome: str
    email: EmailStr
    model_config = ConfigDict(from_attributes=True)


class UserList(BaseModel):
    usuarios: list[UserPublic]


class FilterPage(BaseModel):
    offset: int = Field(ge=0, default=0)
    limit: int = Field(ge=0, default=10)


class FilterSearch(FilterPage):
    cd_remessa: int | None = None
    cd_atendimento: int | None = None
    cd_reg: int | None = None
    nm_paciente: str | None = None
    nm_convenio: str | None = None
    descricao: str | None = None
    tp_atendimento: str | None = None


class Message(BaseModel):
    message: str


class RegistroGlosaCreate(BaseModel):
    codigo_paciente: int
    cd_remessa: int
    cd_atendimento: int
    conta: int
    cd_prestador: int
    cd_convenio: int
    tp_atendimento: str
    procedimento: str
    convenio: str
    guia: str
    prestador: str
    data_atendimento: datetime
    valor: Decimal
    processo_controle_fatura_gab: str
    data_glosa: date
    motivo_glosa: str
    descricao_glosa: str


class RegistroGlosaPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    codigo_paciente: int
    cd_remessa: int
    cd_atendimento: int
    conta: int
    cd_prestador: int
    cd_convenio: int
    tp_atendimento: str
    procedimento: str
    convenio: str
    guia: str
    prestador: str
    data_atendimento: datetime
    valor: Decimal
    processo_controle_fatura_gab: str
    data_glosa: date
    motivo_glosa: str
    descricao_glosa: str
    sn_glosado: bool
    data_criacao: datetime


class RegistroGlosas(BaseModel):
    glosas: list[RegistroGlosaPublic]


class Token(BaseModel):
    access_token: str
    token_type: str


class VersaoOracle(BaseModel):
    banner: str


class Atendimento(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    cd_reg: int
    cd_lancamento: int
    cd_atendimento: int | None = None
    cd_paciente: int | None = None
    nm_paciente: str | None = None
    cd_remessa: int | None = None
    cd_regra: int | None = None
    ds_regra: str | None = None
    cd_convenio: int | None = None
    nm_convenio: str | None = None
    cd_gru_fat: int | None = None
    ds_gru_fat: str | None = None
    cd_pro_fat: int | None = None
    descricao: str | None = None
    cd_guia: int | None = None
    dt_lancamento: datetime | None = None
    hr_lancamento: time | None = None
    cd_prestador: int | None = None
    nm_prestador: str | None = None
    sn_pertence_pacote: str | None = None
    vl_unitario: Decimal | None = None
    vl_total_conta: Decimal | None = None
    vl_honorario_unitario: Decimal | None = None
    vl_acrescimo: Decimal | None = None
    vl_desconto: Decimal | None = None
    cd_ati_med: int | None = None
    ds_ati_med: str | None = None
    cd_usuario: str | None = None
    nm_usuario: str | None = None
    tp_atendimento: str | None = None
    dt_ordenacao: datetime | None = None

    @field_validator('hr_lancamento', mode='before')
    @classmethod
    def parse_hr_lancamento(cls, value):
        if isinstance(value, datetime):
            return value.time()
        return value


class Atendimentos(BaseModel):
    atendimentos: list[Atendimento]
    total: int
    limit: int | None = None
    offset: int
