"""Controle auditável de check-in, check-out e justificativas de plantões."""

from datetime import date, datetime, timedelta
from http import HTTPStatus
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app_prontocardio.agenda_medica_models import AgendaMedicaVinculo
from app_prontocardio.database import get_session_oracle, get_session_postgres
from app_prontocardio.models import Usuario
from app_prontocardio.pronto_escala_models import (
    ProntoEscalaEvento,
    ProntoEscalaJustificativa,
    ProntoEscalaParametro,
    ProntoEscalaPlantao,
    ProntoEscalaRegistro,
)
from app_prontocardio.security import valida_token_usuario_atual
from app_prontocardio.services.agenda_medica_soulmv import AgendaMedicaSoulMV

router = APIRouter(prefix='/pronto-escala', tags=['pronto-escala'])
TELA_MEDICO = 'pronto_escala_medico'
TELA_VALIDADOR = 'pronto_escala_validador'
FUSO = ZoneInfo('America/Sao_Paulo')
ULTIMO_MES = 12

SessionPostgres = Annotated[Session, Depends(get_session_postgres)]
SessionOracle = Annotated[Session, Depends(get_session_oracle)]
UsuarioAtual = Annotated[Usuario, Depends(valida_token_usuario_atual)]


def _agora() -> datetime:
    return datetime.now(FUSO).replace(tzinfo=None)


def _tem_tela(usuario: Usuario, tela: str) -> bool:
    return tela in (usuario.telas_permitidas or [])


def _proibido(mensagem: str) -> HTTPException:
    return HTTPException(status_code=HTTPStatus.FORBIDDEN, detail=mensagem)


def medico_atual(
    usuario: UsuarioAtual, session: SessionPostgres
) -> tuple[Usuario, int]:
    if not _tem_tela(usuario, TELA_MEDICO):
        raise _proibido('Acesso restrito aos médicos do Pronto Escala.')
    vinculo = session.scalar(
        select(AgendaMedicaVinculo).where(
            AgendaMedicaVinculo.usuario_id == usuario.id
        )
    )
    if vinculo is None:
        raise _proibido('Usuário sem prestador vinculado.')
    return usuario, vinculo.cd_prestador


def validador_atual(usuario: UsuarioAtual) -> Usuario:
    if not _tem_tela(usuario, TELA_VALIDADOR):
        raise _proibido('Acesso restrito ao administrador-validador.')
    return usuario


Medico = Annotated[tuple[Usuario, int], Depends(medico_atual)]
Validador = Annotated[Usuario, Depends(validador_atual)]


class ParametrosInput(BaseModel):
    antecedencia_checkin_min: int = Field(ge=0, le=720)
    atraso_checkin_min: int = Field(ge=0, le=720)
    antecedencia_checkout_min: int = Field(ge=0, le=720)
    atraso_checkout_min: int = Field(ge=0, le=720)


class PlantaoInput(BaseModel):
    cd_prestador: int = Field(gt=0)
    nm_prestador: str = Field(min_length=2, max_length=200)
    setor: str = Field(min_length=2, max_length=160)
    inicio: datetime
    fim: datetime
    cd_agenda_central: int | None = Field(default=None, gt=0)
    origem: Literal['MANUAL', 'SOULMV'] = 'MANUAL'

    @model_validator(mode='after')
    def validar_periodo(self):
        if self.fim <= self.inicio:
            raise ValueError('O fim do plantão deve ser posterior ao início.')
        return self


class SincronizacaoInput(BaseModel):
    dias: int = Field(default=30, ge=0, le=180)


class JustificativaInput(BaseModel):
    plantao_id: int = Field(gt=0)
    tipo: Literal['CHECKIN', 'CHECKOUT']
    texto: str = Field(min_length=5, max_length=2000)
    modo: Literal['salvar', 'enviar'] = 'enviar'


class DecisaoInput(BaseModel):
    decisao: Literal['DEFERIDA', 'INDEFERIDA']
    motivo: str | None = Field(default=None, max_length=2000)

    @model_validator(mode='after')
    def exigir_motivo(self):
        if self.decisao == 'INDEFERIDA' and not (self.motivo or '').strip():
            raise ValueError('Informe o motivo do indeferimento.')
        return self


def _parametros(session: Session) -> ProntoEscalaParametro:
    item = session.scalar(select(ProntoEscalaParametro).limit(1))
    if item is None:
        item = ProntoEscalaParametro()
        session.add(item)
        session.flush()
    return item


