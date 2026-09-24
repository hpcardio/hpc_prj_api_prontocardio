from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from http import HTTPStatus
from threading import Lock

from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field
from sqlalchemy import func, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app_prontocardio.database import get_session_oracle, get_session_postgres
from app_prontocardio.models import DesafioOtpPaciente, SessaoPaciente
from app_prontocardio.routers.livre import (
    CONSULTA_PACIENTE_POR_CPF_NASCIMENTO,
    _somente_digitos,
)
from app_prontocardio.settings import Settings
from app_prontocardio.whatsapp_service import enviar_otp_whatsapp

router = APIRouter(prefix='/paciente', tags=['paciente'])
settings = Settings()
bearer_optional = HTTPBearer(auto_error=False)
TAMANHO_CPF = 11
TAMANHO_TELEFONE_MINIMO = 12
TAMANHO_TELEFONE_MAXIMO = 13
JANELA_LIMITE = timedelta(minutes=15)


class IniciarAutenticacaoPacienteInput(BaseModel):
    cpf: str = Field(min_length=11, max_length=14)
    dt_nascimento: date


class ValidarOtpPacienteInput(BaseModel):
    desafio_id: str = Field(min_length=20, max_length=64)
    codigo: str = Field(pattern=r'^\d{6}$')


class InicioAutenticacaoPaciente(BaseModel):
    desafio_id: str
    mensagem: str
    expira_em_segundos: int
    pode_reenviar_em_segundos: int
    telefone_final: str | None = None
    codigo_teste: str | None = None


class PacienteAutenticado(BaseModel):
    access_token: str
    token_type: str = 'Bearer'
    expires_in: int
    paciente: dict


@dataclass(frozen=True)
class PrincipalPaciente:
    cd_paciente: int
    nome: str
    tipo: str = 'paciente'


@dataclass
class DesafioOtpLocal:
    desafio_id: str
    cd_paciente: int
    nm_paciente: str
    cpf_hash: str
    telefone_final: str
    codigo_hash: str
    expira_em: datetime
    data_criacao: datetime
    tentativas: int = 0
    utilizado_em: datetime | None = None


@dataclass
class SessaoPacienteLocal:
    token_hash: str
    cd_paciente: int
    nm_paciente: str
    expira_em: datetime
    revogado_em: datetime | None = None


_armazenamento_local_lock = Lock()
_desafios_locais: dict[str, DesafioOtpLocal] = {}
_sessoes_locais: dict[str, SessaoPacienteLocal] = {}


def _usar_memoria_local() -> bool:
    return (
        settings.PATIENT_AUTH_LOCAL_TEST_MODE
        and settings.PATIENT_AUTH_LOCAL_MEMORY_STORE
    )


def _agora() -> datetime:
    return datetime.now(timezone.utc)


def _com_timezone(valor: datetime) -> datetime:
    return valor if valor.tzinfo else valor.replace(tzinfo=timezone.utc)


def _hash(valor: str) -> str:
    return hashlib.sha256(valor.encode('utf-8')).hexdigest()


def _hash_secreto(*partes: str) -> str:
    mensagem = ':'.join(partes).encode('utf-8')
    return hmac.new(
        settings.SECRET_KEY.encode('utf-8'), mensagem, hashlib.sha256
    ).hexdigest()


def _telefone_paciente(row: dict) -> str | None:
    ddd = _somente_digitos(row.get('nr_ddd_celular')) or ''
    numero = _somente_digitos(row.get('nr_celular')) or ''
    if not ddd or not numero:
        return None
    telefone = f'55{ddd}{numero}'
    tamanho_valido = (
        TAMANHO_TELEFONE_MINIMO
        <= len(telefone)
        <= TAMANHO_TELEFONE_MAXIMO
    )
    return telefone if tamanho_valido else None


def resolver_sessao_paciente(
    session: Session,
    token: str,
) -> PrincipalPaciente | None:
    if _usar_memoria_local():
        with _armazenamento_local_lock:
            registro_local = _sessoes_locais.get(_hash(token))
            if (
                registro_local is None
                or registro_local.revogado_em is not None
                or registro_local.expira_em <= _agora()
            ):
                return None
            return PrincipalPaciente(
                cd_paciente=registro_local.cd_paciente,
                nome=registro_local.nm_paciente,
            )
    registro = session.scalar(
        select(SessaoPaciente).where(
            SessaoPaciente.token_hash == _hash(token),
            SessaoPaciente.revogado_em.is_(None),
        )
    )
    if registro is None or _com_timezone(registro.expira_em) <= _agora():
        return None
    return PrincipalPaciente(
        cd_paciente=registro.cd_paciente,
        nome=registro.nm_paciente,
    )


