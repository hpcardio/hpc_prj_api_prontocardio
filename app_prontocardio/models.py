from datetime import date, datetime, time
from decimal import Decimal

from sqlalchemy import Boolean, Date, DateTime, Numeric, String, func
from sqlalchemy.orm import Mapped, mapped_column, registry

from app_prontocardio.settings import Settings

table_registry = registry()
settings = Settings()


@table_registry.mapped_as_dataclass
class Usuario:
    __tablename__ = 'usuarios_api'
    __table_args__ = {'schema': settings.POSTGRES_SCHEMA}

    id: Mapped[int] = mapped_column(primary_key=True, init=False)
    nome: Mapped[str] = mapped_column(String, unique=True)
    email: Mapped[str] = mapped_column(String, unique=True)
    senha: Mapped[str]
    data_criacao: Mapped[datetime] = mapped_column(
        init=False, server_default=func.now()
    )


@table_registry.mapped_as_dataclass
class RegistroGlosa:
    __tablename__ = 'registros_glosa'
    __table_args__ = {'schema': settings.POSTGRES_SCHEMA}

    id: Mapped[int] = mapped_column(primary_key=True, init=False)
    codigo_paciente: Mapped[int]
    cd_remessa: Mapped[int]
    cd_atendimento: Mapped[int]
    conta: Mapped[int]
    cd_prestador: Mapped[int]
    cd_convenio: Mapped[int]
    tp_atendimento: Mapped[str] = mapped_column(String)
    procedimento: Mapped[str] = mapped_column(String)
    convenio: Mapped[str] = mapped_column(String)
    guia: Mapped[str] = mapped_column(String)
    prestador: Mapped[str] = mapped_column(String)
    data_atendimento: Mapped[datetime]
    valor: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    processo_controle_fatura_gab: Mapped[str] = mapped_column(String)
    data_glosa: Mapped[date] = mapped_column(Date)
    motivo_glosa: Mapped[str] = mapped_column(String)
    descricao_glosa: Mapped[str] = mapped_column(String)
    sn_glosado: Mapped[bool] = mapped_column(Boolean, default=True)
    data_criacao: Mapped[datetime] = mapped_column(
        init=False, server_default=func.now()
    )


@table_registry.mapped_as_dataclass
class ModelContaAtendimento:
    __tablename__ = 'HPC_V_CONTA_ATENDIMENTO'
    __table_args__ = {'schema': 'DBAMV'}

    cd_reg: Mapped[int] = mapped_column(primary_key=True, init=False)
    cd_lancamento: Mapped[int] = mapped_column(primary_key=True, init=False)
    cd_atendimento: Mapped[int | None] = mapped_column(init=False)
    cd_paciente: Mapped[int | None] = mapped_column(init=False)
    nm_paciente: Mapped[str | None] = mapped_column(String, init=False)
    cd_remessa: Mapped[int | None] = mapped_column(init=False)
    cd_regra: Mapped[int | None] = mapped_column(init=False)
    ds_regra: Mapped[str | None] = mapped_column(String, init=False)
    cd_convenio: Mapped[int | None] = mapped_column(init=False)
    nm_convenio: Mapped[str | None] = mapped_column(String, init=False)
    cd_gru_fat: Mapped[int | None] = mapped_column(init=False)
    ds_gru_fat: Mapped[str | None] = mapped_column(String, init=False)
    cd_pro_fat: Mapped[int | None] = mapped_column(init=False)
    descricao: Mapped[str | None] = mapped_column(String, init=False)
    cd_guia: Mapped[int | None] = mapped_column(init=False)
    dt_lancamento: Mapped[datetime | None] = mapped_column(
        DateTime,
        init=False,
    )
    hr_lancamento: Mapped[time | None] = mapped_column(init=False)
    cd_prestador: Mapped[int | None] = mapped_column(init=False)
    nm_prestador: Mapped[str | None] = mapped_column(String, init=False)
    sn_pertence_pacote: Mapped[str | None] = mapped_column(String, init=False)
    vl_unitario: Mapped[Decimal | None] = mapped_column(Numeric, init=False)
    vl_total_conta: Mapped[Decimal | None] = mapped_column(Numeric, init=False)
    vl_honorario_unitario: Mapped[Decimal | None] = mapped_column(
        Numeric,
        init=False,
    )
    vl_acrescimo: Mapped[Decimal | None] = mapped_column(Numeric, init=False)
    vl_desconto: Mapped[Decimal | None] = mapped_column(Numeric, init=False)
    cd_ati_med: Mapped[int | None] = mapped_column(init=False)
    ds_ati_med: Mapped[str | None] = mapped_column(String, init=False)
    cd_usuario: Mapped[str | None] = mapped_column(String, init=False)
    nm_usuario: Mapped[str | None] = mapped_column(String, init=False)
    tp_atendimento: Mapped[str | None] = mapped_column(String, init=False)
    dt_ordenacao: Mapped[datetime | None] = mapped_column(DateTime, init=False)