def _evento(  # noqa: PLR0913
    session: Session,
    usuario: Usuario,
    acao: str,
    *,
    plantao_id: int | None = None,
    justificativa_id: int | None = None,
    detalhe: str | None = None,
) -> None:
    session.add(
        ProntoEscalaEvento(
            acao=acao,
            usuario_id=usuario.id,
            usuario_nome=usuario.nome,
            plantao_id=plantao_id,
            justificativa_id=justificativa_id,
            detalhe=detalhe,
        )
    )


def _registro(session: Session, plantao_id: int, tipo: str):
    return session.scalar(
        select(ProntoEscalaRegistro).where(
            ProntoEscalaRegistro.plantao_id == plantao_id,
            ProntoEscalaRegistro.tipo == tipo,
        )
    )


def _dispensa_checkin(session: Session, plantao: ProntoEscalaPlantao) -> bool:
    anterior = session.scalar(
        select(ProntoEscalaPlantao).where(
            ProntoEscalaPlantao.cd_prestador == plantao.cd_prestador,
            ProntoEscalaPlantao.setor == plantao.setor,
            ProntoEscalaPlantao.fim == plantao.inicio,
            ProntoEscalaPlantao.id != plantao.id,
        )
    )
    return (
        anterior is not None
        and _registro(session, anterior.id, 'CHECKIN') is not None
    )


def _limite_justificativa(plantao: ProntoEscalaPlantao) -> date:
    ano = plantao.inicio.year + (
        1 if plantao.inicio.month == ULTIMO_MES else 0
    )
    mes = 1 if plantao.inicio.month == ULTIMO_MES else plantao.inicio.month + 1
    return date(ano, mes, 5)


def _contagem_mes(
    session: Session, cd_prestador: int, referencia: datetime
) -> int:
    inicio = datetime(referencia.year, referencia.month, 1)
    proximo = datetime(
        referencia.year + (1 if referencia.month == ULTIMO_MES else 0),
        1 if referencia.month == ULTIMO_MES else referencia.month + 1,
        1,
    )
    return (
        session.scalar(
            select(func.count(ProntoEscalaJustificativa.id))
            .join(
                ProntoEscalaPlantao,
                ProntoEscalaPlantao.id == ProntoEscalaJustificativa.plantao_id,
            )
            .where(
                ProntoEscalaPlantao.cd_prestador == cd_prestador,
                ProntoEscalaPlantao.inicio >= inicio,
                ProntoEscalaPlantao.inicio < proximo,
                ProntoEscalaJustificativa.status != 'RASCUNHO',
            )
        )
        or 0
    )


def _plantao_json(session: Session, item: ProntoEscalaPlantao) -> dict:
    registros = session.scalars(
        select(ProntoEscalaRegistro).where(
            ProntoEscalaRegistro.plantao_id == item.id
        )
    ).all()
    return {
        'id': item.id,
        'cd_prestador': item.cd_prestador,
        'nm_prestador': item.nm_prestador,
        'setor': item.setor,
        'inicio': item.inicio,
        'fim': item.fim,
        'status': item.status,
        'cd_agenda_central': item.cd_agenda_central,
        'origem': item.origem,
        'dispensa_checkin_continuidade': _dispensa_checkin(session, item),
        'limite_justificativa': _limite_justificativa(item),
        'registros': {
            registro.tipo.lower(): registro.registrado_em
            for registro in registros
        },
    }


@router.get('/me')
def meu_acesso(usuario: UsuarioAtual, session: SessionPostgres):
    vinculo = session.scalar(
        select(AgendaMedicaVinculo).where(
            AgendaMedicaVinculo.usuario_id == usuario.id
        )
    )
    return {
        'medico': _tem_tela(usuario, TELA_MEDICO),
        'validador': _tem_tela(usuario, TELA_VALIDADOR),
        'cd_prestador': vinculo.cd_prestador if vinculo else None,
    }


@router.get('/parametros')
def obter_parametros(_: Validador, session: SessionPostgres):
    return _parametros(session)


@router.put('/parametros')
def alterar_parametros(
    dados: ParametrosInput, usuario: Validador, session: SessionPostgres
):
    item = _parametros(session)
    for campo, valor in dados.model_dump().items():
        setattr(item, campo, valor)
    item.atualizado_por_id = usuario.id
    item.atualizado_por_nome = usuario.nome
    _evento(
        session,
        usuario,
        'PARAMETROS_ALTERADOS',
        detalhe=str(dados.model_dump()),
    )
    session.commit()
    session.refresh(item)
    return item


