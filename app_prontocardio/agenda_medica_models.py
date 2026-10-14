"""Tabelas da Agenda Médica (PostgreSQL da API).

Guardam ofertas de grade, pedidos dos médicos e decisões. Nada aqui é gravado
no SoulMV: o MV só recebe a agenda quando um pedido de interesse é aprovado e a
gravação está habilitada (AGENDA_MEDICA_MV_GRAVACAO_HABILITADA).
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import JSON, Date, DateTime, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app_prontocardio.models import table_registry
from app_prontocardio.settings import Settings

settings = Settings()


@table_registry.mapped_as_dataclass
class AgendaMedicaVinculo:
    """Liga um usuário da API ao prestador do SoulMV que ele representa."""

    __tablename__ = 'agenda_medica_vinculos'
    __table_args__ = {'schema': settings.POSTGRES_SCHEMA}

    id: Mapped[int] = mapped_column(primary_key=True, init=False)
    usuario_id: Mapped[int] = mapped_column(Integer, unique=True)
    cd_prestador: Mapped[int] = mapped_column(Integer, index=True)
    criado_por: Mapped[str] = mapped_column(String(200))
    criado_em: Mapped[datetime] = mapped_column(
        DateTime, init=False, server_default=func.now()
    )


@table_registry.mapped_as_dataclass
class AgendaMedicaOferta:
    """Grade oferecida aos médicos, baseada numa escala-modelo do SoulMV."""

    __tablename__ = 'agenda_medica_ofertas'
    __table_args__ = {'schema': settings.POSTGRES_SCHEMA}

    id: Mapped[int] = mapped_column(primary_key=True, init=False)
    cd_escala_modelo: Mapped[int] = mapped_column(Integer, index=True)
    data_agenda: Mapped[date] = mapped_column(Date, index=True)
    especialidade: Mapped[str] = mapped_column(String(120))
    criado_por_id: Mapped[int] = mapped_column(Integer)
    criado_por_nome: Mapped[str] = mapped_column(String(200))
    observacao: Mapped[str | None] = mapped_column(Text, default=None)
    status: Mapped[str] = mapped_column(
        String(20), default='ABERTA', index=True
    )
    criado_em: Mapped[datetime] = mapped_column(
        DateTime, init=False, server_default=func.now()
    )


@table_registry.mapped_as_dataclass
class AgendaMedicaSolicitacao:
    """Pedido de um médico: interesse numa oferta ou cancelamento de agenda."""

    __tablename__ = 'agenda_medica_solicitacoes'
    __table_args__ = {'schema': settings.POSTGRES_SCHEMA}

    id: Mapped[int] = mapped_column(primary_key=True, init=False)
    tipo: Mapped[str] = mapped_column(String(20), index=True)
    cd_prestador: Mapped[int] = mapped_column(Integer, index=True)
    criado_por_id: Mapped[int] = mapped_column(Integer)
    criado_por_nome: Mapped[str] = mapped_column(String(200))
    oferta_id: Mapped[int | None] = mapped_column(
        Integer, default=None, index=True
    )
    cd_agenda_central: Mapped[int | None] = mapped_column(
        Integer, default=None
    )
    motivo: Mapped[str | None] = mapped_column(Text, default=None)
    status: Mapped[str] = mapped_column(
        String(20), default='PENDENTE', index=True
    )
    decisao_por_id: Mapped[int | None] = mapped_column(Integer, default=None)
    decisao_por_nome: Mapped[str | None] = mapped_column(
        String(200), default=None
    )
    decisao_em: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    resultado_mv: Mapped[dict | None] = mapped_column(JSON, default=None)
    criado_em: Mapped[datetime] = mapped_column(
        DateTime, init=False, server_default=func.now()
    )


@table_registry.mapped_as_dataclass
class AgendaMedicaEvento:
    """Trilha de auditoria: quem fez, o quê, quando e por quê."""

    __tablename__ = 'agenda_medica_eventos'
    __table_args__ = {'schema': settings.POSTGRES_SCHEMA}

    id: Mapped[int] = mapped_column(primary_key=True, init=False)
    acao: Mapped[str] = mapped_column(String(60), index=True)
    usuario_id: Mapped[int] = mapped_column(Integer)
    usuario_nome: Mapped[str] = mapped_column(String(200))
    oferta_id: Mapped[int | None] = mapped_column(Integer, default=None)
    solicitacao_id: Mapped[int | None] = mapped_column(
        Integer, default=None, index=True
    )
    detalhe: Mapped[str | None] = mapped_column(Text, default=None)
    criado_em: Mapped[datetime] = mapped_column(
        DateTime, init=False, server_default=func.now()
    )
