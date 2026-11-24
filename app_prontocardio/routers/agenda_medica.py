"""Agenda Médica: ofertas de grade, pedidos dos médicos e decisões.

Os pedidos ficam no PostgreSQL da API até a decisão. Quando a chave
transacional está habilitada, a aprovação administrativa executa a rotina
oficial da MV; com a chave desligada, devolve apenas o plano técnico.
"""

import os
from datetime import date, datetime
from http import HTTPStatus
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app_prontocardio.agenda_medica_models import (
    AgendaMedicaContraproposta,
    AgendaMedicaEvento,
    AgendaMedicaOferta,
    AgendaMedicaSolicitacao,
    AgendaMedicaTratativaPaciente,
    AgendaMedicaTriagem,
    AgendaMedicaVinculo,
)
from app_prontocardio.database import get_session_oracle, get_session_postgres
from app_prontocardio.models import Usuario
from app_prontocardio.security import valida_token_usuario_atual
from app_prontocardio.services.agenda_medica_soulmv import (
    DIAS_SEMANA,
    AgendaMedicaSoulMV,
    BloqueioAgendaErro,
    GeracaoAgendaErro,
    plano_geracao,
    tp_dia_semana_de,
)

router = APIRouter(prefix='/agenda-medica', tags=['agenda-medica'])

TELA_MEDICO = 'agenda_medica_medico'
TELA_ATENDENTE = 'agenda_medica_atendente'
TELA_ADMIN = 'agenda_medica_admin'
TELA_RESPONSAVEL = 'agenda_medica_responsavel'
TELA_EVOLUCOES = 'agenda_medica_evolucoes'
PERFIS_GESTAO = {'ti', 'administrador'}
USUARIO_MV = 'AGENDA_MEDICA'
# Usuário do MV registrado na geração (GERACAO_AGENDA.CD_USUARIO / FNC).
USUARIO_MV_GRAVACAO = os.getenv('AGENDA_MEDICA_USUARIO_MV', 'DBAMV')


def gravacao_soulmv_habilitada() -> bool:
    """Chave própria da Agenda Médica; começa desligada."""
    return (
        os.getenv('AGENDA_MEDICA_MV_GRAVACAO_HABILITADA', 'false').lower()
        == 'true'
    )


def cancelamento_soulmv_habilitado() -> bool:
    """Chave independente para o bloqueio de agendas após aprovação."""
    return (
        os.getenv('AGENDA_MEDICA_MV_CANCELAMENTO_HABILITADO', 'false').lower()
        == 'true'
    )


MOTIVO_MINIMO = 5
FUSO_HORARIO_BRASIL = ZoneInfo('America/Sao_Paulo')

SessionPostgres = Annotated[Session, Depends(get_session_postgres)]
UsuarioAtual = Annotated[Usuario, Depends(valida_token_usuario_atual)]


def get_soulmv_agenda_medica(
    session: Session = Depends(get_session_oracle),
) -> AgendaMedicaSoulMV:
    return AgendaMedicaSoulMV(session)


SoulMV = Annotated[AgendaMedicaSoulMV, Depends(get_soulmv_agenda_medica)]


# ---------------------------------------------------------------- acesso


def _tem_tela(usuario: Usuario, tela: str) -> bool:
    if usuario.perfil in PERFIS_GESTAO:
        return True
    return tela in (usuario.telas_permitidas or [])


def _proibido(mensagem: str) -> HTTPException:
    return HTTPException(status_code=HTTPStatus.FORBIDDEN, detail=mensagem)


def acesso_admin(usuario: UsuarioAtual) -> Usuario:
    if not _tem_tela(usuario, TELA_ADMIN):
        raise _proibido('Acesso restrito ao administrador de agendas.')
    return usuario


def acesso_responsavel(usuario: UsuarioAtual) -> Usuario:
    if not _tem_tela(usuario, TELA_RESPONSAVEL):
        raise _proibido('Acesso restrito ao responsável institucional.')
    return usuario


def acesso_leitura(usuario: UsuarioAtual) -> Usuario:
    telas = (TELA_MEDICO, TELA_ATENDENTE, TELA_ADMIN, TELA_RESPONSAVEL)
    if not any(_tem_tela(usuario, tela) for tela in telas):
        raise _proibido('Sem acesso à Agenda Médica.')
    return usuario


def medico_atual(
    usuario: UsuarioAtual, session: SessionPostgres
) -> tuple[Usuario, int]:
    if TELA_MEDICO not in (usuario.telas_permitidas or []):
        raise _proibido('Acesso restrito a médicos da Agenda Médica.')
    vinculo = session.scalar(
        select(AgendaMedicaVinculo).where(
            AgendaMedicaVinculo.usuario_id == usuario.id
        )
    )
    if vinculo is None:
        raise _proibido(
            'Usuário sem prestador vinculado. Peça ao administrador.'
        )
    return usuario, vinculo.cd_prestador


Admin = Annotated[Usuario, Depends(acesso_admin)]
Responsavel = Annotated[Usuario, Depends(acesso_responsavel)]
Leitor = Annotated[Usuario, Depends(acesso_leitura)]
Medico = Annotated[tuple[Usuario, int], Depends(medico_atual)]


# ---------------------------------------------------------------- schemas


class VinculoInput(BaseModel):
    usuario_id: int = Field(gt=0)
    cd_prestador: int = Field(gt=0)
    # Sem nível informado: mantém o atual (ou 'pleno' num vínculo novo).
    nivel: Literal['senior', 'pleno'] | None = None


class OfertaInput(BaseModel):
    cd_escala_modelo: int = Field(gt=0)
    data_agenda: date
    especialidade: str = Field(min_length=2, max_length=120)
    prazo_interesse: datetime
    observacao: str | None = Field(default=None, max_length=1000)


class InteresseAdministrativoInput(BaseModel):
    cd_prestador: int = Field(gt=0)


class PropostaGradeInput(BaseModel):
    cd_escala_modelo: int = Field(gt=0)
    data_agenda: date
    hr_inicio: str = Field(pattern=r'^([01]\d|2[0-3]):[0-5]\d$')
    hr_fim: str = Field(pattern=r'^([01]\d|2[0-3]):[0-5]\d$')
    especialidade: str = Field(min_length=2, max_length=120)
    observacao: str | None = Field(default=None, max_length=1000)
    cd_prestador: int | None = Field(default=None, gt=0)