@router.post('/plantoes', status_code=HTTPStatus.CREATED)
def criar_plantao(
    dados: PlantaoInput, usuario: Validador, session: SessionPostgres
):
    item = ProntoEscalaPlantao(
        **dados.model_dump(),
        criado_por_id=usuario.id,
        criado_por_nome=usuario.nome,
    )
    session.add(item)
    session.flush()
    _evento(session, usuario, 'PLANTAO_CRIADO', plantao_id=item.id)
    session.commit()
    session.refresh(item)
    return _plantao_json(session, item)


def _data_hora_soulmv(valor: object) -> datetime:
    if isinstance(valor, datetime):
        return valor.replace(tzinfo=None)
    return datetime.fromisoformat(str(valor)).replace(tzinfo=None)


@router.post('/plantoes/sincronizar-soulmv')
def sincronizar_plantoes_soulmv(
    dados: SincronizacaoInput,
    usuario: Validador,
    postgres: SessionPostgres,
    oracle: SessionOracle,
):
    """Importa agendas reais do SoulMV sem escrever no Oracle.

    A chave da agenda central torna a operação idempotente. Alterações de
    prestador, setor ou período feitas no MV são refletidas no registro local,
    preservando os apontamentos e a trilha de auditoria já existentes.
    """
    agendas = AgendaMedicaSoulMV(oracle).agendas_atuais(dados.dias)
    criados = atualizados = ignorados = 0
    ids: list[int] = []
    for agenda in agendas:
        cd_agenda = agenda.get('cd_agenda_central')
        cd_prestador = agenda.get('cd_prestador')
        inicio = agenda.get('hr_inicio')
        fim = agenda.get('hr_fim')
        if not all((cd_agenda, cd_prestador, inicio, fim)):
            ignorados += 1
            continue
        valores = {
            'cd_prestador': int(cd_prestador),
            'nm_prestador': str(
                agenda.get('nm_prestador') or f'Prestador {cd_prestador}'
            ),
            'setor': str(
                agenda.get('nm_setor')
                or agenda.get('ds_unidade_atendimento')
                or f'Setor {agenda.get("cd_setor") or "não informado"}'
            ),
            'inicio': _data_hora_soulmv(inicio),
            'fim': _data_hora_soulmv(fim),
            'origem': 'SOULMV',
            'status': 'PROGRAMADO',
        }
        item = postgres.scalar(
            select(ProntoEscalaPlantao).where(
                ProntoEscalaPlantao.cd_agenda_central == int(cd_agenda)
            )
        )
        if item is None:
            item = ProntoEscalaPlantao(
                **valores,
                cd_agenda_central=int(cd_agenda),
                criado_por_id=usuario.id,
                criado_por_nome=usuario.nome,
            )
            postgres.add(item)
            postgres.flush()
            _evento(
                postgres,
                usuario,
                'PLANTAO_IMPORTADO_SOULMV',
                plantao_id=item.id,
                detalhe=f'Agenda central {cd_agenda}',
            )
            criados += 1
        else:
            mudou = any(
                getattr(item, campo) != valor
                for campo, valor in valores.items()
            )
            if mudou:
                for campo, valor in valores.items():
                    setattr(item, campo, valor)
                _evento(
                    postgres,
                    usuario,
                    'PLANTAO_ATUALIZADO_SOULMV',
                    plantao_id=item.id,
                    detalhe=f'Agenda central {cd_agenda}',
                )
                atualizados += 1
        ids.append(item.id)
    _evento(
        postgres,
        usuario,
        'SINCRONIZACAO_SOULMV',
        detalhe=(
            f'dias={dados.dias}; lidas={len(agendas)}; criadas={criados}; '
            f'atualizadas={atualizados}; ignoradas={ignorados}'
        ),
    )
    postgres.commit()
    return {
        'fonte': 'SoulMV/AGENDA_CENTRAL',
        'somente_leitura_oracle': True,
        'lidas': len(agendas),
        'criadas': criados,
        'atualizadas': atualizados,
        'ignoradas': ignorados,
        'plantao_ids': ids,
    }


