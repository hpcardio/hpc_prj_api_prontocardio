from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field


class HorarioDisponivel(BaseModel):
    cd_it_agenda_central: int
    cd_agenda_central: int
    cd_tip_mar: int | None = None
    cd_item_agendamento: int
    ds_item_agendamento: str
    data_agenda: date
    horario: datetime
    data_hora: datetime | None = None
    cd_unidade_atendimento: int | None = None
    ds_unidade_atendimento: str | None = None
    ds_local_unidade_atendimento: str | None = None
    cd_prestador: int | None = None
    nm_prestador: str | None = None


class HorariosDisponiveis(BaseModel):
    horarios: list[HorarioDisponivel]
    total: int


class ReservaHorarioInput(BaseModel):
    cd_paciente: int = Field(gt=0)
    cd_it_agenda_central: int = Field(gt=0)
    reserva_token: str = Field(min_length=16, max_length=100)


class LiberarReservaHorarioInput(BaseModel):
    reserva_token: str = Field(min_length=16, max_length=100)


class ReservaHorarioResultado(BaseModel):
    cd_it_agenda_central: int
    reservado: bool
    expira_em: datetime
    expira_em_segundos: int


class JornadaIntegradaItemInput(BaseModel):
    cd_item_agendamento: int = Field(gt=0)
    ds_item_agendamento: str | None = Field(default=None, max_length=250)
    cd_prestador: int | None = Field(default=None, gt=0)
    cd_tip_mar: int | None = Field(default=None, gt=0)


class JornadaIntegradaSugestaoInput(BaseModel):
    itens: list[JornadaIntegradaItemInput] = Field(min_length=2, max_length=12)
    data_inicio: date | None = None
    data_fim: date | None = None
    cd_paciente: int | None = Field(default=None, gt=0)
    cd_convenio: int | None = Field(default=None, gt=0)
    cd_con_pla: int | None = Field(default=None, gt=0)
    dias_busca: int = Field(default=120, ge=1, le=180)
    limite_por_item: int = Field(default=800, ge=1, le=1500)


class JornadaIntegradaPrimeiraData(BaseModel):
    cd_item_agendamento: int
    ds_item_agendamento: str
    primeira_data: date | None = None
    total_datas: int
    status: str


class JornadaIntegradaDataCandidata(BaseModel):
    data: date
    total_itens: int
    itens_disponiveis: list[int]
    itens_ausentes: list[int]


class JornadaIntegradaSugestao(BaseModel):
    data_comum: date | None = None
    melhor_data: date | None = None
    melhor_total_itens: int
    total_itens: int
    primeiras_datas: list[JornadaIntegradaPrimeiraData]
    candidatas: list[JornadaIntegradaDataCandidata]
    itens_sem_vaga: list[int]
    gargalos: list[int]
    mensagem: str


class PacienteResumo(BaseModel):
    cd_paciente: int
    nm_paciente: str
    dt_nascimento: date | None = None
    tp_sexo: str | None = None
    cpf_final: str | None = None
    email: str | None = None
    nr_ddi_celular: str | None = None
    nr_ddd_celular: str | None = None
    nr_celular: str | None = None
    nr_ddi_fone: str | None = None
    nr_ddd_fone: str | None = None
    nr_fone: str | None = None
    nr_ddi_fone_comercial: str | None = None
    nr_ddd_fone_comercial: str | None = None
    nr_fone_comercial: str | None = None
    nr_cep: str | None = None
    ds_endereco: str | None = None
    nr_endereco: int | None = None
    ds_complemento: str | None = None
    nm_bairro: str | None = None
    cd_cidade: int | None = None
    nm_cidade: str | None = None
    cd_uf: str | None = None


class PacientesEncontrados(BaseModel):
    pacientes: list[PacienteResumo]
    total: int


class PacienteLoteResumo(BaseModel):
    cd_paciente: int
    nm_paciente: str
    nr_ddd_celular: str | None = None
    nr_celular: str | None = None
    nr_ddd_fone: str | None = None
    nr_fone: str | None = None
    nr_ddd_fone_comercial: str | None = None
    nr_fone_comercial: str | None = None


class PacientesLoteResultado(BaseModel):
    pacientes: list[PacienteLoteResumo]
    total: int
    proximo_cursor: int | None = None


