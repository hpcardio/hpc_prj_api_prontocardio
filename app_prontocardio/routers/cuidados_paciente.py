from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app_prontocardio.database import get_session_oracle, get_session_postgres
from app_prontocardio.models import (
    RespostaPosAlta,
    SolicitacaoPaciente,
    Usuario,
)
from app_prontocardio.routers.paciente_auth import (
    PrincipalPaciente,
    validar_paciente_atual,
)
from app_prontocardio.security import valida_token_usuario_atual

router = APIRouter(prefix='/paciente/cuidados', tags=['paciente-cuidados'])
operational_router = APIRouter(
    prefix='/operacional/cuidados',
    tags=['operacional-cuidados'],
)

EtapaPosAlta = Literal[
    'first_contact',
    'second_contact',
    'one_week',
    'thirty_days',
]
DestinoSolicitacao = Literal['c3', 'call_center', 'autorizacao', 'enfermagem']
UsuarioOperacional = Annotated[Usuario, Depends(valida_token_usuario_atual)]


class RespostaPosAltaInput(BaseModel):
    etapa: EtapaPosAlta
    cd_atendimento: int = Field(gt=0)
    respostas: dict[str, bool]
    chave_idempotencia: str = Field(
        min_length=8,
        max_length=60,
        pattern=r'^[A-Za-z0-9._:-]+$',
    )


class SolicitacaoPacienteInput(BaseModel):
    tipo: str = Field(min_length=3, max_length=40)
    destino: DestinoSolicitacao
    assunto: str = Field(min_length=3, max_length=300)
    contexto: dict = Field(default_factory=dict)
    chave_idempotencia: str = Field(
        min_length=8,
        max_length=60,
        pattern=r'^[A-Za-z0-9._:-]+$',
    )


EXPECTED_ANSWERS = {
    'first_contact': {
        'well': True,
        'symptoms': False,
        'procedure_problem': False,
        'medication': True,
    },
    'second_contact': {
        'well': True,
        'symptoms': False,
        'procedure_problem': False,
        'medication': True,
    },
    'one_week': {
        'well': True,
        'symptoms': False,
        'procedure_problem': False,
        'medication': True,
    },
    'thirty_days': {
        'well': True,
        'symptoms': False,
        'emergency_return': False,
        'medication': True,
    },
}

STAGE_DELAYS = (
    ('first_contact', timedelta(hours=24)),
    ('second_contact', timedelta(hours=72)),
    ('one_week', timedelta(days=7)),
    ('thirty_days', timedelta(days=30)),
)


def resolver_etapa_pendente(
    *,
    data_alta: datetime,
    etapas_respondidas: set[str],
    agora: datetime | None = None,
) -> str | None:
    referencia = agora or datetime.now(timezone.utc)
    alta = (
        data_alta
        if data_alta.tzinfo is not None
        else data_alta.replace(tzinfo=timezone.utc)
    )
    for etapa, atraso in STAGE_DELAYS:
        if referencia >= alta + atraso and etapa not in etapas_respondidas:
            return etapa
    return None


def _serializar_solicitacao(
    registro: SolicitacaoPaciente,
    *,
    idempotente: bool,
) -> dict:
    return {
        'id': registro.id,
        'destino': registro.destino,
        'status': registro.status,
        'idempotente': idempotente,
    }


@router.get('/pos-alta/pendente')
def consultar_pos_alta_pendente(
    paciente: PrincipalPaciente = Depends(validar_paciente_atual),
    oracle: Session = Depends(get_session_oracle),
    postgres: Session = Depends(get_session_postgres),
):
    alta = (
        oracle
        .execute(
            text(
                """
            SELECT cd_atendimento, data_alta
              FROM (
                    SELECT a.CD_ATENDIMENTO AS cd_atendimento,
                           a.DT_ALTA AS data_alta
                      FROM DBAMV.ATENDIME a
                     WHERE a.CD_PACIENTE = :cd_paciente
                       AND a.TP_ATENDIMENTO = 'I'
                       AND a.DT_ALTA IS NOT NULL
                     ORDER BY a.DT_ALTA DESC
              )
             WHERE ROWNUM = 1
            """
            ),
            {'cd_paciente': paciente.cd_paciente},
        )
        .mappings()
        .first()
    )
    if alta is None:
        return {'pendente': False}

    cd_atendimento = int(alta['cd_atendimento'])
    respondidas = set(
        postgres.scalars(
            select(RespostaPosAlta.etapa).where(
                RespostaPosAlta.cd_paciente == paciente.cd_paciente,
                RespostaPosAlta.cd_atendimento == cd_atendimento,
            )
        ).all()
    )
    etapa = resolver_etapa_pendente(
        data_alta=alta['data_alta'],
        etapas_respondidas=respondidas,
    )
    if etapa is None:
        return {'pendente': False}
    return {
        'pendente': True,
        'cd_atendimento': cd_atendimento,
        'etapa': etapa,
    }