class CancelamentoInput(BaseModel):
    cd_agenda_central: int = Field(gt=0)
    motivo: str = Field(min_length=MOTIVO_MINIMO, max_length=1000)


class DecisaoInput(BaseModel):
    acao: Literal['aprovar', 'recusar']
    motivo: str | None = Field(default=None, max_length=1000)


class ContrapropostaInput(BaseModel):
    data_agenda: date
    hr_inicio: str = Field(pattern=r'^([01]\d|2[0-3]):[0-5]\d$')
    hr_fim: str = Field(pattern=r'^([01]\d|2[0-3]):[0-5]\d$')
    motivo: str = Field(min_length=MOTIVO_MINIMO, max_length=1000)


class RespostaContrapropostaInput(BaseModel):
    acao: Literal['aceitar', 'recusar']
    motivo: str | None = Field(default=None, max_length=1000)


# ---------------------------------------------------------------- apoio


def _registrar(  # noqa: PLR0913, PLR0917
    session: Session,
    usuario: Usuario,
    acao: str,
    detalhe: str | None = None,
    oferta_id: int | None = None,
    solicitacao_id: int | None = None,
) -> None:
    session.add(
        AgendaMedicaEvento(
            acao=acao,
            usuario_id=usuario.id,
            usuario_nome=usuario.nome,
            oferta_id=oferta_id,
            solicitacao_id=solicitacao_id,
            detalhe=detalhe,
        )
    )


def _oferta_dict(oferta: AgendaMedicaOferta) -> dict:
    return {
        'id': oferta.id,
        'cd_escala_modelo': oferta.cd_escala_modelo,
        'data_agenda': oferta.data_agenda.isoformat(),
        'especialidade': oferta.especialidade,
        'hr_inicio': oferta.hr_inicio,
        'hr_fim': oferta.hr_fim,
        'cd_unidade_atendimento': oferta.cd_unidade_atendimento,
        'ds_unidade_atendimento': oferta.ds_unidade_atendimento,
        'observacao': oferta.observacao,
        'prazo_interesse': (
            oferta.prazo_interesse.isoformat()
            if oferta.prazo_interesse
            else None
        ),
        'status': oferta.status,
        'criado_por': oferta.criado_por_nome,
    }


def _solicitacao_dict(item: AgendaMedicaSolicitacao) -> dict:
    return {
        'id': item.id,
        'tipo': item.tipo,
        'status': item.status,
        'cd_prestador': item.cd_prestador,
        'oferta_id': item.oferta_id,
        'cd_agenda_central': item.cd_agenda_central,
        'motivo': item.motivo,
        'criado_por': item.criado_por_nome,
        'decisao_por': item.decisao_por_nome,
        'decisao_em': item.decisao_em.isoformat() if item.decisao_em else None,
        'resultado_mv': item.resultado_mv,
    }


def _contraproposta_dict(item: AgendaMedicaContraproposta) -> dict:
    return {
        'id': item.id,
        'solicitacao_id': item.solicitacao_id,
        'data_agenda': item.data_agenda.isoformat(),
        'hr_inicio': item.hr_inicio,
        'hr_fim': item.hr_fim,
        'motivo': item.motivo,
        'status': item.status,
        'criado_por': item.criado_por_nome,
        'criado_em': item.criado_em.isoformat() if item.criado_em else None,
        'respondido_por': item.respondido_por_nome,
        'respondido_em': (
            item.respondido_em.isoformat() if item.respondido_em else None
        ),
        'resposta_motivo': item.resposta_motivo,
    }


def _nao_encontrado(mensagem: str) -> HTTPException:
    return HTTPException(status_code=HTTPStatus.NOT_FOUND, detail=mensagem)


def _conflito(mensagem) -> HTTPException:
    return HTTPException(status_code=HTTPStatus.CONFLICT, detail=mensagem)


# ---------------------------------------------------------------- consultas


@router.get('/me')
def quem_sou(usuario: Leitor, session: SessionPostgres):
    """Papéis do usuário na Agenda Médica e o prestador que ele representa."""
    vinculo = session.scalar(
        select(AgendaMedicaVinculo).where(
            AgendaMedicaVinculo.usuario_id == usuario.id
        )
    )
    papeis = [
        papel
        for papel, tela in (
            ('medico', TELA_MEDICO),
            ('atendente', TELA_ATENDENTE),
            ('admin', TELA_ADMIN),
            ('responsavel', TELA_RESPONSAVEL),
        )
        if tela in (usuario.telas_permitidas or [])
        or (papel != 'medico' and usuario.perfil in PERFIS_GESTAO)
    ]
    return {
        'id': usuario.id,
        'nome': usuario.nome,
        'perfil': usuario.perfil,
        'papeis': papeis,
        'cd_prestador': vinculo.cd_prestador if vinculo else None,
        'nivel': vinculo.nivel if vinculo else None,
    }


@router.get('/status')
def status_agenda_medica(_: Leitor):
    grade = gravacao_soulmv_habilitada()
    cancelamento = cancelamento_soulmv_habilitado()
    return {
        'gravacao_soulmv': grade,
        'cancelamento_soulmv': cancelamento,
        'mensagem': (
            f'Criação de grade: {"habilitada" if grade else "protegida"}. '
            f'Bloqueio após cancelamento: '
            f'{"habilitado" if cancelamento else "protegido"}.'
        ),
    }


@router.get('/escalas-modelo')
def listar_escalas_modelo(
    _: Leitor,
    soulmv: SoulMV,
    cd_unidade: int | None = Query(default=None, gt=0),
    tp_dia_semana: int | None = Query(default=None, ge=1, le=7),
):
    return {'escalas': soulmv.escalas_modelo(cd_unidade, tp_dia_semana)}