def validar_paciente_atual(
    credenciais: HTTPAuthorizationCredentials | None = Depends(bearer_optional),
    session: Session = Depends(get_session_postgres),
) -> PrincipalPaciente:
    if credenciais is None or credenciais.scheme.lower() != 'bearer':
        raise HTTPException(
            status_code=HTTPStatus.UNAUTHORIZED,
            detail='Sessao do paciente necessaria.',
        )
    principal = resolver_sessao_paciente(session, credenciais.credentials)
    if principal is None:
        raise HTTPException(
            status_code=HTTPStatus.UNAUTHORIZED,
            detail='Sessao expirada ou invalida.',
        )
    return principal


@router.post(
    '/auth/iniciar',
    response_model=InicioAutenticacaoPaciente,
    status_code=HTTPStatus.OK,
)
def iniciar_autenticacao_paciente(  # noqa: PLR0912
    payload: IniciarAutenticacaoPacienteInput,
    oracle: Session = Depends(get_session_oracle),
    postgres: Session = Depends(get_session_postgres),
):
    cpf = _somente_digitos(payload.cpf) or ''
    if len(cpf) != TAMANHO_CPF:
        raise HTTPException(
            status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
            detail='CPF invalido.',
        )

    cpf_hash = _hash_secreto('cpf', cpf)
    inicio_janela = _agora() - JANELA_LIMITE
    if _usar_memoria_local():
        with _armazenamento_local_lock:
            solicitacoes = sum(
                1
                for desafio in _desafios_locais.values()
                if desafio.cpf_hash == cpf_hash
                and desafio.data_criacao >= inicio_janela
            )
    else:
        solicitacoes = postgres.scalar(
            select(func.count(DesafioOtpPaciente.id)).where(
                DesafioOtpPaciente.cpf_hash == cpf_hash,
                DesafioOtpPaciente.data_criacao >= inicio_janela,
            )
        )
    if (solicitacoes or 0) >= settings.PATIENT_OTP_MAX_REQUESTS_15_MIN:
        raise HTTPException(
            status_code=HTTPStatus.TOO_MANY_REQUESTS,
            detail='Muitas tentativas. Aguarde alguns minutos para tentar novamente.',
        )

    desafio_id = secrets.token_urlsafe(32)
    resposta = {
        'desafio_id': desafio_id,
        'mensagem': (
            'Se os dados estiverem corretos, enviaremos um codigo ao WhatsApp '
            'cadastrado no hospital.'
        ),
        'expira_em_segundos': settings.PATIENT_OTP_TTL_SECONDS,
        'pode_reenviar_em_segundos': settings.PATIENT_OTP_RESEND_SECONDS,
        'telefone_final': None,
        'codigo_teste': None,
    }
    try:
        row = (
            oracle.execute(
                CONSULTA_PACIENTE_POR_CPF_NASCIMENTO,
                {
                    'cpf': cpf,
                    'dt_nascimento': payload.dt_nascimento.isoformat(),
                },
            )
            .mappings()
            .first()
        )
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=HTTPStatus.SERVICE_UNAVAILABLE,
            detail='Nao foi possivel consultar o cadastro do paciente no MV.',
        ) from exc

    if not row:
        return resposta

    paciente = dict(row)
    telefone = _telefone_paciente(paciente)
    if not telefone:
        return resposta

    codigo = (
        settings.PATIENT_AUTH_TEST_CODE
        if settings.PATIENT_AUTH_LOCAL_TEST_MODE
        and settings.PATIENT_AUTH_TEST_CODE
        else f'{secrets.randbelow(1_000_000):06d}'
    )
    agora = _agora()
    if _usar_memoria_local():
        desafio = DesafioOtpLocal(
            desafio_id=desafio_id,
            cd_paciente=int(paciente['cd_paciente']),
            nm_paciente=str(paciente['nm_paciente']).strip(),
            cpf_hash=cpf_hash,
            telefone_final=telefone[-4:],
            codigo_hash=_hash_secreto('otp', desafio_id, codigo),
            expira_em=agora
            + timedelta(seconds=settings.PATIENT_OTP_TTL_SECONDS),
            data_criacao=agora,
        )
        with _armazenamento_local_lock:
            _desafios_locais[desafio_id] = desafio
    else:
        desafio = DesafioOtpPaciente(
            desafio_id=desafio_id,
            cd_paciente=int(paciente['cd_paciente']),
            nm_paciente=str(paciente['nm_paciente']).strip(),
            cpf_hash=cpf_hash,
            telefone_final=telefone[-4:],
            codigo_hash=_hash_secreto('otp', desafio_id, codigo),
            expira_em=agora
            + timedelta(seconds=settings.PATIENT_OTP_TTL_SECONDS),
            proximo_envio_em=agora
            + timedelta(seconds=settings.PATIENT_OTP_RESEND_SECONDS),
        )
        postgres.add(desafio)
        postgres.commit()

    resposta['telefone_final'] = telefone[-4:]
    if settings.PATIENT_AUTH_LOCAL_TEST_MODE:
        resposta['codigo_teste'] = codigo
        if not settings.PATIENT_AUTH_LOCAL_SEND_WHATSAPP:
            return resposta

    try:
        enviar_otp_whatsapp(telefone=telefone, codigo=codigo)
    except HTTPException as exc:
        desafio.utilizado_em = _agora()
        if not _usar_memoria_local():
            postgres.commit()
        erro_meta = exc.detail.get('error', {}) if isinstance(exc.detail, dict) else {}
        raise HTTPException(
            status_code=HTTPStatus.BAD_GATEWAY,
            detail={
                'mensagem': 'Não foi possível enviar o código pelo WhatsApp.',
                'meta_codigo': erro_meta.get('code'),
                'meta_subcodigo': erro_meta.get('error_subcode'),
                'meta_mensagem': erro_meta.get('message'),
            },
        ) from exc
    except Exception:
        desafio.utilizado_em = _agora()
        if not _usar_memoria_local():
            postgres.commit()
        raise HTTPException(
            status_code=HTTPStatus.BAD_GATEWAY,
            detail='Nao foi possivel enviar o codigo pelo WhatsApp.',
        )
    return resposta