@router.get('/plantoes')
def listar_plantoes(
    session: SessionPostgres,
    usuario: UsuarioAtual,
    inicio: date | None = Query(default=None),
    fim: date | None = Query(default=None),
):
    medico = _tem_tela(usuario, TELA_MEDICO)
    validador = _tem_tela(usuario, TELA_VALIDADOR)
    if not medico and not validador:
        raise _proibido('Sem acesso ao Pronto Escala.')
    consulta = select(ProntoEscalaPlantao).order_by(ProntoEscalaPlantao.inicio)
    if medico and not validador:
        vinculo = session.scalar(
            select(AgendaMedicaVinculo).where(
                AgendaMedicaVinculo.usuario_id == usuario.id
            )
        )
        if vinculo is None:
            raise _proibido('Usuário sem prestador vinculado.')
        consulta = consulta.where(
            ProntoEscalaPlantao.cd_prestador == vinculo.cd_prestador
        )
    if inicio:
        consulta = consulta.where(
            ProntoEscalaPlantao.inicio
            >= datetime.combine(inicio, datetime.min.time())
        )
    if fim:
        consulta = consulta.where(
            ProntoEscalaPlantao.inicio
            < datetime.combine(fim + timedelta(days=1), datetime.min.time())
        )
    return {
        'plantoes': [
            _plantao_json(session, item)
            for item in session.scalars(consulta).all()
        ]
    }


def _marcar(
    tipo: str, plantao_id: int, medico: tuple[Usuario, int], session: Session
):
    usuario, cd_prestador = medico
    plantao = session.get(ProntoEscalaPlantao, plantao_id)
    if plantao is None or plantao.cd_prestador != cd_prestador:
        raise HTTPException(
            status_code=HTTPStatus.NOT_FOUND, detail='Plantão não encontrado.'
        )
    if _registro(session, plantao.id, tipo):
        raise HTTPException(
            status_code=HTTPStatus.CONFLICT,
            detail=f'{tipo.title()} já registrado.',
        )
    parametros = _parametros(session)
    agora = _agora()
    if tipo == 'CHECKIN' and _dispensa_checkin(session, plantao):
        raise HTTPException(
            status_code=HTTPStatus.CONFLICT,
            detail='Novo check-in dispensado por continuidade no mesmo setor.',
        )
    base = plantao.inicio if tipo == 'CHECKIN' else plantao.fim
    antes = (
        parametros.antecedencia_checkin_min
        if tipo == 'CHECKIN'
        else parametros.antecedencia_checkout_min
    )
    depois = (
        parametros.atraso_checkin_min
        if tipo == 'CHECKIN'
        else parametros.atraso_checkout_min
    )
    if (
        not base - timedelta(minutes=antes)
        <= agora
        <= base + timedelta(minutes=depois)
    ):
        raise HTTPException(
            status_code=HTTPStatus.CONFLICT,
            detail=(
                'Registro fora da janela permitida. Envie uma justificativa.'
            ),
        )
    registro = ProntoEscalaRegistro(
        plantao_id=plantao.id,
        tipo=tipo,
        registrado_em=agora,
        usuario_id=usuario.id,
        usuario_nome=usuario.nome,
    )
    session.add(registro)
    _evento(session, usuario, tipo, plantao_id=plantao.id)
    session.commit()
    return {'plantao_id': plantao.id, 'tipo': tipo, 'registrado_em': agora}


@router.post('/plantoes/{plantao_id}/check-in')
def checkin(plantao_id: int, medico: Medico, session: SessionPostgres):
    return _marcar('CHECKIN', plantao_id, medico, session)


@router.post('/plantoes/{plantao_id}/check-out')
def checkout(plantao_id: int, medico: Medico, session: SessionPostgres):
    return _marcar('CHECKOUT', plantao_id, medico, session)


@router.post('/justificativas', status_code=HTTPStatus.CREATED)
def justificar(
    dados: JustificativaInput, medico: Medico, session: SessionPostgres
):
    usuario, cd_prestador = medico
    plantao = session.get(ProntoEscalaPlantao, dados.plantao_id)
    if plantao is None or plantao.cd_prestador != cd_prestador:
        raise HTTPException(
            status_code=HTTPStatus.NOT_FOUND, detail='Plantão não encontrado.'
        )
    if _registro(session, plantao.id, dados.tipo):
        raise HTTPException(
            status_code=HTTPStatus.CONFLICT,
            detail=(
                'O apontamento já foi registrado e não exige justificativa.'
            ),
        )
    if _agora().date() > _limite_justificativa(plantao):
        raise HTTPException(
            status_code=HTTPStatus.CONFLICT,
            detail=(
                'Prazo de justificativa encerrado no dia 5 do mês seguinte.'
            ),
        )
    conteudo = dados.model_dump(exclude={'modo'})
    item = ProntoEscalaJustificativa(
        **conteudo,
        usuario_id=usuario.id,
        usuario_nome=usuario.nome,
        status='RASCUNHO' if dados.modo == 'salvar' else 'PENDENTE',
    )
    session.add(item)
    try:
        session.flush()
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(
            status_code=HTTPStatus.CONFLICT,
            detail='Justificativa já registrada para este apontamento.',
        ) from exc
    _evento(
        session,
        usuario,
        (
            'JUSTIFICATIVA_SALVA'
            if dados.modo == 'salvar'
            else 'JUSTIFICATIVA_ENVIADA'
        ),
        plantao_id=plantao.id,
        justificativa_id=item.id,
    )
    session.commit()
    return {
        'id': item.id,
        'status': item.status,
        'justificativas_no_mes': _contagem_mes(
            session, cd_prestador, plantao.inicio
        ),
    }