@router.get('/agendas')
def listar_agendas_atuais(
    _: Leitor,
    soulmv: SoulMV,
    dias: int = Query(default=30, ge=1, le=90),
):
    """Visão operacional atual do SoulMV, consultada a cada carregamento."""
    agendas = soulmv.agendas_atuais(dias)
    vagas = sum(int(item.get('qt_atendimento') or 0) for item in agendas)
    marcados = sum(int(item.get('qt_marcados') or 0) for item in agendas)
    livres = sum(int(item.get('qt_livres') or 0) for item in agendas)
    bloqueados = sum(int(item.get('qt_bloqueados') or 0) for item in agendas)
    return {
        'consultado_em': datetime.now(FUSO_HORARIO_BRASIL).isoformat(),
        'dias': dias,
        'summary': {
            'agendas': len(agendas),
            'vagas_configuradas': vagas,
            'horarios_marcados': marcados,
            'vagas_livres': livres,
            'horarios_bloqueados': bloqueados,
        },
        'agendas': agendas,
    }


@router.get('/agendas/{cd_agenda_central}/horarios')
def detalhar_horarios_agenda(
    cd_agenda_central: int,
    _: Leitor,
    soulmv: SoulMV,
):
    """Detalhamento analítico e atual dos horários de uma agenda do SoulMV."""
    horarios = soulmv.horarios_agenda(cd_agenda_central)
    if not horarios:
        raise _nao_encontrado('Agenda não encontrada ou sem horários.')
    resumo = {
        'total': len(horarios),
        'ocupados': 0,
        'livres': 0,
        'bloqueados': 0,
    }
    for horario in horarios:
        status = horario.get('status_horario')
        if status == 'OCUPADO':
            resumo['ocupados'] += 1
        elif status == 'LIVRE':
            resumo['livres'] += 1
        else:
            resumo['bloqueados'] += 1
    return {
        'consultado_em': datetime.now(FUSO_HORARIO_BRASIL).isoformat(),
        'cd_agenda_central': cd_agenda_central,
        'resumo': resumo,
        'horarios': horarios,
    }


# ---------------------------------------------------------------- vínculos


@router.post('/vinculos', status_code=HTTPStatus.CREATED)
def vincular_prestador(
    payload: VinculoInput,
    usuario: Admin,
    session: SessionPostgres,
    soulmv: SoulMV,
):
    if session.get(Usuario, payload.usuario_id) is None:
        raise _nao_encontrado('Usuário não encontrado.')
    prestador = soulmv.prestador(payload.cd_prestador)
    if prestador is None:
        raise _nao_encontrado('Prestador não encontrado no SoulMV.')
    vinculo = session.scalar(
        select(AgendaMedicaVinculo).where(
            AgendaMedicaVinculo.usuario_id == payload.usuario_id
        )
    )
    if vinculo is None:
        vinculo = AgendaMedicaVinculo(
            usuario_id=payload.usuario_id,
            cd_prestador=payload.cd_prestador,
            criado_por=usuario.nome,
            nivel=payload.nivel or 'pleno',
        )
        session.add(vinculo)
    else:
        vinculo.cd_prestador = payload.cd_prestador
        if payload.nivel:
            vinculo.nivel = payload.nivel
    _registrar(
        session,
        usuario,
        'VINCULO_PRESTADOR',
        f'usuario {payload.usuario_id} -> prestador {payload.cd_prestador}'
        f' ({vinculo.nivel})',
    )
    session.commit()
    return {
        'usuario_id': payload.usuario_id,
        'cd_prestador': payload.cd_prestador,
        'nm_prestador': prestador.get('nm_prestador'),
        'nivel': vinculo.nivel,
    }


@router.get('/vinculos')
def listar_vinculos(_: Admin, session: SessionPostgres):
    vinculos = session.scalars(select(AgendaMedicaVinculo)).all()
    return {
        'vinculos': [
            {
                'usuario_id': v.usuario_id,
                'cd_prestador': v.cd_prestador,
                'nivel': v.nivel,
            }
            for v in vinculos
        ]
    }


# ---------------------------------------------------------------- ofertas


@router.post('/ofertas', status_code=HTTPStatus.CREATED)
def criar_oferta(
    payload: OfertaInput,
    usuario: Admin,
    session: SessionPostgres,
    soulmv: SoulMV,
):
    if payload.data_agenda <= date.today():
        raise _conflito('A data da grade precisa ser futura.')
    escala = soulmv.escala_modelo(payload.cd_escala_modelo)
    if escala is None:
        raise _nao_encontrado('Escala-modelo não encontrada no SoulMV.')
    agora = datetime.now(FUSO_HORARIO_BRASIL).replace(tzinfo=None)
    prazo = payload.prazo_interesse.replace(tzinfo=None)
    inicio_grade = datetime.combine(
        payload.data_agenda,
        datetime.strptime(escala['hr_inicio'], '%H:%M').time(),
    )
    if prazo <= agora:
        raise _conflito('O prazo para interesse precisa ser futuro.')
    if prazo >= inicio_grade:
        raise _conflito(
            'O prazo para interesse precisa terminar antes do início da grade.'
        )
    dia = tp_dia_semana_de(payload.data_agenda)
    if escala['tp_dia_semana'] != dia:
        esperado = DIAS_SEMANA.get(escala['tp_dia_semana'], '?')
        raise _conflito(
            f'A escala-modelo é de {esperado}; escolha uma data nesse dia.'
        )
    oferta = AgendaMedicaOferta(
        cd_escala_modelo=payload.cd_escala_modelo,
        data_agenda=payload.data_agenda,
        especialidade=payload.especialidade.strip().upper(),
        hr_inicio=escala['hr_inicio'],
        hr_fim=escala['hr_fim'],
        cd_unidade_atendimento=escala['cd_unidade_atendimento'],
        criado_por_id=usuario.id,
        criado_por_nome=usuario.nome,
        ds_unidade_atendimento=(
            escala.get('ds_unidade_atendimento') or ''
        ).strip()
        or None,
        observacao=payload.observacao,
        prazo_interesse=prazo,
    )
    session.add(oferta)
    session.flush()
    _registrar(
        session,
        usuario,
        'OFERTA_CRIADA',
        f'escala-modelo {payload.cd_escala_modelo} em {payload.data_agenda}',
        oferta_id=oferta.id,
    )
    session.commit()
    return {**_oferta_dict(oferta), 'escala': escala}


@router.get('/ofertas')
def listar_ofertas(
    _: Leitor,
    session: SessionPostgres,
    status: str | None = Query(default='ABERTA'),
):
    consulta = select(AgendaMedicaOferta).order_by(
        AgendaMedicaOferta.data_agenda
    )
    if status:
        consulta = consulta.where(AgendaMedicaOferta.status == status)
    return {'ofertas': [_oferta_dict(o) for o in session.scalars(consulta)]}