class CadastroPacienteInput(BaseModel):
    nm_paciente: str = Field(min_length=3, max_length=200)
    nr_cpf: str = Field(min_length=11, max_length=14)
    dt_nascimento: date
    tp_sexo: str = Field(pattern='^[FMIO]$')
    email: str | None = Field(default=None, max_length=200)
    nr_ddi_celular: str | None = Field(default=None, max_length=3)
    nr_ddd_celular: str | None = Field(default=None, max_length=4)
    nr_celular: str | None = Field(default=None, max_length=20)
    nr_ddi_fone: str | None = Field(default=None, max_length=3)
    nr_ddd_fone: str | None = Field(default=None, max_length=4)
    nr_fone: str | None = Field(default=None, max_length=20)
    nr_ddi_fone_comercial: str | None = Field(default=None, max_length=3)
    nr_ddd_fone_comercial: str | None = Field(default=None, max_length=4)
    nr_fone_comercial: str | None = Field(default=None, max_length=20)
    nr_cep: str | None = Field(default=None, max_length=12)
    ds_endereco: str | None = Field(default=None, max_length=200)
    nr_endereco: int | None = Field(default=None, ge=0)
    ds_complemento: str | None = Field(default=None, max_length=100)
    nm_bairro: str | None = Field(default=None, max_length=100)
    cd_cidade: int | None = Field(default=None, gt=0)
    cd_convenio: int = Field(gt=0)
    cd_con_pla: int = Field(gt=0)
    nr_carteira: str | None = Field(default=None, max_length=25)


class PacienteCadastrado(BaseModel):
    cd_paciente: int
    nm_paciente: str
    nr_cpf: str
    mensagem: str


class AtualizacaoPacienteInput(BaseModel):
    email: str | None = Field(default=None, max_length=200)
    nr_ddi_celular: str | None = Field(default=None, max_length=3)
    nr_ddd_celular: str | None = Field(default=None, max_length=4)
    nr_celular: str | None = Field(default=None, max_length=20)
    nr_ddi_fone: str | None = Field(default=None, max_length=3)
    nr_ddd_fone: str | None = Field(default=None, max_length=4)
    nr_fone: str | None = Field(default=None, max_length=20)
    nr_ddi_fone_comercial: str | None = Field(default=None, max_length=3)
    nr_ddd_fone_comercial: str | None = Field(default=None, max_length=4)
    nr_fone_comercial: str | None = Field(default=None, max_length=20)
    cd_convenio: int | None = Field(default=None, gt=0)
    cd_con_pla: int | None = Field(default=None, gt=0)
    nr_carteira: str | None = Field(default=None, max_length=25)
    nr_cep: str | None = Field(default=None, max_length=12)
    ds_endereco: str | None = Field(default=None, max_length=200)
    nr_endereco: int | None = Field(default=None, ge=0)
    ds_complemento: str | None = Field(default=None, max_length=100)
    nm_bairro: str | None = Field(default=None, max_length=100)
    cd_cidade: int | None = Field(default=None, gt=0)
    nm_cidade: str | None = Field(default=None, max_length=100)
    cd_uf: str | None = Field(default=None, min_length=2, max_length=2)


class EnderecoCep(BaseModel):
    cep: str
    logradouro: str
    bairro: str
    cidade: str
    uf: str
    codigo_ibge: str
    cd_cidade: int | None = None
    correspondencia_cidade: str = Field(pattern='^(exata|pendente)$')


class PacienteAtualizado(BaseModel):
    cd_paciente: int
    mensagem: str


class UltimoAtendimentoPaciente(BaseModel):
    cd_atendimento: int
    horario_atendimento: datetime
    tipo_atendimento: str | None = None
    ds_tipo_atendimento: str | None = None
    cd_prestador: int | None = None
    nm_prestador: str | None = None
    cd_convenio: int | None = None
    nm_convenio: str | None = None
    cd_con_pla: int | None = None
    ds_con_pla: str | None = None


class HistoricoPaciente(BaseModel):
    ultimo_atendimento: UltimoAtendimentoPaciente | None = None
    atendimentos: list[UltimoAtendimentoPaciente] = Field(default_factory=list)
    total: int


class LinhaCuidadoResposta(BaseModel):
    campo: str | None = None
    identificador: str | None = None
    resposta: str | None = None


