from datetime import datetime

from pydantic import BaseModel, Field, field_validator


class EvolucaoRascunhoInput(BaseModel):
    cd_atendimento: int = Field(gt=0)


class EvolucaoTextoInput(BaseModel):
    texto: str = Field(min_length=1, max_length=4000)
    linha_cuidado: str | None = Field(default=None, max_length=32)

    @field_validator('texto')
    @classmethod
    def validar_texto(cls, value: str) -> str:
        if not value.strip():
            raise ValueError('O texto da evolucao nao pode estar vazio.')
        return value


class ContextoEvolucaoMv(BaseModel):
    cd_atendimento: int
    cd_paciente: int
    cd_prestador: int
    usuario_mv: str
    cd_id_usuario: int
    cd_tipo_documento: int
    cd_objeto: int
    tipo_documento: str
    objeto: str
    assinatura_digital_configurada: bool
    escrita_habilitada: bool


class EvolucaoMv(BaseModel):
    cd_pre_med: int
    cd_documento_clinico: int
    cd_atendimento: int
    cd_paciente: int
    cd_prestador: int
    cd_tipo_documento: int
    cd_objeto: int
    tp_pre_med: str
    tp_status: str
    sn_fechado: str
    fl_impresso: str
    usuario_mv: str
    usuario_autorizador: str | None = None
    dh_criacao: datetime
    dh_fechamento: datetime | None = None
    dh_impressao: datetime | None = None
    texto: str | None = None
    assinatura_certificada: bool = False


class EvolucoesMv(BaseModel):
    evolucoes: list[EvolucaoMv]


class ExameLinhaCuidado(BaseModel):
    codigo: str
    descricao: str
    criterio: str
    momentos: list[str] = Field(default_factory=list)
    opcional: bool = False


class DiagramaLinhaCuidado(BaseModel):
    titulo: str
    url: str


class LinhaCuidadoEvolucao(BaseModel):
    codigo: str
    nome: str
    texto_evolucao: str
    documento_mv_parametrizado: bool
    fluxo_resumido: list[str]
    exames: list[ExameLinhaCuidado]
    diagramas: list[DiagramaLinhaCuidado]


class LinhasCuidadoEvolucao(BaseModel):
    linhas: list[LinhaCuidadoEvolucao]