@router.post('/ofertas/{oferta_id}/interesse', status_code=HTTPStatus.CREATED)
def manifestar_interesse(
    oferta_id: int, medico: Medico, session: SessionPostgres
):
    usuario, cd_prestador = medico
    oferta = session.get(AgendaMedicaOferta, oferta_id)
    if oferta is None:
        raise _nao_encontrado('Oferta não encontrada.')
    if oferta.status != 'ABERTA':
        raise _conflito('Esta grade não está mais aberta.')
    repetido = session.scalar(
        select(AgendaMedicaSolicitacao).where(
            AgendaMedicaSolicitacao.oferta_id == oferta_id,
            AgendaMedicaSolicitacao.cd_prestador == cd_prestador,
            AgendaMedicaSolicitacao.status.in_(('PENDENTE', 'APROVADA')),
        )
    )
    if repetido is not None:
        raise _conflito('Você já tem um pedido para esta grade.')
    pedido = AgendaMedicaSolicitacao(
        tipo='INTERESSE',
        cd_prestador=cd_prestador,
        criado_por_id=usuario.id,
        criado_por_nome=usuario.nome,
        oferta_id=oferta_id,
    )
    session.add(pedido)
    session.flush()
    _registrar(
        session,
        usuario,
        'INTERESSE_MANIFESTADO',
        oferta_id=oferta_id,
        solicitacao_id=pedido.id,
    )
    session.commit()
    return _solicitacao_dict(pedido)


@router.post(
    '/ofertas/{oferta_id}/interesse-administrativo',
    status_code=HTTPStatus.CREATED,
)
def manifestar_interesse_administrativo(
    oferta_id: int,
    payload: InteresseAdministrativoInput,
    usuario: Admin,
    session: SessionPostgres,
    soulmv: SoulMV,
):
    """Registra, com auditoria, o interesse lançado por um administrador."""
    oferta = session.get(AgendaMedicaOferta, oferta_id)
    if oferta is None:
        raise _nao_encontrado('Oferta não encontrada.')
    if oferta.status != 'ABERTA':
        raise _conflito('Esta grade não está mais aberta.')
    if soulmv.prestador(payload.cd_prestador) is None:
        raise _nao_encontrado('Prestador não encontrado no SoulMV.')
    repetido = session.scalar(
        select(AgendaMedicaSolicitacao).where(
            AgendaMedicaSolicitacao.oferta_id == oferta_id,
            AgendaMedicaSolicitacao.cd_prestador == payload.cd_prestador,
            AgendaMedicaSolicitacao.status.in_(('PENDENTE', 'APROVADA')),
        )
    )
    if repetido is not None:
        raise _conflito('Este médico já tem um pedido para esta grade.')
    pedido = AgendaMedicaSolicitacao(
        tipo='INTERESSE',
        cd_prestador=payload.cd_prestador,
        criado_por_id=usuario.id,
        criado_por_nome=usuario.nome,
        oferta_id=oferta_id,
    )
    session.add(pedido)
    session.flush()
    _registrar(
        session,
        usuario,
        'INTERESSE_ADMINISTRATIVO',
        'interesse lançado pelo administrador para prestador '
        f'{payload.cd_prestador}',
        oferta_id=oferta_id,
        solicitacao_id=pedido.id,
    )
    session.commit()
    return _solicitacao_dict(pedido)


@router.post('/propostas-grade', status_code=HTTPStatus.CREATED)
def propor_grade(
    payload: PropostaGradeInput,
    usuario: UsuarioAtual,
    session: SessionPostgres,
    soulmv: SoulMV,
):
    """Registra uma sugestão do médico sem gravar nada no SoulMV."""
    if payload.cd_prestador is not None and _tem_tela(usuario, TELA_ADMIN):
        cd_prestador = payload.cd_prestador
    else:
        _, cd_prestador = medico_atual(usuario, session)
    if payload.data_agenda <= date.today():
        raise _conflito('A data sugerida precisa ser futura.')
    if payload.hr_fim <= payload.hr_inicio:
        raise _conflito('O horário final precisa ser posterior ao inicial.')
    escala = soulmv.escala_modelo(payload.cd_escala_modelo)
    if escala is None:
        raise _nao_encontrado('Escala-modelo não encontrada no SoulMV.')
    dia = tp_dia_semana_de(payload.data_agenda)
    if escala['tp_dia_semana'] != dia:
        esperado = DIAS_SEMANA.get(escala['tp_dia_semana'], '?')
        raise _conflito(
            f'A escala-modelo é de {esperado}; escolha uma data nesse dia.'
        )
    if soulmv.prestador(cd_prestador) is None:
        raise _nao_encontrado('Prestador não encontrado no SoulMV.')
    repetida = session.scalar(
        select(AgendaMedicaSolicitacao)
        .join(
            AgendaMedicaOferta,
            AgendaMedicaOferta.id == AgendaMedicaSolicitacao.oferta_id,
        )
        .where(
            AgendaMedicaSolicitacao.cd_prestador == cd_prestador,
            AgendaMedicaSolicitacao.status == 'PENDENTE',
            AgendaMedicaOferta.status == 'PROPOSTA',
            AgendaMedicaOferta.data_agenda == payload.data_agenda,
        )
    )
    if repetida is not None:
        raise _conflito('Já existe uma sugestão pendente para esta data.')
    oferta = AgendaMedicaOferta(
        cd_escala_modelo=payload.cd_escala_modelo,
        data_agenda=payload.data_agenda,
        especialidade=payload.especialidade.strip().upper(),
        hr_inicio=payload.hr_inicio,
        hr_fim=payload.hr_fim,
        cd_unidade_atendimento=escala['cd_unidade_atendimento'],
        criado_por_id=usuario.id,
        criado_por_nome=usuario.nome,
        ds_unidade_atendimento=(
            escala.get('ds_unidade_atendimento') or ''
        ).strip()
        or None,
        observacao=(payload.observacao or '').strip() or None,
        prazo_interesse=None,
        status='PROPOSTA',
    )
    session.add(oferta)
    session.flush()
    pedido = AgendaMedicaSolicitacao(
        tipo='INTERESSE',
        cd_prestador=cd_prestador,
        criado_por_id=usuario.id,
        criado_por_nome=usuario.nome,
        oferta_id=oferta.id,
        motivo='Sugestão de nova grade enviada pelo médico.',
    )
    session.add(pedido)
    session.flush()
    _registrar(
        session,
        usuario,
        'PROPOSTA_GRADE_CRIADA',
        f'prestador {cd_prestador}; escala-modelo '
        f'{payload.cd_escala_modelo}; data {payload.data_agenda}',
        oferta_id=oferta.id,
        solicitacao_id=pedido.id,
    )
    session.commit()
    return {
        'oferta': _oferta_dict(oferta),
        'solicitacao': _solicitacao_dict(pedido),
        'escala': escala,
    }