@router.post('/justificativas/{justificativa_id}/enviar')
def enviar_justificativa(
    justificativa_id: int,
    medico: Medico,
    session: SessionPostgres,
):
    usuario, cd_prestador = medico
    item = session.get(ProntoEscalaJustificativa, justificativa_id)
    if item is None:
        raise HTTPException(
            status_code=HTTPStatus.NOT_FOUND,
            detail='Justificativa não encontrada.',
        )
    plantao = session.get(ProntoEscalaPlantao, item.plantao_id)
    if plantao.cd_prestador != cd_prestador:
        raise _proibido('A justificativa pertence a outro prestador.')
    if item.status != 'RASCUNHO':
        raise HTTPException(
            status_code=HTTPStatus.CONFLICT,
            detail='A justificativa já foi enviada.',
        )
    if _agora().date() > _limite_justificativa(plantao):
        raise HTTPException(
            status_code=HTTPStatus.CONFLICT,
            detail='Prazo de justificativa encerrado.',
        )
    item.status = 'PENDENTE'
    _evento(
        session,
        usuario,
        'JUSTIFICATIVA_ENVIADA',
        plantao_id=plantao.id,
        justificativa_id=item.id,
    )
    session.commit()
    return {
        'id': item.id,
        'status': item.status,
        'justificativas_no_mes': _contagem_mes(
            session, cd_prestador, plantao.inicio
        ),
    }


@router.get('/justificativas')
def listar_justificativas(
    usuario: Validador, session: SessionPostgres, status: str | None = None
):
    consulta = select(ProntoEscalaJustificativa).order_by(
        ProntoEscalaJustificativa.criado_em.desc()
    )
    if status:
        consulta = consulta.where(
            ProntoEscalaJustificativa.status == status.upper()
        )
    itens = []
    for item in session.scalars(consulta).all():
        plantao = session.get(ProntoEscalaPlantao, item.plantao_id)
        itens.append({
            'id': item.id,
            'plantao_id': item.plantao_id,
            'tipo': item.tipo,
            'texto': item.texto,
            'status': item.status,
            'criado_em': item.criado_em,
            'nm_prestador': plantao.nm_prestador,
            'setor': plantao.setor,
            'inicio': plantao.inicio,
            'justificativas_no_mes': _contagem_mes(
                session, plantao.cd_prestador, plantao.inicio
            ),
        })
    return {'justificativas': itens}


@router.post('/justificativas/{justificativa_id}/decisao')
def decidir(
    justificativa_id: int,
    dados: DecisaoInput,
    usuario: Validador,
    session: SessionPostgres,
):
    item = session.get(ProntoEscalaJustificativa, justificativa_id)
    if item is None:
        raise HTTPException(
            status_code=HTTPStatus.NOT_FOUND,
            detail='Justificativa não encontrada.',
        )
    if item.status != 'PENDENTE':
        raise HTTPException(
            status_code=HTTPStatus.CONFLICT,
            detail='Justificativa já analisada.',
        )
    item.status = dados.decisao
    item.motivo_decisao = dados.motivo
    item.decisao_por_id = usuario.id
    item.decisao_por_nome = usuario.nome
    item.decisao_em = _agora()
    _evento(
        session,
        usuario,
        f'JUSTIFICATIVA_{dados.decisao}',
        plantao_id=item.plantao_id,
        justificativa_id=item.id,
        detalhe=dados.motivo,
    )
    session.commit()
    return {
        'id': item.id,
        'status': item.status,
        'decisao_em': item.decisao_em,
    }


@router.get('/eventos')
def listar_eventos(
    _: Validador,
    session: SessionPostgres,
    limite: int = Query(default=100, ge=1, le=500),
):
    return {
        'eventos': session.scalars(
            select(ProntoEscalaEvento)
            .order_by(ProntoEscalaEvento.criado_em.desc())
            .limit(limite)
        ).all()
    }
