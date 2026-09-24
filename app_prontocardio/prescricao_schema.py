from datetime import datetime

from pydantic import BaseModel, Field


class ItemPrescritoPaciente(BaseModel):
    cd_atendimento: int
    data_atendimento: datetime
    cd_pre_med: int
    data_prescricao: datetime | None = None
    prescricao_fechada: bool
    cd_itpre_med: int
    cd_tip_presc: int
    descricao_prescrita: str
    codigo_tuss: str | None = None
    cd_item_agendamento: int | None = None
    ds_item_agendamento: str | None = None
    duracao_minutos: int | None = None
    medico_solicitante: str | None = None
    agendavel: bool
    agendado: bool
    motivo_indisponibilidade: str | None = None


class PrescricoesPaciente(BaseModel):
    itens: list[ItemPrescritoPaciente] = Field(default_factory=list)
    total: int
    total_pendentes: int
    total_agendados: int
    total_nao_mapeados: int
    recomendacao: str