# ---------------------------------------------------------------- pedidos


@router.post('/cancelamentos', status_code=HTTPStatus.CREATED)
def pedir_cancelamento(
    payload: CancelamentoInput,
    medico: Medico,
    session: SessionPostgres,
    soulmv: SoulMV,
):
    usuario, cd_prestador = medico
    agenda = soulmv.agenda(payload.cd_agenda_central)
    if agenda is None or agenda['cd_prestador'] != cd_prestador:
        raise _nao_encontrado('Agenda não encontrada para este médico.')
    data_agenda = agenda['dt_agenda']
    if isinstance(data_agenda, datetime):
        data_agenda = data_agenda.date()
    if data_agenda <= date.today():
        raise _conflito('Só é possível pedir cancelamento de agenda futura.')
    repetido = session.scalar(
        select(AgendaMedicaSolicitacao).where(
            AgendaMedicaSolicitacao.tipo == 'CANCELAMENTO',
            AgendaMedicaSolicitacao.cd_agenda_central
            == payload.cd_agenda_central,
            AgendaMedicaSolicitacao.status == 'PENDENTE',
        )
    )
    if repetido is not None:
        raise _conflito('Já existe um pedido pendente para esta agenda.')
    pedido = AgendaMedicaSolicitacao(
        tipo='CANCELAMENTO',
        cd_prestador=cd_prestador,
        criado_por_id=usuario.id,
        criado_por_nome=usuario.nome,
        cd_agenda_central=payload.cd_agenda_central,
        motivo=payload.motivo.strip(),
    )
    session.add(pedido)
    session.flush()
    _registrar(
        session,
        usuario,
        'CANCELAMENTO_SOLICITADO',
        payload.motivo.strip(),
        solicitacao_id=pedido.id,
    )
    session.commit()
    return _solicitacao_dict(pedido)


@router.get('/solicitacoes')
def listar_solicitacoes(
    usuario: Leitor,
    session: SessionPostgres,
    tipo: Literal['INTERESSE', 'CANCELAMENTO'] | None = None,
    status: str | None = None,
):
    consulta = select(AgendaMedicaSolicitacao).order_by(
        AgendaMedicaSolicitacao.id.desc()
    )
    if tipo:
        consulta = consulta.where(AgendaMedicaSolicitacao.tipo == tipo)
    if status:
        consulta = consulta.where(AgendaMedicaSolicitacao.status == status)
    so_medico = not (
        _tem_tela(usuario, TELA_ADMIN) or _tem_tela(usuario, TELA_RESPONSAVEL)
    )
    if so_medico:
        vinculo = session.scalar(
            select(AgendaMedicaVinculo).where(
                AgendaMedicaVinculo.usuario_id == usuario.id
            )
        )
        if vinculo is None:
            return {'solicitacoes': []}
        consulta = consulta.where(
            AgendaMedicaSolicitacao.cd_prestador == vinculo.cd_prestador
        )
    return {
        'solicitacoes': [
            _solicitacao_dict(s) for s in session.scalars(consulta)
        ]
    }


@router.get('/contrapropostas')
def listar_contrapropostas(
    usuario: Leitor,
    session: SessionPostgres,
    status: str | None = None,
):
    consulta = select(AgendaMedicaContraproposta).order_by(
        AgendaMedicaContraproposta.id.desc()
    )
    if status:
        consulta = consulta.where(AgendaMedicaContraproposta.status == status)
    if not (_tem_tela(usuario, TELA_ADMIN) or _tem_tela(usuario, TELA_RESPONSAVEL)):
        vinculo = session.scalar(
            select(AgendaMedicaVinculo).where(
                AgendaMedicaVinculo.usuario_id == usuario.id
            )
        )
        if vinculo is None:
            return {'contrapropostas': []}
        consulta = consulta.join(
            AgendaMedicaSolicitacao,
            AgendaMedicaSolicitacao.id
            == AgendaMedicaContraproposta.solicitacao_id,
        ).where(
            AgendaMedicaSolicitacao.cd_prestador == vinculo.cd_prestador
        )
    return {
        'contrapropostas': [
            _contraproposta_dict(item) for item in session.scalars(consulta)
        ]
    }


@router.post(
    '/solicitacoes/{solicitacao_id}/contrapropostas',
    status_code=HTTPStatus.CREATED,
)
def criar_contraproposta(
    solicitacao_id: int,
    payload: ContrapropostaInput,
    usuario: Admin,
    session: SessionPostgres,
):
    pedido = session.get(AgendaMedicaSolicitacao, solicitacao_id)
    if pedido is None or pedido.tipo != 'INTERESSE':
        raise _nao_encontrado('Solicitação de grade não encontrada.')
    if pedido.status != 'PENDENTE':
        raise _conflito('Somente solicitações pendentes aceitam contraproposta.')
    if payload.data_agenda <= date.today():
        raise _conflito('A nova data precisa ser futura.')
    if payload.hr_fim <= payload.hr_inicio:
        raise _conflito('O horário final precisa ser posterior ao inicial.')
    aberta = session.scalar(
        select(AgendaMedicaContraproposta).where(
            AgendaMedicaContraproposta.solicitacao_id == solicitacao_id,
            AgendaMedicaContraproposta.status == 'AGUARDANDO_MEDICO',
        )
    )
    if aberta is not None:
        raise _conflito('Já existe uma contraproposta aguardando o médico.')
    item = AgendaMedicaContraproposta(
        solicitacao_id=solicitacao_id,
        data_agenda=payload.data_agenda,
        hr_inicio=payload.hr_inicio,
        hr_fim=payload.hr_fim,
        motivo=payload.motivo.strip(),
        criado_por_id=usuario.id,
        criado_por_nome=usuario.nome,
    )
    session.add(item)
    session.flush()
    _registrar(
        session,
        usuario,
        'CONTRAPROPOSTA_CRIADA',
        f'{payload.data_agenda} {payload.hr_inicio}-{payload.hr_fim}: '
        f'{payload.motivo.strip()}',
        solicitacao_id=solicitacao_id,
    )
    session.commit()
    session.refresh(item)
    return _contraproposta_dict(item)


