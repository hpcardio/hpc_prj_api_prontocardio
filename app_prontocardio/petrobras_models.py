"""Separate Petrobras source, Oracle reconciliation and user work."""

from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    LargeBinary,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app_prontocardio.models import settings, table_registry

SCHEMA = settings.POSTGRES_SCHEMA


def utcnow():
    return datetime.now(timezone.utc)


@table_registry.mapped
class PetrobrasProtocolo:
    __tablename__ = 'petrobras_protocolos'
    __table_args__ = (
        UniqueConstraint('prestador', 'numero', name='uq_petrobras_protocolo'),
        {'schema': SCHEMA},
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    prestador: Mapped[str] = mapped_column(String(14))
    numero: Mapped[str] = mapped_column(String(12))
    cd_remessa: Mapped[int | None] = mapped_column(Integer)
    ativo: Mapped[bool] = mapped_column(Boolean, default=True)
    ultima_sincronizacao: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    proxima_sincronizacao: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    erro: Mapped[str | None] = mapped_column(String(200))


@table_registry.mapped
class PetrobrasExecucao:
    __tablename__ = 'petrobras_execucoes'
    __table_args__ = {'schema': SCHEMA}
    id: Mapped[int] = mapped_column(primary_key=True)
    competencia: Mapped[str | None] = mapped_column(String(6))
    inicio: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    fim: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    usuario_id: Mapped[int | None] = mapped_column(Integer)
    situacao: Mapped[str] = mapped_column(String(30), default='em_execucao')
    quantidade: Mapped[int] = mapped_column(Integer, default=0)
    erro: Mapped[str | None] = mapped_column(String(200))


@table_registry.mapped
class PetrobrasGlosa:
    __tablename__ = 'petrobras_glosas'
    __table_args__ = {'schema': SCHEMA}
    id: Mapped[int] = mapped_column(primary_key=True)
    chave: Mapped[str] = mapped_column(String(64), unique=True)
    protocolo_id: Mapped[int] = mapped_column(
        ForeignKey(f'{SCHEMA}.petrobras_protocolos.id'), index=True
    )
    demonstrativo: Mapped[str] = mapped_column(String(20))
    guia_prestador: Mapped[str | None] = mapped_column(String(20), index=True)
    guia_operadora: Mapped[str | None] = mapped_column(String(20))
    tuss: Mapped[str | None] = mapped_column(String(20), index=True)
    fonte: Mapped[dict] = mapped_column(JSON)
    valor_glosa: Mapped[float | None] = mapped_column(Numeric(14, 2))
    presente_na_fonte: Mapped[bool] = mapped_column(Boolean, default=True)
    cruzamento: Mapped[str] = mapped_column(
        String(30), default='dados_insuficientes', index=True
    )
    oracle: Mapped[dict | None] = mapped_column(JSON)
    divergencias: Mapped[list] = mapped_column(JSON, default=list)
    candidatos: Mapped[int] = mapped_column(Integer, default=0)
    erro_oracle: Mapped[str | None] = mapped_column(String(200))
    atualizado_em: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    justificativa: Mapped[str] = mapped_column(Text, default='')
    versao: Mapped[int] = mapped_column(Integer, default=1)
    prazo: Mapped[object | None] = mapped_column(Date)
    origem_prazo: Mapped[str | None] = mapped_column(String(500))
    recurso_estado: Mapped[str] = mapped_column(
        String(30), default='rascunho', index=True
    )
    recurso_guia: Mapped[str | None] = mapped_column(String(20))
    recurso_protocolo: Mapped[str | None] = mapped_column(String(12))
    recurso_resultado: Mapped[dict | None] = mapped_column(JSON)
    ultima_consulta_recurso: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )


@table_registry.mapped
class PetrobrasEvento:
    __tablename__ = 'petrobras_eventos'
    __table_args__ = {'schema': SCHEMA}
    id: Mapped[int] = mapped_column(primary_key=True)
    glosa_id: Mapped[int] = mapped_column(
        ForeignKey(f'{SCHEMA}.petrobras_glosas.id'), index=True
    )
    criado_em: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    usuario_id: Mapped[int | None] = mapped_column(Integer)
    tipo: Mapped[str] = mapped_column(String(40))
    dados: Mapped[dict] = mapped_column(JSON)


@table_registry.mapped
class PetrobrasAnexo:
    __tablename__ = 'petrobras_anexos'
    __table_args__ = (
        UniqueConstraint('glosa_id', 'sha256', name='uq_petrobras_anexo'),
        {'schema': SCHEMA},
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    glosa_id: Mapped[int] = mapped_column(
        ForeignKey(f'{SCHEMA}.petrobras_glosas.id'), index=True
    )
    nome: Mapped[str] = mapped_column(String(200))
    mime: Mapped[str] = mapped_column(String(100))
    conteudo: Mapped[bytes] = mapped_column(LargeBinary)
    sha256: Mapped[str] = mapped_column(String(64))
    tipo_documento: Mapped[str] = mapped_column(String(2))
    natureza_guia: Mapped[str] = mapped_column(String(1))
    formato: Mapped[str] = mapped_column(String(2))
    estado: Mapped[str] = mapped_column(String(30), default='rascunho')
    protocolo: Mapped[str | None] = mapped_column(String(20))
    criado_em: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