class LinhaCuidadoPaciente(BaseModel):
    cd_documento: int
    cd_registro: int | None = None
    cd_atendimento: int | None = None
    ds_documento: str | None = None
    ds_tipo_documento: str | None = None
    tp_status: str | None = None
    dh_documento: datetime | None = None
    dh_fechamento: datetime | None = None
    cd_usuario_criou: str | None = None
    respostas: list[LinhaCuidadoResposta] = Field(default_factory=list)


class LinhasCuidadoPaciente(BaseModel):
    linhas: list[LinhaCuidadoPaciente]
    total: int


class PlanoDisponivel(BaseModel):
    cd_convenio: int
    nm_convenio: str
    cd_con_pla: int
    ds_con_pla: str


class PlanosDisponiveis(BaseModel):
    planos: list[PlanoDisponivel]
    total: int


class ItemAgendamentoResumo(BaseModel):
    cd_item_agendamento: int
    ds_item_agendamento: str
    cd_exa_rx: int | None = None
    duracao_minutos: int | None = None


class ItensAgendamentoEncontrados(BaseModel):
    itens: list[ItemAgendamentoResumo]
    total: int


class ProcedimentoMv(BaseModel):
    codigo_mv: str
    descricao_mv: str


class PaginaProcedimentosMv(BaseModel):
    itens: list[ProcedimentoMv]
    proximo_cursor: str | None = None


class ConvenioPaciente(BaseModel):
    cd_convenio: int
    nm_convenio: str
    cd_con_pla: int
    ds_con_pla: str
    ultimo_atendimento: datetime | None = None


class ConveniosPaciente(BaseModel):
    convenios: list[ConvenioPaciente]
    total: int


class AgendamentoPaciente(BaseModel):
    protocolo: int | None = None
    item_movimento_id: int | None = None
    cd_it_agenda_central: int
    cd_agenda_central: int
    cd_item_agendamento: int
    ds_item_agendamento: str
    horario: datetime
    cd_prestador: int | None = None
    nm_prestador: str | None = None
    ds_unidade_atendimento: str | None = None
    status: str | None = None
    cd_tip_mar: int | None = None
    cd_convenio: int | None = None
    nm_convenio: str | None = None
    cd_con_pla: int | None = None
    ds_con_pla: str | None = None


class AgendamentosPaciente(BaseModel):
    agendamentos: list[AgendamentoPaciente]
    total: int


class CancelarAgendamentoInput(BaseModel):
    motivo: str = Field(min_length=1, max_length=200)
    chave_idempotencia: str = Field(min_length=8, max_length=100)


class AgendamentoCancelado(BaseModel):
    status: str
    mensagem: str
    horario_id: int
    retorno_mv: str | None = None
    whatsapp_status: str | None = None
    whatsapp_mensagem: str | None = None


class PrestadorAgendamento(BaseModel):
    cd_prestador: int
    nm_prestador: str
    ds_codigo_conselho: str | None = None


class PrestadoresAgendamento(BaseModel):
    prestadores: list[PrestadorAgendamento]
    total: int
    exige_prestador: bool


class TipoMarcacaoConsulta(BaseModel):
    cd_tip_mar: int
    ds_tip_mar: str


class TiposMarcacaoConsulta(BaseModel):
    tipos: list[TipoMarcacaoConsulta]
    total: int


class PreValidacaoAgendamentoInput(BaseModel):
    cd_paciente: int = Field(gt=0)
    cd_item_agendamento: int = Field(gt=0)
    cd_it_agenda_central: int = Field(gt=0)
    cd_convenio: int = Field(gt=0)
    cd_con_pla: int = Field(gt=0)
    cd_prestador: int | None = Field(default=None, gt=0)
    cd_tip_mar: int | None = Field(default=None, gt=0)
    reserva_token: str | None = Field(default=None, min_length=16, max_length=100)


class PreValidacaoAgendamento(BaseModel):
    pode_agendar: bool
    alertas: list[str]
    cd_paciente: int
    nm_paciente: str
    cd_item_agendamento: int
    ds_item_agendamento: str
    cd_it_agenda_central: int
    cd_agenda_central: int
    cd_tip_mar: int | None = None
    horario: datetime
    cd_convenio: int
    nm_convenio: str
    cd_con_pla: int
    ds_con_pla: str
    cd_prestador: int | None = None
    nm_prestador: str | None = None
    ds_unidade_atendimento: str | None = None
    ds_local_unidade_atendimento: str | None = None
    agendamento_existente_slot: int | None = None
    agendamento_existente_horario: datetime | None = None