@router.post(
    '/auth/validar',
    response_model=PacienteAutenticado,
    status_code=HTTPStatus.OK,
)
def validar_otp_paciente(
    payload: ValidarOtpPacienteInput,
    postgres: Session = Depends(get_session_postgres),
):
    if _usar_memoria_local():
        with _armazenamento_local_lock:
            desafio = _desafios_locais.get(payload.desafio_id)
    else:
        desafio = postgres.scalar(
            select(DesafioOtpPaciente).where(
                DesafioOtpPaciente.desafio_id == payload.desafio_id
            )
        )
    agora = _agora()
    invalido = (
        desafio is None
        or desafio.utilizado_em is not None
        or (
            desafio is not None
            and _com_timezone(desafio.expira_em) <= agora
        )
        or (
            desafio is not None
            and desafio.tentativas >= settings.PATIENT_OTP_MAX_ATTEMPTS
        )
    )
    if invalido:
        raise HTTPException(
            status_code=HTTPStatus.UNAUTHORIZED,
            detail='Codigo invalido ou expirado.',
        )

    esperado = _hash_secreto('otp', desafio.desafio_id, payload.codigo)
    if not hmac.compare_digest(desafio.codigo_hash, esperado):
        desafio.tentativas += 1
        if not _usar_memoria_local():
            postgres.commit()
        raise HTTPException(
            status_code=HTTPStatus.UNAUTHORIZED,
            detail='Codigo invalido ou expirado.',
        )

    desafio.utilizado_em = agora
    token = secrets.token_urlsafe(48)
    expira_em = agora + timedelta(seconds=settings.PATIENT_SESSION_TTL_SECONDS)
    if _usar_memoria_local():
        with _armazenamento_local_lock:
            for sessao in _sessoes_locais.values():
                if (
                    sessao.cd_paciente == desafio.cd_paciente
                    and sessao.revogado_em is None
                ):
                    sessao.revogado_em = agora
            _sessoes_locais[_hash(token)] = SessaoPacienteLocal(
                token_hash=_hash(token),
                cd_paciente=desafio.cd_paciente,
                nm_paciente=desafio.nm_paciente,
                expira_em=expira_em,
            )
    else:
        postgres.execute(
            update(SessaoPaciente)
            .where(
                SessaoPaciente.cd_paciente == desafio.cd_paciente,
                SessaoPaciente.revogado_em.is_(None),
            )
            .values(revogado_em=agora)
        )
        postgres.add(
            SessaoPaciente(
                token_hash=_hash(token),
                cd_paciente=desafio.cd_paciente,
                nm_paciente=desafio.nm_paciente,
                expira_em=expira_em,
            )
        )
        postgres.commit()
    return {
        'access_token': token,
        'token_type': 'Bearer',
        'expires_in': settings.PATIENT_SESSION_TTL_SECONDS,
        'paciente': {
            'cd_paciente': desafio.cd_paciente,
            'nm_paciente': desafio.nm_paciente,
            'telefone_final': desafio.telefone_final,
        },
    }


@router.get('/me')
def consultar_paciente_atual(
    paciente: PrincipalPaciente = Depends(validar_paciente_atual),
):
    return {
        'cd_paciente': paciente.cd_paciente,
        'nm_paciente': paciente.nome,
    }


@router.post('/auth/logout')
def encerrar_sessao_paciente(
    credenciais: HTTPAuthorizationCredentials | None = Depends(bearer_optional),
    postgres: Session = Depends(get_session_postgres),
):
    if credenciais is not None:
        token_hash = _hash(credenciais.credentials)
        if _usar_memoria_local():
            with _armazenamento_local_lock:
                sessao = _sessoes_locais.get(token_hash)
                if sessao is not None:
                    sessao.revogado_em = _agora()
        else:
            postgres.execute(
                update(SessaoPaciente)
                .where(
                    SessaoPaciente.token_hash == token_hash,
                    SessaoPaciente.revogado_em.is_(None),
                )
                .values(revogado_em=_agora())
            )
            postgres.commit()
    return {'autenticado': False}