@router.post('/solicitacoes', status_code=HTTPStatus.CREATED)
def criar_solicitacao(
    payload: SolicitacaoPacienteInput,
    response: Response,
    paciente: PrincipalPaciente = Depends(validar_paciente_atual),
    session: Session = Depends(get_session_postgres),
):
    existente = session.scalar(
        select(SolicitacaoPaciente).where(
            SolicitacaoPaciente.cd_paciente == paciente.cd_paciente,
            SolicitacaoPaciente.chave_idempotencia
            == payload.chave_idempotencia,
        )
    )
    if existente is not None:
        response.status_code = HTTPStatus.OK
        return _serializar_solicitacao(existente, idempotente=True)

    registro = SolicitacaoPaciente(
        chave_idempotencia=payload.chave_idempotencia,
        cd_paciente=paciente.cd_paciente,
        tipo=payload.tipo,
        destino=payload.destino,
        assunto=payload.assunto.strip(),
        contexto=payload.contexto,
    )
    session.add(registro)
    session.commit()
    session.refresh(registro)
    return _serializar_solicitacao(registro, idempotente=False)


@operational_router.get('/solicitacoes')
def listar_solicitacoes(
    _: UsuarioOperacional,
    destino: DestinoSolicitacao,
    session: Session = Depends(get_session_postgres),
):
    registros = session.scalars(
        select(SolicitacaoPaciente)
        .where(
            SolicitacaoPaciente.destino == destino,
            SolicitacaoPaciente.status == 'pendente',
        )
        .order_by(SolicitacaoPaciente.data_criacao)
        .limit(500)
    ).all()
    return {
        'solicitacoes': [
            {
                'id': registro.id,
                'cd_paciente': registro.cd_paciente,
                'tipo': registro.tipo,
                'destino': registro.destino,
                'assunto': registro.assunto,
                'contexto': registro.contexto,
                'status': registro.status,
                'data_criacao': registro.data_criacao,
            }
            for registro in registros
        ],
        'total': len(registros),
    }


@router.post('/pos-alta/respostas', status_code=HTTPStatus.CREATED)
def registrar_respostas_pos_alta(
    payload: RespostaPosAltaInput,
    response: Response,
    paciente: PrincipalPaciente = Depends(validar_paciente_atual),
    session: Session = Depends(get_session_postgres),
):
    esperado = EXPECTED_ANSWERS[payload.etapa]
    if set(payload.respostas) != set(esperado):
        raise HTTPException(
            status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
            detail='Conjunto de respostas inválido para esta etapa.',
        )

    existente = session.scalar(
        select(RespostaPosAlta).where(
            RespostaPosAlta.cd_paciente == paciente.cd_paciente,
            RespostaPosAlta.chave_idempotencia == payload.chave_idempotencia,
        )
    )
    if existente is not None:
        response.status_code = HTTPStatus.OK
        return {
            'id': existente.id,
            'resultado': existente.resultado,
            'idempotente': True,
        }

    alterada = any(
        payload.respostas[chave] != valor for chave, valor in esperado.items()
    )
    resultado = 'encaminhar_c3' if alterada else 'tranquilizar'
    registro = RespostaPosAlta(
        chave_idempotencia=payload.chave_idempotencia,
        cd_paciente=paciente.cd_paciente,
        cd_atendimento=payload.cd_atendimento,
        etapa=payload.etapa,
        respostas=payload.respostas,
        resultado=resultado,
    )
    session.add(registro)
    if alterada:
        session.add(
            SolicitacaoPaciente(
                chave_idempotencia=f'c3-{payload.chave_idempotencia}',
                cd_paciente=paciente.cd_paciente,
                tipo='acompanhamento_pos_alta',
                destino='c3',
                assunto='Resposta alterada no acompanhamento pós-alta',
                contexto={
                    'cd_atendimento': payload.cd_atendimento,
                    'etapa': payload.etapa,
                    'respostas': payload.respostas,
                },
            )
        )
    session.commit()
    session.refresh(registro)
    return {
        'id': registro.id,
        'resultado': registro.resultado,
        'idempotente': False,
    }