@router.post('/contrapropostas/{contraproposta_id}/resposta')
def responder_contraproposta(
    contraproposta_id: int,
    payload: RespostaContrapropostaInput,
    medico: Medico,
    session: SessionPostgres,
):
    usuario, cd_prestador = medico
    item = session.get(AgendaMedicaContraproposta, contraproposta_id)
    if item is None:
        raise _nao_encontrado('Contraproposta não encontrada.')
    pedido = session.get(AgendaMedicaSolicitacao, item.solicitacao_id)
    if pedido is None or pedido.cd_prestador != cd_prestador:
        raise _proibido('Esta contraproposta pertence a outro profissional.')
    if item.status != 'AGUARDANDO_MEDICO' or pedido.status != 'PENDENTE':
        raise _conflito('Esta contraproposta não está mais aguardando resposta.')
    oferta = session.get(AgendaMedicaOferta, pedido.oferta_id)
    if oferta is None:
        raise _nao_encontrado('Oferta vinculada não encontrada.')
    aceita = payload.acao == 'aceitar'
    item.status = 'ACEITA' if aceita else 'RECUSADA'
    item.respondido_por_id = usuario.id
    item.respondido_por_nome = usuario.nome
    item.resposta_motivo = (payload.motivo or '').strip() or None
    item.respondido_em = datetime.now(FUSO_HORARIO_BRASIL).replace(tzinfo=None)
    if aceita:
        oferta.data_agenda = item.data_agenda
        oferta.hr_inicio = item.hr_inicio
        oferta.hr_fim = item.hr_fim
    _registrar(
        session,
        usuario,
        'CONTRAPROPOSTA_ACEITA' if aceita else 'CONTRAPROPOSTA_RECUSADA',
        item.resposta_motivo,
        oferta_id=oferta.id,
        solicitacao_id=pedido.id,
    )
    session.commit()
    session.refresh(item)
    return _contraproposta_dict(item)


# ---------------------------------------------------------------- decisões


def _avaliar_interesse(
    pedido: AgendaMedicaSolicitacao,
    oferta: AgendaMedicaOferta,
    session: Session,
    soulmv: AgendaMedicaSoulMV,
) -> tuple[list[str], dict]:
    """Regras do SoulMV para aprovar o médico; retorna (pendências, plano)."""
    pendencias: list[str] = []
    if oferta.data_agenda <= date.today():
        pendencias.append('A data da grade já passou.')
    if oferta.status not in ('ABERTA', 'PROPOSTA'):
        pendencias.append('Esta grade já foi preenchida ou cancelada.')
    escala = soulmv.escala_modelo(oferta.cd_escala_modelo)
    if escala is None:
        pendencias.append('A escala-modelo não está mais ativa no SoulMV.')
        return pendencias, {}
    if oferta.status == 'PROPOSTA' and (
        oferta.hr_inicio != escala['hr_inicio']
        or oferta.hr_fim != escala['hr_fim']
    ):
        pendencias.append(
            'O horário sugerido não corresponde ao modelo do SoulMV. '
            'A gestão deve recusar esta proposta ou criar uma escala-modelo '
            'compatível antes da aprovação.'
        )
    especialidades = soulmv.especialidades_prestador(pedido.cd_prestador)
    if oferta.especialidade not in especialidades:
        pendencias.append(
            f'O médico não tem {oferta.especialidade} cadastrada no SoulMV.'
        )
    conflitos = soulmv.conflitos(
        pedido.cd_prestador,
        oferta.data_agenda,
        oferta.hr_inicio,
        oferta.hr_fim,
    )
    if conflitos:
        agendas = ', '.join(str(c['cd_agenda_central']) for c in conflitos)
        pendencias.append(f'Conflito com agenda(s) no SoulMV: {agendas}.')
    outro_aprovado = session.scalar(
        select(AgendaMedicaSolicitacao).where(
            AgendaMedicaSolicitacao.oferta_id == oferta.id,
            AgendaMedicaSolicitacao.status == 'APROVADA',
            AgendaMedicaSolicitacao.id != pedido.id,
        )
    )
    if outro_aprovado is not None:
        pendencias.append('Esta grade já foi aprovada para outro médico.')
    plano = plano_geracao(
        escala, pedido.cd_prestador, oferta.data_agenda, USUARIO_MV
    )
    return pendencias, plano


def _resultado_aprovacao(
    pedido: AgendaMedicaSolicitacao,
    oferta: AgendaMedicaOferta,
    plano: dict,
    soulmv: AgendaMedicaSoulMV,
) -> dict:
    """Gravação ligada: gera a agenda no SoulMV; desligada: devolve o plano."""
    if not gravacao_soulmv_habilitada():
        return {**plano, 'gravado_no_soulmv': False}
    escala = soulmv.escala_modelo(oferta.cd_escala_modelo)
    try:
        return soulmv.gerar_agenda_aprovada(
            escala,
            pedido.cd_prestador,
            oferta.data_agenda,
            USUARIO_MV_GRAVACAO,
        )
    except GeracaoAgendaErro as erro:
        raise _conflito({'pendencias': [str(erro)]}) from erro


def _resultado_cancelamento(
    pedido: AgendaMedicaSolicitacao,
    soulmv: AgendaMedicaSoulMV,
) -> dict:
    agenda = soulmv.agenda(pedido.cd_agenda_central)
    if agenda is None:
        raise _nao_encontrado('Agenda não encontrada no SoulMV.')
    if agenda['qt_marcados'] > 0:
        raise _conflito({
            'pendencias': [
                f'Há {agenda["qt_marcados"]} paciente(s) marcado(s). '
                'Remaneje os pacientes antes de cancelar.'
            ]
        })
    if not cancelamento_soulmv_habilitado():
        return {
            'modo': 'protegido',
            'gravado_no_soulmv': False,
            'orientacao': (
                'A rotina oficial de bloqueio foi identificada, mas a chave '
                'transacional de cancelamento está desligada.'
            ),
            'cd_agenda_central': pedido.cd_agenda_central,
        }
    try:
        return soulmv.bloquear_agenda_aprovada(
            pedido.cd_agenda_central,
            pedido.motivo,
            USUARIO_MV_GRAVACAO,
        )
    except BloqueioAgendaErro as erro:
        raise _conflito({'pendencias': [str(erro)]}) from erro


