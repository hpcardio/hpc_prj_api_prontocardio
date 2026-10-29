"""Persistência do controle de plantões do Pronto Escala."""

from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app_prontocardio.models import table_registry
from app_prontocardio.settings import Settings

settings = Settings()


@table_registry.mapped_as_dataclass
class ProntoEscalaParametro:
    __tablename__ = 'pronto_escala_parametros'
    __table_args__ = {'schema': settings.POSTGRES_SCHEMA}

    id: Mapped[int] = mapped_column(primary_key=True, init=False)
    antecedencia_checkin_min: Mapped[int] = mapped_column(Integer, default=60)
    atraso_checkin_min: Mapped[int] = mapped_column(Integer, default=30)
    antecedencia_checkout_min: Mapped[int] = mapped_column(Integer, default=0)
    atraso_checkout_min: Mapped[int] = mapped_column(Integer, default=120)
    atualizado_por_id: Mapped[int | None] = mapped_column(
        Integer, default=None
    )
    atualizado_por_nome: Mapped[str | None] = mapped_column(
        String(200), default=None
    )
    atualizado_em: Mapped[datetime] = mapped_column(
        DateTime, init=False, server_default=func.now(), onupdate=func.now()
    )


@table_registry.mapped_as_dataclass
class ProntoEscalaPlantao:
    __tablename__ = 'pronto_escala_plantoes'
    __table_args__ = (
        UniqueConstraint('cd_agenda_central'),
        {'schema': settings.POSTGRES_SCHEMA},
    )

    id: Mapped[int] = mapped_column(primary_key=True, init=False)
    cd_prestador: Mapped[int] = mapped_column(Integer, index=True)
    nm_prestador: Mapped[str] = mapped_column(String(200))
    setor: Mapped[str] = mapped_column(String(160), index=True)
    inicio: Mapped[datetime] = mapped_column(DateTime, index=True)
    fim: Mapped[datetime] = mapped_column(DateTime, index=True)
    criado_por_id: Mapped[int] = mapped_column(Integer)
    criado_por_nome: Mapped[str] = mapped_column(String(200))
    cd_agenda_central: Mapped[int | None] = mapped_column(
        Integer, default=None, index=True
    )
    origem: Mapped[str] = mapped_column(String(30), default='MANUAL')
    status: Mapped[str] = mapped_column(
        String(20), default='PROGRAMADO', index=True
    )
    criado_em: Mapped[datetime] = mapped_column(
        DateTime, init=False, server_default=func.now()
    )


@table_registry.mapped_as_dataclass
class ProntoEscalaRegistro:
    __tablename__ = 'pronto_escala_registros'
    __table_args__ = (
        UniqueConstraint('plantao_id', 'tipo'),
        {'schema': settings.POSTGRES_SCHEMA},
    )

    id: Mapped[int] = mapped_column(primary_key=True, init=False)
    plantao_id: Mapped[int] = mapped_column(Integer, index=True)
    tipo: Mapped[str] = mapped_column(String(10))
    registrado_em: Mapped[datetime] = mapped_column(DateTime)
    usuario_id: Mapped[int] = mapped_column(Integer)
    usuario_nome: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(20), default='REGISTRADO')


@table_registry.mapped_as_dataclass
class ProntoEscalaJustificativa:
    __tablename__ = 'pronto_escala_justificativas'
    __table_args__ = (
        UniqueConstraint('plantao_id', 'tipo'),
        {'schema': settings.POSTGRES_SCHEMA},
    )

    id: Mapped[int] = mapped_column(primary_key=True, init=False)
    plantao_id: Mapped[int] = mapped_column(Integer, index=True)
    tipo: Mapped[str] = mapped_column(String(10))
    texto: Mapped[str] = mapped_column(Text)
    usuario_id: Mapped[int] = mapped_column(Integer)
    usuario_nome: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(
        String(20), default='PENDENTE', index=True
    )
    decisao_por_id: Mapped[int | None] = mapped_column(Integer, default=None)
    decisao_por_nome: Mapped[str | None] = mapped_column(
        String(200), default=None
    )
    decisao_em: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    motivo_decisao: Mapped[str | None] = mapped_column(Text, default=None)
    criado_em: Mapped[datetime] = mapped_column(
        DateTime, init=False, server_default=func.now()
    )


@table_registry.mapped_as_dataclass
class ProntoEscalaEvento:
    __tablename__ = 'pronto_escala_eventos'
    __table_args__ = {'schema': settings.POSTGRES_SCHEMA}

    id: Mapped[int] = mapped_column(primary_key=True, init=False)
    acao: Mapped[str] = mapped_column(String(60), index=True)
    usuario_id: Mapped[int] = mapped_column(Integer)
    usuario_nome: Mapped[str] = mapped_column(String(200))
    plantao_id: Mapped[int | None] = mapped_column(
        Integer, default=None, index=True
    )
    justificativa_id: Mapped[int | None] = mapped_column(
        Integer, default=None, index=True
    )
    detalhe: Mapped[str | None] = mapped_column(Text, default=None)
    criado_em: Mapped[datetime] = mapped_column(
        DateTime, init=False, server_default=func.now()
    )
