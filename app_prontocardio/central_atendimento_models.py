from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Index, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app_prontocardio.models import settings, table_registry

READONLY_STATES = (
    'RECEBIDA',
    'PRE_VALIDADA',
    'BLOQUEADA_ESCRITA',
    'CONFLITO',
    'FALHA_LEITURA',
)
STATE_SQL = ', '.join(f"'{state}'" for state in READONLY_STATES)


@table_registry.mapped_as_dataclass
class CentralAtendimentoTentativa:
    __tablename__ = 'central_atendimento_tentativas'
    __table_args__ = (
        CheckConstraint(f'state IN ({STATE_SQL})', name='ck_central_state'),
        Index('ix_central_tentativas_state', 'state'),
        Index(
            'ix_central_tentativas_unit_type',
            'unit_code',
            'attendance_type',
        ),
        {'schema': settings.POSTGRES_SCHEMA},
    )

    id: Mapped[int] = mapped_column(primary_key=True, init=False)
    attempt_id: Mapped[str] = mapped_column(String(36), unique=True)
    command_hash: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(30))
    rule_version: Mapped[str] = mapped_column(String(40))
    unit_code: Mapped[int] = mapped_column(Integer)
    attendance_type: Mapped[str] = mapped_column(String(20))
    revision: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), init=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        init=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


@table_registry.mapped_as_dataclass
class CentralAtendimentoEvento:
    __tablename__ = 'central_atendimento_eventos'
    __table_args__ = (
        Index('ix_central_eventos_attempt_id', 'attempt_id'),
        Index('ix_central_eventos_created_at', 'created_at'),
        {'schema': settings.POSTGRES_SCHEMA},
    )

    id: Mapped[int] = mapped_column(primary_key=True, init=False)
    attempt_id: Mapped[str] = mapped_column(String(36))
    operator_id: Mapped[int] = mapped_column(Integer)
    route: Mapped[str] = mapped_column(String(80))
    state_new: Mapped[str] = mapped_column('new_state', String(30))
    result_code: Mapped[str] = mapped_column(String(60))
    unit_code: Mapped[int] = mapped_column(Integer)
    attendance_type: Mapped[str] = mapped_column(String(20))
    previous_state: Mapped[str | None] = mapped_column(
        String(30), default=None
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), init=False, server_default=func.now()
    )