def _criar_fila_remanejamento(
    pedido: AgendaMedicaSolicitacao,
    usuario: Usuario,
    session: Session,
    soulmv: AgendaMedicaSoulMV,
) -> int:
    """Registra horários ocupados sem copiar nome, contato ou prontuário."""
    consultar_horarios = getattr(soulmv, 'horarios_agenda', None)
    horarios = (
        consultar_horarios(pedido.cd_agenda_central)
        if consultar_horarios is not None
        else []
    )
    ocupados = [
        item
        for item in horarios
        if item.get('cd_paciente') is not None
        and item.get('status_horario') == 'OCUPADO'
    ]
    criados = 0
    for horario in ocupados:
        cd_it = int(horario['cd_it_agenda_central'])
        existente = session.scalar(
            select(AgendaMedicaTratativaPaciente).where(
                AgendaMedicaTratativaPaciente.cd_it_agenda_central == cd_it,
                AgendaMedicaTratativaPaciente.tipo == 'REMANEJAMENTO',
                AgendaMedicaTratativaPaciente.status.in_(
                    ('PENDENTE', 'EM_TRATATIVA')
                ),
            )
        )
        if existente is not None:
            continue
        session.add(
            AgendaMedicaTratativaPaciente(
                cd_it_agenda_central=cd_it,
                cd_agenda_central=pedido.cd_agenda_central,
                solicitacao_id=pedido.id,
                tipo='REMANEJAMENTO',
                status='PENDENTE',
                cd_it_agenda_destino=None,
                valor_anterior=None,
                valor_novo=None,
                motivo='Remanejamento obrigatório antes do cancelamento da agenda.',
                criado_por_id=usuario.id,
                criado_por_nome=usuario.nome,
                concluido_por_id=None,
                concluido_por_nome=None,
                concluido_em=None,
            )
        )
        criados += 1
    _registrar(
        session,
        usuario,
        'REMANEJAMENTO_CRIADO',
        f'{len(ocupados)} horário(s) aguardando remanejamento',
        solicitacao_id=pedido.id,
    )
    session.commit()
    return len(ocupados)


@router.post('/solicitacoes/{solicitacao_id}/decisao')
def decidir_solicitacao(
    solicitacao_id: int,
    payload: DecisaoInput,
    usuario: UsuarioAtual,
    session: SessionPostgres,
    soulmv: SoulMV,
):
    pedido = session.get(AgendaMedicaSolicitacao, solicitacao_id)
    if pedido is None:
        raise _nao_encontrado('Solicitação não encontrada.')
    if pedido.tipo == 'INTERESSE':
        acesso_admin(usuario)
    else:
        acesso_responsavel(usuario)
    if pedido.status != 'PENDENTE':
        raise _conflito('Esta solicitação já foi decidida.')

    oferta = (
        session.get(AgendaMedicaOferta, pedido.oferta_id)
        if pedido.tipo == 'INTERESSE'
        else None
    )

    if payload.acao == 'recusar':
        resultado = {'modo': 'recusado', 'motivo': payload.motivo}
        status = 'RECUSADA'
        if oferta is not None and oferta.status == 'PROPOSTA':
            oferta.status = 'RECUSADA'
    elif pedido.tipo == 'INTERESSE':
        pendencias, plano = _avaliar_interesse(pedido, oferta, session, soulmv)
        if pendencias:
            raise _conflito({'pendencias': pendencias})
        resultado = _resultado_aprovacao(pedido, oferta, plano, soulmv)
        oferta.status = 'PREENCHIDA'
        status = 'APROVADA'
    else:
        agenda_cancelada = soulmv.agenda(pedido.cd_agenda_central)
        if agenda_cancelada is not None and agenda_cancelada['qt_marcados'] > 0:
            total = _criar_fila_remanejamento(
                pedido, usuario, session, soulmv
            )
            total = max(total, int(agenda_cancelada['qt_marcados']))
            raise _conflito({
                'pendencias': [
                    f'{total} paciente(s) precisam ser remanejados antes '
                    'do cancelamento. A fila operacional foi criada.'
                ]
            })
        resultado = _resultado_cancelamento(pedido, soulmv)
        status = 'APROVADA'

    pedido.status = status
    pedido.decisao_por_id = usuario.id
    pedido.decisao_por_nome = usuario.nome
    pedido.decisao_em = datetime.now()
    pedido.resultado_mv = resultado
    _registrar(
        session,
        usuario,
        f'{pedido.tipo}_{status}',
        payload.motivo,
        oferta_id=pedido.oferta_id,
        solicitacao_id=pedido.id,
    )
    session.commit()
    return _solicitacao_dict(pedido)


@router.get('/eventos')
def listar_eventos(
    _: Leitor,
    session: SessionPostgres,
    limite: int = Query(default=100, ge=1, le=500),
):
    eventos = session.scalars(
        select(AgendaMedicaEvento)
        .order_by(AgendaMedicaEvento.id.desc())
        .limit(limite)
    ).all()
    return {
        'eventos': [
            {
                'acao': e.acao,
                'usuario': e.usuario_nome,
                'oferta_id': e.oferta_id,
                'solicitacao_id': e.solicitacao_id,
                'detalhe': e.detalhe,
                'criado_em': e.criado_em.isoformat() if e.criado_em else None,
            }
            for e in eventos
        ]
    }


# ---------------------------------------------------------------- triagem
#
# Médicos sêniores escolhem quais pacientes agendados (consultas
# ambulatoriais dos próximos dias) ficam com eles e quais seguem para os
# demais prestadores. A decisão fica na API e é auditada; o SoulMV não é
# alterado. As evoluções do prontuário só são lidas por médicos sêniores, um
# paciente por vez, e cada consulta fica registrada.

TRIAGEM_DIAS = 30
TRIAGEM_DIAS_MAXIMO = 60


