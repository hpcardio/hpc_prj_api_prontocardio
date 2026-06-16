from datetime import date, datetime
from decimal import Decimal

from pydantic import (
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
)

from app_prontocardio.models import TipoAtendimento


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
    nr_guia: int | None = None
    cd_senha: str | None = None
    nm_paciente: str | None = None
    nm_convenio: str | None = None
    descricao: str | None = None


class Message(BaseModel):
    message: str


class RegistroGlosaCreate(BaseModel):
    codigo_paciente: int
    cd_remessa: int
    cd_atendimento: int
    conta: int
    cd_prestador: int
    cd_convenio: int
    tp_atendimento: TipoAtendimento
    procedimento: str
    convenio: str
    guia: str
    prestador: str
    data_atendimento: datetime
    valor: Decimal
    processo_controle_fatura_gab: str
    processo_recurso: str | None = None
    data_glosa: date
    motivo_glosa: str
    descricao_glosa: str
    qtd_glosada: Decimal | None = None
    valor_glosado: Decimal | None = None
    dt_recurso: date | None = None
    dt_pagamento: date | None = None


class RegistroGlosaPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    codigo_paciente: int
    cd_remessa: int
    cd_atendimento: int
    conta: int
    cd_prestador: int
    cd_convenio: int
    tp_atendimento: TipoAtendimento
    procedimento: str
    convenio: str
    guia: str
    prestador: str
    data_atendimento: datetime
    valor: Decimal
    processo_controle_fatura_gab: str
    processo_recurso: str | None = None
    data_glosa: date
    motivo_glosa: str
    descricao_glosa: str
    qtd_glosada: Decimal | None = None
    valor_glosado: Decimal | None = None
    dt_recurso: date | None = None
    dt_pagamento: date | None = None
    sn_glosado: bool
    data_criacao: datetime


class RegistroGlosas(BaseModel):
    glosas: list[RegistroGlosaPublic]


class TissPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    codigo_termo: str
    termo: str
    dt_inicio_vigencia: date | None = None
    dt_fim_vigencia: date | None = None
    dt_fim_implantacao: date | None = None


class TissList(BaseModel):
    itens: list[TissPublic]


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
    nr_guia: int | None = None
    cd_senha: str | None = None
    dt_atendimento: datetime | None = None
    dt_alta: datetime | None = None
    dt_remessa: datetime | None = None
    dt_fechamento: datetime | None = None
    dt_lancamento: datetime | None = None
    hr_lancamento: datetime | None = None
    cd_prestador: int | None = None
    nm_prestador: str | None = None
    sn_fechada: str | None = None
    sn_pertence_pacote: str | None = None
    qt_lancamento: Decimal | None = None
    vl_unitario: Decimal | None = None
    vl_total_conta: Decimal | None = None
    vl_honorario_unitario: Decimal | None = None
    vl_acrescimo: Decimal | None = None
    vl_desconto: Decimal | None = None
    cd_ati_med: int | None = None
    ds_ati_med: str | None = None
    cd_usuario: str | None = None
    nm_usuario: str | None = None
    tp_atendimento: TipoAtendimento | None = None
    dt_ordenacao: datetime | None = None


class Atendimentos(BaseModel):
    atendimentos: list[Atendimento]
    total: int
    limit: int | None = None
    offset: int