class ConfirmarAgendamentoInput(PreValidacaoAgendamentoInput):
    """Dados necessarios para confirmar um agendamento unico.

    O contrato existe desde ja, mas a escrita permanece protegida ate a
    procedure transacional do MV ser configurada.
    """

    chave_idempotencia: str = Field(min_length=8, max_length=100)
    cd_agenda_central: int = Field(gt=0)
    cd_it_agenda_fim: int | None = Field(default=None, gt=0)
    cd_atendimento: int | None = Field(default=None, gt=0)
    nr_ddd_celular: str | None = Field(default=None, max_length=4)
    nr_celular: str | None = Field(default=None, max_length=20)
    usuario_mv: str | None = Field(default=None, max_length=60)
    nome_operador: str | None = Field(default=None, max_length=120)
    observacao: str | None = Field(default=None, max_length=600)


class AgendamentoConfirmado(BaseModel):
    status: str
    mensagem: str
    protocolo: int | None = None
    movimento_id: int | None = None
    item_movimento_id: int | None = None
    horario_id: int
    agenda_id: int
    whatsapp_status: str | None = None
    whatsapp_mensagem: str | None = None


class AtendimentoAgendamentoMv(BaseModel):
    cd_it_agenda_central: int
    cd_agenda_central: int
    cd_paciente: int
    cd_item_agendamento: int
    cd_atendimento: int
    dh_atendimento: datetime


class CandidatoAtendimentoProcedimentoMv(BaseModel):
    cd_atendimento: int
    cd_paciente: int
    dh_atendimento: datetime
    codigo_procedimento: str
    descricao_procedimento: str | None = None


class ReagendarAgendamentoInput(ConfirmarAgendamentoInput):
    cd_it_agenda_central_anterior: int = Field(gt=0)
    motivo: str = Field(min_length=1, max_length=200)


class AgendamentoReagendado(AgendamentoConfirmado):
    horario_anterior_id: int


class OrientacaoExame(BaseModel):
    cd_item_agendamento: int
    cd_exa_rx: int | None = None
    ds_exa_rx: str | None = None
    orientacoes: list[str]
    total: int


class EventoHistoricoAgendamento(BaseModel):
    tipo: str = Field(
        description=(
            "Código de operação do MV. Somente 'A' é interpretado pela API "
            'como criação do agendamento; os demais códigos são opacos.'
        )
    )
    data_hora: datetime | None = None
    usuario: str | None = None
    descricao: str | None = None


class ItemHistoricoAgendamento(BaseModel):
    cd_it_agenda_central: int
    protocolo: int | None = None
    horario: datetime
    cd_item_agendamento: int
    ds_item_agendamento: str
    nm_prestador: str | None = None
    ds_unidade_atendimento: str | None = None
    status_mv: str | None = None
    status_descricao: str
    observacao: str | None = None
    observacao_geral: str | None = None
    usuario_agendou: str | None = None
    usuario_agendou_status: Literal['confirmado', 'ausente', 'ambiguo'] = Field(
        default='ausente',
        description='Confiabilidade da identificação do autor do agendamento.',
    )
    ultima_operacao: EventoHistoricoAgendamento | None = Field(default=None)
    historico_operacoes: list[EventoHistoricoAgendamento] = Field(
        default_factory=list
    )
    historico_operacoes_status: Literal[
        'disponivel', 'ausente', 'ambiguo'
    ] = Field(default='ausente')
    usuario_responsavel: str | None = Field(default=None, deprecated=True)
    origem: str | None = None
    origem_status: Literal[
        'confirmada',
        'provavel',
        'nao_identificada',
        'temporariamente_indisponivel',
    ]
    evidencia_origem: str | None = None
    cd_it_agenda_central_anterior: int | None = None
    eventos: list[EventoHistoricoAgendamento] = Field(
        default_factory=list,
        deprecated=True,
    )


class HistoricoAgendamentosInterno(BaseModel):
    agendamentos: list[ItemHistoricoAgendamento]
    total: int
    pagina: int
    limite: int