class TriagemDecisaoInput(BaseModel):
    cd_it_agenda_central: int = Field(gt=0)
    # None desfaz a decisão do próprio sênior.
    decisao: Literal['senior', 'demais'] | None


class TriagemEvolucoesInput(BaseModel):
    cd_paciente: int = Field(gt=0)


def medico_senior(
    usuario: UsuarioAtual, session: SessionPostgres
) -> tuple[Usuario, AgendaMedicaVinculo]:
    vinculo = session.scalar(
        select(AgendaMedicaVinculo).where(
            AgendaMedicaVinculo.usuario_id == usuario.id
        )
    )
    if (
        TELA_MEDICO not in (usuario.telas_permitidas or [])
        or vinculo is None
        or vinculo.nivel != 'senior'
    ):
        raise _proibido('Triagem restrita aos médicos de nível sênior.')
    return usuario, vinculo


def acesso_triagem(usuario: UsuarioAtual, session: SessionPostgres) -> Usuario:
    """Sêniores decidem; o administrador acompanha (somente leitura)."""
    if _tem_tela(usuario, TELA_ADMIN):
        return usuario
    return medico_senior(usuario, session)[0]


Senior = Annotated[tuple[Usuario, AgendaMedicaVinculo], Depends(medico_senior)]
LeitorTriagem = Annotated[Usuario, Depends(acesso_triagem)]


def acesso_evolucoes(
    usuario: UsuarioAtual, session: SessionPostgres
) -> Usuario:
    """Autoriza sênior ou usuário com permissão clínica explícita.

    Perfis de gestão não recebem este acesso automaticamente: o prontuário só
    pode ser consultado quando ``agenda_medica_evolucoes`` tiver sido atribuída
    nominalmente ao usuário.
    """
    vinculo = session.scalar(
        select(AgendaMedicaVinculo).where(
            AgendaMedicaVinculo.usuario_id == usuario.id
        )
    )
    eh_senior = (
        TELA_MEDICO in (usuario.telas_permitidas or [])
        and vinculo is not None
        and vinculo.nivel == 'senior'
    )
    acesso_explicito = TELA_EVOLUCOES in (usuario.telas_permitidas or [])
    if not eh_senior and not acesso_explicito:
        raise _proibido(
            'Evoluções restritas a médicos sêniores ou usuários autorizados.'
        )
    return usuario


LeitorEvolucoes = Annotated[Usuario, Depends(acesso_evolucoes)]


def _triagem_dict(item: AgendaMedicaTriagem) -> dict:
    return {
        'cd_it_agenda_central': item.cd_it_agenda_central,
        'decisao': item.decisao,
        'cd_prestador_senior': item.cd_prestador_senior,
        'usuario': item.usuario_nome,
        'decidido_em': (
            item.decidido_em.isoformat() if item.decidido_em else None
        ),
    }


@router.get('/triagem')
def listar_triagem(
    _: LeitorTriagem,
    session: SessionPostgres,
    soulmv: SoulMV,
    dias: int = Query(default=TRIAGEM_DIAS, ge=1, le=TRIAGEM_DIAS_MAXIMO),
):
    pacientes = soulmv.triagem_pacientes(dias)
    itens = {p['cd_it_agenda_central'] for p in pacientes}
    decisoes = session.scalars(select(AgendaMedicaTriagem)).all()
    return {
        'gerado_em': datetime.now().isoformat(timespec='seconds'),
        'dias': dias,
        'pacientes': pacientes,
        'decisoes': [
            _triagem_dict(d)
            for d in decisoes
            if d.cd_it_agenda_central in itens
        ],
    }


@router.post('/triagem/decisoes')
def decidir_triagem(
    payload: TriagemDecisaoInput,
    senior: Senior,
    session: SessionPostgres,
    soulmv: SoulMV,
):
    usuario, vinculo = senior
    item = soulmv.item_triagem(
        payload.cd_it_agenda_central, TRIAGEM_DIAS_MAXIMO
    )
    if item is None:
        raise _nao_encontrado('Agendamento fora da triagem.')
    atual = session.scalar(
        select(AgendaMedicaTriagem).where(
            AgendaMedicaTriagem.cd_it_agenda_central
            == payload.cd_it_agenda_central
        )
    )
    if atual is not None and atual.cd_prestador_senior != vinculo.cd_prestador:
        raise _conflito(f'Paciente já decidido por {atual.usuario_nome}.')
    registro = f'agendamento {payload.cd_it_agenda_central}'
    if payload.decisao is None:
        if atual is None:
            raise _nao_encontrado('Não há decisão para desfazer.')
        session.delete(atual)
        _registrar(session, usuario, 'TRIAGEM_DESFEITA', registro)
        session.commit()
        return {
            'cd_it_agenda_central': payload.cd_it_agenda_central,
            'decisao': None,
        }
    if atual is None:
        atual = AgendaMedicaTriagem(
            cd_it_agenda_central=payload.cd_it_agenda_central,
            cd_paciente=item['cd_paciente'],
            decisao=payload.decisao,
            cd_prestador_senior=vinculo.cd_prestador,
            usuario_id=usuario.id,
            usuario_nome=usuario.nome,
        )
        session.add(atual)
    else:
        atual.decisao = payload.decisao
    acao = (
        'TRIAGEM_SENIOR' if payload.decisao == 'senior' else 'TRIAGEM_DEMAIS'
    )
    _registrar(session, usuario, acao, registro)
    session.commit()
    session.refresh(atual)
    return _triagem_dict(atual)


@router.post('/triagem/evolucoes')
def evolucoes_triagem(
    payload: TriagemEvolucoesInput,
    usuario: LeitorEvolucoes,
    session: SessionPostgres,
    soulmv: SoulMV,
):
    if not soulmv.paciente_na_triagem(
        payload.cd_paciente, TRIAGEM_DIAS_MAXIMO
    ):
        raise _proibido('Paciente fora da triagem.')
    evolucoes = soulmv.evolucoes_paciente(payload.cd_paciente)
    _registrar(
        session,
        usuario,
        'EVOLUCAO_CONSULTADA',
        f'paciente {payload.cd_paciente} · {len(evolucoes)} evolução(ões)',
    )
    session.commit()
    return {'cd_paciente': payload.cd_paciente, 'evolucoes': evolucoes}
