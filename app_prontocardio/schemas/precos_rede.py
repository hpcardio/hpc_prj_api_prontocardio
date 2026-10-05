from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, Field


class PrecoRedeItem(BaseModel):
    codigo_procedimento: str
    codigos_procedimento: list[str]
    codigo_pacote: str | None = None
    descricao: str
    valor_rede: Decimal = Field(gt=0)
    vigencia_inicio: date
    vigencia_fim: date | None = None
    origem_id: str
    extraido_em: datetime


class DivergenciaPrecoRede(BaseModel):
    chave: str
    motivo: str


class PrecoRedeResultado(BaseModel):
    count: int
    results: list[PrecoRedeItem]
    divergencias: list[DivergenciaPrecoRede]
    next_cursor: str | None = None
    extraido_em: datetime
