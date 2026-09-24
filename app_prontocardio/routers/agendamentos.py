import base64
import hashlib
import hmac
import logging
import os
import time
import unicodedata
from dataclasses import dataclass
from threading import Lock
from types import SimpleNamespace
from datetime import date, datetime, timedelta
from http import HTTPStatus
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import TextClause

from app_prontocardio.agendamento_schema import (
    AgendamentoCancelado,
    AgendamentoConfirmado,
    AgendamentoReagendado,
    AgendamentosPaciente,
    AtendimentoAgendamentoMv,
    AtualizacaoPacienteInput,
    CadastroPacienteInput,
    CancelarAgendamentoInput,
    CandidatoAtendimentoProcedimentoMv,
    ConfirmarAgendamentoInput,
    ConveniosPaciente,
    EnderecoCep,
    HorariosDisponiveis,
    HistoricoAgendamentosInterno,
    HistoricoPaciente,
    ItensAgendamentoEncontrados,
    JornadaIntegradaSugestao,
    JornadaIntegradaSugestaoInput,
    LinhasCuidadoPaciente,
    OrientacaoExame,
    PacienteAtualizado,
    PacienteCadastrado,
    PacientesEncontrados,
    PacientesLoteResultado,
    PaginaProcedimentosMv,
    PlanosDisponiveis,
    PrestadoresAgendamento,
    TiposMarcacaoConsulta,
    PreValidacaoAgendamento,
    PreValidacaoAgendamentoInput,
    ReagendarAgendamentoInput,
    LiberarReservaHorarioInput,
    ReservaHorarioInput,
    ReservaHorarioResultado,
)
from app_prontocardio.agendamento_contatos import (
    sincronizar_contatos_agendamento,
)
from app_prontocardio.agendamento_erros import (
    detalhe_seguro_atualizacao_paciente,
)
from app_prontocardio.agendamento_tipos import binds_contato_paciente
from app_prontocardio.cep_service import (
    CepErro,
    consultar_viacep,
    resolver_cidade_rows,
)
from app_prontocardio.database import get_session_oracle, get_session_postgres, postgres_engine
from app_prontocardio.models import AuditoriaAgendamento, Usuario
from app_prontocardio.rede_atendimento_service import (
    consultar_atendimento_agendamento,
    listar_candidatos_procedimento,
)
from app_prontocardio.routers.paciente_auth import (
    PrincipalPaciente,
    resolver_sessao_paciente,
)
from app_prontocardio.security import valida_token_usuario_atual
from app_prontocardio.services.agendamento_historico import (
    RepositorioHistoricoAgendamento,
)
from app_prontocardio.services.agendamento_origens import (
    AgendamentoOrigemService,
    EvidenciaOrigem,
    OrigemAgendamento,
    RegistroOrigemInput,
    RegistroOrigemResultado,
    origem_do_principal,
)
from app_prontocardio.settings import Settings
from app_prontocardio.whatsapp_service import enviar_template_whatsapp

router = APIRouter(prefix='/agendamentos', tags=['agendamentos'])
logger = logging.getLogger(__name__)
settings = Settings()


_RESERVA_HORARIO_TTL_SEGUNDOS = 300
_RESERVA_HORARIO_LIMITE_POR_SESSAO = 12


@dataclass
class _ReservaHorarioAtiva:
    cd_paciente: int
    token_hash: str
    expira_em: datetime


_reservas_horario: dict[int, _ReservaHorarioAtiva] = {}
_reservas_horario_lock = Lock()


def _registrar_evento_pos_mv(  # noqa: PLR0913
    *,
    engine,
    usuario_atual,
    status: str,
    cd_paciente: int,
    cd_item_agendamento: int,
    cd_it_agenda_central: int,
    cd_agenda_central: int,
    cd_tip_mar: int | None,
    protocolo_mv: int | None,
    cd_it_agenda_central_anterior: int | None = None,
    protocolo_mv_anterior: int | None = None,
) -> bool:
    """Persiste auditoria depois do commit MV, sem desfazer o atendimento."""
    if engine is None:
        return False
    try:
        with Session(engine) as postgres:
            origem_resultado = RegistroOrigemResultado(
                status='registro_desabilitado',
                origem=origem_do_principal(usuario_atual),
            )
            if (
                settings.AGENDAMENTO_REGISTRAR_ORIGEM
                and protocolo_mv is not None
                and protocolo_mv > 0
            ):
                service = AgendamentoOrigemService(postgres)
                instancia_anterior = (
                    (cd_it_agenda_central_anterior, protocolo_mv_anterior)
                    if cd_it_agenda_central_anterior
                    and protocolo_mv_anterior
                    else None
                )
                anterior = (
                    service.buscar_por_instancias([instancia_anterior]).get(
                        instancia_anterior
                    )
                    if instancia_anterior is not None
                    else None
                )
                if status == 'reagendado' and anterior is not None:
                    origem_resultado = service.herdar(
                        slot_anterior=cd_it_agenda_central_anterior,
                        protocolo_anterior=protocolo_mv_anterior,
                        slot_novo=cd_it_agenda_central,
                        cd_paciente=cd_paciente,
                        cd_agenda_central=cd_agenda_central,
                        protocolo_mv=protocolo_mv,
                        registrada_por_id=getattr(usuario_atual, 'id', None),
                        registrada_por_nome=getattr(
                            usuario_atual, 'nome', 'ACESSO_INTERNO'
                        ),
                    )
                elif status in {'agendado', 'reagendado'}:
                    origem_resultado = service.registrar(
                        RegistroOrigemInput(
                            cd_it_agenda_central=cd_it_agenda_central,
                            cd_paciente=cd_paciente,
                            cd_agenda_central=cd_agenda_central,
                            protocolo_mv=protocolo_mv,
                            origem=origem_do_principal(usuario_atual),
                            evidencia=EvidenciaOrigem.GRAVACAO_DIRETA,
                            referencia_externa=None,
                            registrada_por_id=getattr(
                                usuario_atual, 'id', None
                            ),
                            registrada_por_nome=getattr(
                                usuario_atual, 'nome', 'ACESSO_INTERNO'
                            ),
                        ),
                        finalizar_transacao=False,
                    )
            postgres.add(
                AuditoriaAgendamento(
                    operador_id=getattr(usuario_atual, 'id', None),
                    operador_nome=getattr(
                        usuario_atual, 'nome', 'ACESSO_INTERNO'
                    ),
                    origem=origem_resultado.origem.value,
                    cd_paciente=cd_paciente,
                    cd_item_agendamento=cd_item_agendamento,
                    cd_it_agenda_central=cd_it_agenda_central,
                    cd_agenda_central=cd_agenda_central,
                    cd_tip_mar=cd_tip_mar,
                    protocolo_mv=protocolo_mv,
                    status=status,
                    chave_efeito_lote=None,
                )
            )
            postgres.commit()
        return True
    except (SQLAlchemyError, ValueError):
        logger.warning('agendamento_auditoria_falhou')
        return False


def _hash_reserva_token(token: str | None) -> str:
    return hashlib.sha256((token or '').encode('utf-8')).hexdigest()


def _limpar_reservas_expiradas(agora: datetime) -> None:
    expiradas = [
        slot
        for slot, reserva in _reservas_horario.items()
        if reserva.expira_em <= agora
    ]
    for slot in expiradas:
        _reservas_horario.pop(slot, None)


def _garantir_reserva_disponivel(
    cd_it_agenda_central: int,
    reserva_token: str | None,
) -> None:
    agora = datetime.now()
    token_hash = _hash_reserva_token(reserva_token)
    with _reservas_horario_lock:
        _limpar_reservas_expiradas(agora)
        reserva = _reservas_horario.get(cd_it_agenda_central)
        if reserva is not None and not hmac.compare_digest(
            reserva.token_hash, token_hash
        ):
            raise HTTPException(
                status_code=HTTPStatus.CONFLICT,
                detail=(
                    'Este horario esta temporariamente reservado por outro '
                    'atendente. Atualize a disponibilidade.'
                ),
            )


def _liberar_reserva_horario(
    cd_it_agenda_central: int,
    reserva_token: str | None,
) -> bool:
    if not reserva_token:
        return False
    token_hash = _hash_reserva_token(reserva_token)
    with _reservas_horario_lock:
        reserva = _reservas_horario.get(cd_it_agenda_central)
        if reserva is None or not hmac.compare_digest(
            reserva.token_hash, token_hash
        ):
            return False
        _reservas_horario.pop(cd_it_agenda_central, None)
        return True


def _filtrar_reservas_horarios(
    horarios: list[dict],
    reserva_token: str | None,
) -> list[dict]:
    agora = datetime.now()
    token_hash = _hash_reserva_token(reserva_token)
    with _reservas_horario_lock:
        _limpar_reservas_expiradas(agora)
        bloqueados = {
            slot
            for slot, reserva in _reservas_horario.items()
            if not hmac.compare_digest(reserva.token_hash, token_hash)
        }
    return [
        horario
        for horario in horarios
        if horario.get('cd_it_agenda_central') not in bloqueados
    ]


def _observacao_agendamento(texto: str | None) -> str | None:
    if not texto:
        return None
    normalizada = ' '.join(texto.strip().split())
    return normalizada[:600] if normalizada else None


def _valor_marcador_observacao(
    observacao: str | None, marcador: str
) -> str | None:
    prefixo = f'{marcador.upper()}:'
    for parte in (observacao or '').split('|'):
        texto = parte.strip()
        if texto.upper().startswith(prefixo):
            return texto[len(prefixo) :].strip()
    return None


def _observacao_livre_sem_identificacao(observacao: str | None) -> str | None:
    partes = []
    for parte in (observacao or '').split('|'):
        texto = parte.strip()
        superior = texto.upper()
        if not texto or superior == 'AGENDAMENTO WEB MEU PRONTOCARDIO':
            continue
        if superior.startswith(('USUARIO_MV:', 'OPERADOR:')):
            continue
        if superior.startswith('OBS:'):
            texto = texto[4:].strip()
        if texto:
            partes.append(texto)
    return ' | '.join(partes) or None


def _usuario_mv_valido(usuario: str) -> bool:
    permitidos = {'.', '-', '_'}
    return bool(usuario) and all(
        caractere.isalnum() or caractere in permitidos
        for caractere in usuario
    )


MINIMO_LETRAS_NOME_OPERADOR = 2


def _nome_operador_valido(nome: str) -> bool:
    permitidos = {' ', '.', "'", '’', '-'}
    letras = sum(caractere.isalpha() for caractere in nome)
    return letras >= MINIMO_LETRAS_NOME_OPERADOR and all(
        caractere.isalpha() or caractere in permitidos for caractere in nome
    )


def _identificacao_agendamento(
    payload,
    usuario_atual: Usuario | PrincipalPaciente,
) -> tuple[str, str, str]:
    observacao_original = getattr(payload, 'observacao', None)
    if isinstance(usuario_atual, PrincipalPaciente):
        usuario = 'MEU_PRONTOCARDIO'
        nome = ' '.join(str(usuario_atual.nome or '').strip().split())
        if not _nome_operador_valido(nome):
            nome = 'PACIENTE'
    else:
        usuario_informado = getattr(payload, 'usuario_mv', None)
        nome_informado = getattr(payload, 'nome_operador', None)
        usuario_bruto = (
            usuario_informado
            if usuario_informado is not None
            else _valor_marcador_observacao(observacao_original, 'USUARIO_MV')
        )
        nome_bruto = (
            nome_informado
            if nome_informado is not None
            else _valor_marcador_observacao(observacao_original, 'OPERADOR')
        )
        usuario = str(usuario_bruto or '').strip().upper()
        nome = ' '.join(str(nome_bruto or '').strip().split())
        if not _usuario_mv_valido(usuario) or not _nome_operador_valido(nome):
            raise HTTPException(
                status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
                detail=(
                    'Identificação obrigatória: informe um usuário do MV válido '
                    'e o nome de quem está realizando o agendamento, sem números.'
                ),
            )
    prefixo = (
        'AGENDAMENTO WEB MEU PRONTOCARDIO | '
        f'USUARIO_MV: {usuario} | OPERADOR: {nome}'
    )
    observacao_livre = _observacao_livre_sem_identificacao(observacao_original)
    observacao = prefixo
    if observacao_livre:
        espaco_disponivel = max(0, 600 - len(prefixo) - len(' | OBS: '))
        observacao = f'{prefixo} | OBS: {observacao_livre[:espaco_disponivel]}'
    return usuario, nome, observacao


def _anexa_observacao(texto_atual: str | None, observacao: str) -> str:
    atual = (texto_atual or '').strip()
    if atual and observacao not in atual:
        return f'{atual} | {observacao}'[:600]
    return (atual or observacao)[:600]


def _nome_unidade_exibicao(nome: str | None) -> str:
    unidade = (nome or '').strip()
    unidade_normalizada = unidade.upper()
    if unidade_normalizada == 'CLINICA PRONTOCARDIO SAUDE':
        return 'CLINICA DIAGNOSTICA 01'
    if unidade_normalizada == 'UNIDADE DIAGNOSTICO 2':
        return 'CLINICA DIAGNOSTICA 02'
    return unidade or 'ProntoCardio'


def _normaliza_horarios_exibicao(horarios: list[dict]) -> list[dict]:
    for horario in horarios:
        if not horario.get('data_hora') and horario.get('horario') is not None:
            horario['data_hora'] = horario.get('horario')
        if not horario.get('horario') and horario.get('data_hora') is not None:
            horario['horario'] = horario.get('data_hora')
        horario['ds_unidade_atendimento'] = _nome_unidade_exibicao(
            horario.get('ds_unidade_atendimento')
        )
    return horarios


def _telefone_paciente_whatsapp(
    ddd_payload: str | None,
    celular_payload: str | None,
    ddd_banco: str | None,
    celular_banco: str | None,
) -> str | None:
    ddd = ''.join(ch for ch in str(ddd_payload or ddd_banco or '') if ch.isdigit())
    celular = ''.join(
        ch for ch in str(celular_payload or celular_banco or '') if ch.isdigit()
    )
    if not ddd or not celular:
        return None
    return f'55{ddd}{celular}'

_oauth2_optional = OAuth2PasswordBearer(
    tokenUrl='/autenticacao/token', auto_error=False
)
_AGENDAMENTO_COOKIE = 'agendamento_sessao'
_AGENDAMENTO_TTL = 8 * 60 * 60


def _sessao_assinada(expira: int) -> str:
    corpo = str(expira)
    assinatura = hmac.new(
        settings.SECRET_KEY.encode(), corpo.encode(), hashlib.sha256
    ).hexdigest()
    return base64.urlsafe_b64encode(f'{corpo}.{assinatura}'.encode()).decode()


def _sessao_valida(valor: str | None) -> bool:
    if not valor:
        return False
    try:
        corpo, assinatura = base64.urlsafe_b64decode(valor.encode()).decode().split('.', 1)
        esperado = hmac.new(
            settings.SECRET_KEY.encode(), corpo.encode(), hashlib.sha256
        ).hexdigest()
        return int(corpo) >= int(time.time()) and hmac.compare_digest(assinatura, esperado)
    except (ValueError, TypeError, UnicodeDecodeError):
        return False


def valida_acesso_agendamento(
    request: Request,
    session=Depends(get_session_postgres),
    token: str | None = Depends(_oauth2_optional),
):
    if token:
        paciente = resolver_sessao_paciente(session, token)
        if paciente is not None:
            return paciente
        return valida_token_usuario_atual(session, token)
    if _sessao_valida(request.cookies.get(_AGENDAMENTO_COOKIE)):
        return SimpleNamespace(
            id=None,
            nome='ACESSO_INTERNO',
            tipo='interno',
            origem_agendamento='MEU_PRONTOCARDIO',
        )
    raise HTTPException(
        status_code=401,
        detail='Acesso interno necessário. Informe a senha da clínica.',
        headers={'WWW-Authenticate': 'Bearer'},
    )


ValidaUsuarioAtual = Annotated[
    Usuario | PrincipalPaciente,
    Depends(valida_acesso_agendamento),
]
UsuarioConciliacao = Annotated[Usuario, Depends(valida_token_usuario_atual)]
TAMANHO_CPF = 11
PERMISSAO_PACIENTES_LOTE = 'pacientes_lote'
LIMITE_FAIXA_CODIGOS_PACIENTES = 5_000
LIMITE_DIAS_PACIENTES_LOTE = 31


@router.get(
    '/conciliacao/agenda/{cd_it_agenda_central}',
    response_model=AtendimentoAgendamentoMv,
)
def obter_atendimento_agendamento(
    cd_it_agenda_central: int,
    _: UsuarioConciliacao,
    session: Session = Depends(get_session_oracle),
):
    resultado = consultar_atendimento_agendamento(
        session,
        cd_it_agenda_central,
    )
    if resultado is None:
        raise HTTPException(
            status_code=HTTPStatus.NOT_FOUND,
            detail='Atendimento ainda não identificado no MV.',
        )
    return resultado


@router.get(
    '/conciliacao/procedimentos/candidatos',
    response_model=list[CandidatoAtendimentoProcedimentoMv],
)
def obter_candidatos_procedimento(
    _: UsuarioConciliacao,
    cd_paciente: Annotated[int, Query(gt=0)],
    codigo_procedimento: Annotated[str, Query(min_length=1, max_length=30)],
    data_inicio: date,
    data_fim: date,
    session: Session = Depends(get_session_oracle),
):
    try:
        return listar_candidatos_procedimento(
            session,
            cd_paciente,
            codigo_procedimento.strip(),
            data_inicio,
            data_fim,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc


def garantir_paciente_autorizado(
    usuario_atual: Usuario | PrincipalPaciente,
    cd_paciente: int,
) -> None:
    if (
        isinstance(usuario_atual, PrincipalPaciente)
        and usuario_atual.cd_paciente != cd_paciente
    ):
        raise HTTPException(
            status_code=HTTPStatus.FORBIDDEN,
            detail=(
                'A sessão autenticada não permite acessar dados de outro paciente.'
            ),
        )


def bloquear_operacao_interna_para_paciente(
    usuario_atual: Usuario | PrincipalPaciente,
) -> None:
    if isinstance(usuario_atual, PrincipalPaciente):
        raise HTTPException(
            status_code=HTTPStatus.FORBIDDEN,
            detail='Operação disponível apenas para o atendimento interno.',
        )


def exigir_usuario_historico_agendamento(
    usuario_atual: ValidaUsuarioAtual,
) -> Usuario | SimpleNamespace:
    """O histórico contém observações e exige operador individual autorizado."""
    if isinstance(usuario_atual, PrincipalPaciente):
        raise HTTPException(
            status_code=HTTPStatus.FORBIDDEN,
            detail='Operação disponível apenas para operador interno autorizado.',
        )
    if getattr(usuario_atual, 'tipo', None) == 'interno':
        return usuario_atual
    if not isinstance(usuario_atual, Usuario):
        raise HTTPException(
            status_code=HTTPStatus.FORBIDDEN,
            detail='Operação disponível apenas para operador interno autorizado.',
        )
    telas = {
        str(tela).casefold()
        for tela in (usuario_atual.telas_permitidas or [])
    }
    if (
        not usuario_atual.ativo
        or str(usuario_atual.perfil or '').casefold()
        not in settings.agendamento_perfis_permitidos
        or not telas & settings.agendamento_telas_permitidas
    ):
        raise HTTPException(
            status_code=HTTPStatus.FORBIDDEN,
            detail='Operação disponível apenas para operador interno autorizado.',
        )
    return usuario_atual


def exigir_permissao_pacientes_lote(
    usuario_atual: Usuario | PrincipalPaciente,
) -> None:
    bloquear_operacao_interna_para_paciente(usuario_atual)
    permissoes = getattr(usuario_atual, 'telas_permitidas', None) or []
    if PERMISSAO_PACIENTES_LOTE not in permissoes:
        raise HTTPException(
            status_code=HTTPStatus.FORBIDDEN,
            detail='Usuario sem permissao para consultar pacientes em lote.',
        )


def _normaliza_busca(texto: str) -> str:
    decomposed = unicodedata.normalize('NFKD', texto or '')
    sem_acentos = ''.join(
        caractere for caractere in decomposed if not unicodedata.combining(caractere)
    )
    return sem_acentos.upper().strip()


@router.post(
    '/reservas',
    status_code=HTTPStatus.OK,
    response_model=ReservaHorarioResultado,
)
def reservar_horario(
    payload: ReservaHorarioInput,
    usuario_atual: ValidaUsuarioAtual,
    session: Session = Depends(get_session_oracle),
):
    garantir_paciente_autorizado(usuario_atual, payload.cd_paciente)
    slot = session.execute(
        text(
            '''
            SELECT CD_PACIENTE, NVL(SN_BLOQUEADO, 'N')
              FROM DBAMV.IT_AGENDA_CENTRAL
             WHERE CD_IT_AGENDA_CENTRAL = :slot
               AND HR_AGENDA > SYSDATE
            '''
        ),
        {'slot': payload.cd_it_agenda_central},
    ).first()
    if slot is None or slot[0] is not None or slot[1] == 'S':
        raise HTTPException(
            status_code=HTTPStatus.CONFLICT,
            detail=(
                'Este horario nao esta mais livre no MV. '
                'Atualize a disponibilidade.'
            ),
        )

    agora = datetime.now()
    expira_em = agora + timedelta(seconds=_RESERVA_HORARIO_TTL_SEGUNDOS)
    token_hash = _hash_reserva_token(payload.reserva_token)
    with _reservas_horario_lock:
        _limpar_reservas_expiradas(agora)
        atual = _reservas_horario.get(payload.cd_it_agenda_central)
        if atual is not None and not hmac.compare_digest(
            atual.token_hash, token_hash
        ):
            raise HTTPException(
                status_code=HTTPStatus.CONFLICT,
                detail=(
                    'Este horario acabou de ser reservado por outro '
                    'atendente. Escolha outro horario.'
                ),
            )
        reservas_sessao = sum(
            1
            for reserva in _reservas_horario.values()
            if hmac.compare_digest(reserva.token_hash, token_hash)
        )
        if atual is None and reservas_sessao >= _RESERVA_HORARIO_LIMITE_POR_SESSAO:
            raise HTTPException(
                status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
                detail='Limite de horarios temporariamente reservados atingido.',
            )
        _reservas_horario[payload.cd_it_agenda_central] = _ReservaHorarioAtiva(
            cd_paciente=payload.cd_paciente,
            token_hash=token_hash,
            expira_em=expira_em,
        )
    return {
        'cd_it_agenda_central': payload.cd_it_agenda_central,
        'reservado': True,
        'expira_em': expira_em,
        'expira_em_segundos': _RESERVA_HORARIO_TTL_SEGUNDOS,
    }


@router.post('/reservas/liberar-todas', status_code=HTTPStatus.OK)
def liberar_todas_reservas(
    payload: LiberarReservaHorarioInput,
    _: ValidaUsuarioAtual,
):
    token_hash = _hash_reserva_token(payload.reserva_token)
    with _reservas_horario_lock:
        slots = [
            slot
            for slot, reserva in _reservas_horario.items()
            if hmac.compare_digest(reserva.token_hash, token_hash)
        ]
        for slot in slots:
            _reservas_horario.pop(slot, None)
    return {'liberadas': len(slots)}


@router.post('/reservas/{cd_it_agenda_central}/liberar', status_code=HTTPStatus.OK)
def liberar_reserva(
    cd_it_agenda_central: int,
    payload: LiberarReservaHorarioInput,
    _: ValidaUsuarioAtual,
):
    liberada = _liberar_reserva_horario(
        cd_it_agenda_central, payload.reserva_token
    )
    return {'liberada': liberada}


def _somente_digitos(valor: str | None) -> str | None:
    digitos = ''.join(ch for ch in str(valor or '') if ch.isdigit())
    return digitos or None


def _cpf_valido(cpf: str) -> bool:
    if len(cpf) != TAMANHO_CPF or cpf == cpf[0] * TAMANHO_CPF:
        return False
    numeros = [int(ch) for ch in cpf]
    soma = sum(numeros[i] * (10 - i) for i in range(9))
    digito_1 = (soma * 10) % 11
    if digito_1 == 10:
        digito_1 = 0
    soma = sum(numeros[i] * (11 - i) for i in range(10))
    digito_2 = (soma * 10) % 11
    if digito_2 == 10:
        digito_2 = 0
    return numeros[9] == digito_1 and numeros[10] == digito_2


def _texto_maiusculo(valor: str | None, limite: int | None = None) -> str | None:
    texto = ' '.join(str(valor or '').strip().split()).upper()
    if not texto:
        return None
    return texto[:limite] if limite else texto


def _erro_oracle_resumido(exc: Exception) -> str:
    texto = ' '.join(str(exc).split())
    for marcador in ('ORA-', 'DPY-', 'DPI-'):
        posicao = texto.find(marcador)
        if posicao >= 0:
            return texto[posicao:posicao + 260]
    return texto[:260] or 'Erro nao identificado no MV.'


@router.post('/acesso/login')
def login_acesso_interno(payload: dict, response: Response):
    senha_configurada = settings.AGENDAMENTO_SENHA_INTERNA
    senha_informada = str(payload.get('senha') or '')
    if not senha_configurada or not hmac.compare_digest(senha_informada, senha_configurada):
        raise HTTPException(status_code=401, detail='Senha interna inválida.')
    response.set_cookie(
        _AGENDAMENTO_COOKIE,
        _sessao_assinada(int(time.time()) + _AGENDAMENTO_TTL),
        max_age=_AGENDAMENTO_TTL,
        httponly=True,
        samesite=(
            'none'
            if settings.COOKIE_SECURE
            else 'lax'
        ),
        secure=settings.COOKIE_SECURE,
    )
    return {'autenticado': True, 'expira_em_segundos': _AGENDAMENTO_TTL}


@router.post('/acesso/logout')
def logout_acesso_interno(response: Response):
    response.delete_cookie(_AGENDAMENTO_COOKIE)
    return {'autenticado': False}

CONSULTA_PLANOS_ATIVOS = text(
    '''
    SELECT c.CD_CONVENIO AS cd_convenio,
           c.NM_CONVENIO AS nm_convenio,
           cp.CD_CON_PLA AS cd_con_pla,
           cp.DS_CON_PLA AS ds_con_pla
      FROM DBAMV.CONVENIO c
      JOIN DBAMV.CON_PLA cp ON cp.CD_CONVENIO = c.CD_CONVENIO
     WHERE NVL(c.SN_ATIVO, 'S') = 'S'
       AND NVL(cp.SN_ATIVO, 'S') = 'S'
       AND UPPER(TRIM(c.NM_CONVENIO)) NOT LIKE 'SUS%'
     ORDER BY c.NM_CONVENIO, cp.DS_CON_PLA
    '''
)

CONSULTA_TIPOS_MARCACAO_CONSULTA = text(
    """
    SELECT tm.CD_TIP_MAR AS cd_tip_mar,
           tm.DS_TIP_MAR AS ds_tip_mar
      FROM DBAMV.TIP_MAR tm
     WHERE tm.CD_TIP_MAR IN (1, 2, 15)
     ORDER BY CASE tm.CD_TIP_MAR
                WHEN 1 THEN 1
                WHEN 2 THEN 2
                WHEN 15 THEN 3
                WHEN 17 THEN 4
                ELSE 99
              END,
              tm.DS_TIP_MAR
    """
)

CONSULTA_DADOS_CONFIRMACAO_WHATSAPP = text(
    """
    SELECT pac.NM_PACIENTE AS nm_paciente,
           TO_CHAR(pac.NR_DDD_CELULAR) AS nr_ddd_celular,
           TO_CHAR(pac.NR_CELULAR) AS nr_celular,
           ia.DS_ITEM_AGENDAMENTO AS ds_item_agendamento,
           iac.HR_AGENDA AS horario,
           ua.DS_UNIDADE_ATENDIMENTO AS ds_unidade_atendimento
      FROM DBAMV.IT_AGENDA_CENTRAL iac
      JOIN DBAMV.PACIENTE pac
        ON pac.CD_PACIENTE = :cd_paciente
      JOIN DBAMV.ITEM_AGENDAMENTO ia
        ON ia.CD_ITEM_AGENDAMENTO = :cd_item_agendamento
      JOIN DBAMV.AGENDA_CENTRAL ac
        ON ac.CD_AGENDA_CENTRAL = iac.CD_AGENDA_CENTRAL
      LEFT JOIN DBAMV.UNIDADE_ATENDIMENTO ua
        ON ua.CD_UNIDADE_ATENDIMENTO = ac.CD_UNIDADE_ATENDIMENTO
     WHERE iac.CD_IT_AGENDA_CENTRAL = :cd_it_agenda_central
    """
)

CONSULTA_DADOS_CANCELAMENTO_WHATSAPP = text(
    """
    SELECT COALESCE(pac.CD_PACIENTE, mov.CD_PACIENTE) AS cd_paciente,
           COALESCE(pac.NM_PACIENTE, mov.NM_PACIENTE) AS nm_paciente,
           TO_CHAR(pac.NR_DDD_CELULAR) AS nr_ddd_celular,
           TO_CHAR(pac.NR_CELULAR) AS nr_celular,
           ia.DS_ITEM_AGENDAMENTO AS ds_item_agendamento,
           COALESCE(iac.CD_ITEM_AGENDAMENTO, im.CD_ITEM_AGENDAMENTO)
               AS cd_item_agendamento,
           COALESCE(iac.CD_AGENDA_CENTRAL, im.CD_AGENDA_CENTRAL)
               AS cd_agenda_central,
           COALESCE(iac.HR_AGENDA, im.HR_AGENDA) AS horario,
           ua.DS_UNIDADE_ATENDIMENTO AS ds_unidade_atendimento,
           im.CD_MOVIMENTO_AGENDA_CENTRAL AS protocolo
      FROM DBAMV.IT_AGENDA_CENTRAL iac
      LEFT JOIN DBAMV.IT_MOVIMENTO_AGENDA_CENTRAL im
        ON im.CD_IT_AGENDA_CENTRAL = iac.CD_IT_AGENDA_CENTRAL
      LEFT JOIN DBAMV.MOVIMENTO_AGENDA_CENTRAL mov
        ON mov.CD_MOVIMENTO_AGENDA_CENTRAL = im.CD_MOVIMENTO_AGENDA_CENTRAL
      LEFT JOIN DBAMV.PACIENTE pac
        ON pac.CD_PACIENTE = COALESCE(iac.CD_PACIENTE, mov.CD_PACIENTE)
      LEFT JOIN DBAMV.ITEM_AGENDAMENTO ia
        ON ia.CD_ITEM_AGENDAMENTO = COALESCE(iac.CD_ITEM_AGENDAMENTO, im.CD_ITEM_AGENDAMENTO)
      LEFT JOIN DBAMV.AGENDA_CENTRAL ac
        ON ac.CD_AGENDA_CENTRAL = COALESCE(iac.CD_AGENDA_CENTRAL, im.CD_AGENDA_CENTRAL)
      LEFT JOIN DBAMV.UNIDADE_ATENDIMENTO ua
        ON ua.CD_UNIDADE_ATENDIMENTO = COALESCE(ac.CD_UNIDADE_ATENDIMENTO, im.CD_UNIDADE_ATENDIMENTO)
     WHERE iac.CD_IT_AGENDA_CENTRAL = :cd_it_agenda_central
     ORDER BY im.CD_IT_MOVIMENTO_AGENDA_CENTRAL DESC
    """
)


def _enviar_confirmacao_whatsapp_agendamento(
    *,
    session: Session,
    payload: ConfirmarAgendamentoInput,
    protocolo: int,
) -> tuple[str | None, str | None]:
    if os.getenv('WHATSAPP_CONFIRMAR_AGENDAMENTO_AUTO', 'true').lower() != 'true':
        return 'desabilitado', 'Envio automatico de WhatsApp desabilitado.'

    try:
        dados = (
            session.execute(
                CONSULTA_DADOS_CONFIRMACAO_WHATSAPP,
                {
                    'cd_paciente': payload.cd_paciente,
                    'cd_item_agendamento': payload.cd_item_agendamento,
                    'cd_it_agenda_central': payload.cd_it_agenda_central,
                },
            )
            .mappings()
            .first()
        )
    except Exception:
        return (
            'falhou',
            'Agendamento confirmado no MV, mas nao foi possivel consultar os '
            'dados para envio do WhatsApp.',
        )
    if not dados:
        return 'nao_enviado', 'Nao foi possivel montar os dados do WhatsApp.'

    telefone = _telefone_paciente_whatsapp(
        payload.nr_ddd_celular,
        payload.nr_celular,
        dados.get('nr_ddd_celular'),
        dados.get('nr_celular'),
    )
    if not telefone:
        return 'nao_enviado', 'Paciente sem celular para envio de WhatsApp.'

    horario = dados.get('horario')
    if isinstance(horario, datetime):
        data_texto = horario.strftime('%d/%m/%Y')
        hora_texto = horario.strftime('%H:%M')
    else:
        data_texto = ''
        hora_texto = ''

    try:
        enviar_template_whatsapp(
            telefone=telefone,
            nome_template=os.getenv(
                'WHATSAPP_TEMPLATE_CONFIRMACAO_AGENDAMENTO',
                'confirmacao_agendamento',
            ),
            idioma=os.getenv('WHATSAPP_TEMPLATE_CONFIRMACAO_IDIOMA', 'pt_BR'),
            parametros=[
                str(dados.get('nm_paciente') or 'Paciente'),
                str(dados.get('ds_item_agendamento') or 'Agendamento'),
                data_texto,
                hora_texto,
                _nome_unidade_exibicao(dados.get('ds_unidade_atendimento')),
                str(protocolo),
            ],
        )
    except HTTPException as exc:
        return 'falhou', f'WhatsApp nao enviado: {exc.detail}'
    except Exception as exc:
        return 'falhou', f'WhatsApp nao enviado: {exc}'

    return 'enviado', 'Confirmacao enviada por WhatsApp.'


def _enviar_cancelamento_whatsapp_agendamento(
    dados: dict | None,
) -> tuple[str | None, str | None]:
    if os.getenv('WHATSAPP_CANCELAR_AGENDAMENTO_AUTO', 'true').lower() != 'true':
        return 'desabilitado', 'Envio automatico de WhatsApp desabilitado.'
    if not dados:
        return 'nao_enviado', 'Nao foi possivel montar os dados do WhatsApp.'

    telefone = _telefone_paciente_whatsapp(
        None,
        None,
        dados.get('nr_ddd_celular'),
        dados.get('nr_celular'),
    )
    if not telefone:
        return 'nao_enviado', 'Paciente sem celular para envio de WhatsApp.'

    horario = dados.get('horario')
    if isinstance(horario, datetime):
        data_texto = horario.strftime('%d/%m/%Y')
        hora_texto = horario.strftime('%H:%M')
    else:
        data_texto = ''
        hora_texto = ''

    try:
        enviar_template_whatsapp(
            telefone=telefone,
            nome_template=os.getenv(
                'WHATSAPP_TEMPLATE_CANCELAMENTO_AGENDAMENTO',
                'cancelamento_agendamento_ptbr',
            ),
            idioma=os.getenv('WHATSAPP_TEMPLATE_CANCELAMENTO_IDIOMA', 'pt_BR'),
            parametros=[
                str(dados.get('nm_paciente') or 'Paciente'),
                str(dados.get('ds_item_agendamento') or 'Agendamento'),
                data_texto,
                hora_texto,
                str(dados.get('protocolo') or 'Nao informado'),
            ],
        )
    except HTTPException as exc:
        return 'falhou', f'WhatsApp nao enviado: {exc.detail}'
    except Exception as exc:
        return 'falhou', f'WhatsApp nao enviado: {exc}'

    return 'enviado', 'Cancelamento enviado por WhatsApp.'


@router.post(
    '/confirmar',
    status_code=HTTPStatus.OK,
    response_model=AgendamentoConfirmado,
)
def confirmar_agendamento(
    payload: ConfirmarAgendamentoInput,
    usuario_atual: ValidaUsuarioAtual,
    session: Session = Depends(get_session_oracle),
):
    """Conclui um agendamento usando a procedure oficial do SoulMV."""
    garantir_paciente_autorizado(usuario_atual, payload.cd_paciente)
    _, _, observacao_identificada = _identificacao_agendamento(
        payload,
        usuario_atual,
    )
    _garantir_reserva_disponivel(
        payload.cd_it_agenda_central, payload.reserva_token
    )
    if os.getenv('MV_GRAVACAO_HABILITADA', 'false').lower() != 'true':
        raise HTTPException(
            status_code=HTTPStatus.NOT_IMPLEMENTED,
            detail='Gravacao do MV desabilitada neste ambiente.',
        )

    if payload.cd_tip_mar is not None:
        tipo_compativel = session.scalar(
            CONSULTA_SLOT_TIPO_COMPATIVEL,
            {
                'cd_it_agenda_central': payload.cd_it_agenda_central,
                'cd_tip_mar': payload.cd_tip_mar,
            },
        )
        if not tipo_compativel:
            raise HTTPException(
                status_code=HTTPStatus.CONFLICT,
                detail=(
                    'O horario nao aceita o tipo de consulta selecionado. '
                    'Atualize a disponibilidade.'
                ),
            )

    ocupado = session.execute(
        text(
            '''
            SELECT COUNT(*)
              FROM DBAMV.IT_AGENDA_CENTRAL
             WHERE CD_IT_AGENDA_CENTRAL = :slot
               AND CD_PACIENTE IS NOT NULL
            '''
        ),
        {'slot': payload.cd_it_agenda_central},
    ).scalar_one()
    if ocupado:
        raise HTTPException(
            status_code=HTTPStatus.CONFLICT,
            detail='Este horário já está ocupado no MV. Atualize a disponibilidade e selecione outro horário.',
        )

    slot_fim = payload.cd_it_agenda_fim or payload.cd_it_agenda_central
    connection = session.connection().connection
    cursor = connection.cursor()
    retorno = cursor.var(int)
    try:
        cursor.callproc(
            'DBAMV.PKG_AGENDAMENTO_WEB.PRC_CONCLUI_AGENDAMENTO_WEB',
            [
                payload.cd_paciente,
                payload.cd_item_agendamento,
                'A',
                payload.cd_agenda_central,
                payload.cd_it_agenda_central,
                slot_fim,
                None,
                payload.cd_tip_mar,
                retorno,
            ],
        )
        codigo = retorno.getvalue()
        if codigo != 1:
            detalhe = (
                'Este horario acabou de ser ocupado por outro agendamento. '
                'Atualize os horarios e selecione outra opcao.'
                if codigo == 4
                else f'O MV recusou o agendamento (retorno {codigo}).'
            )
            raise HTTPException(
                status_code=HTTPStatus.CONFLICT,
                detail=detalhe,
            )
        sincronizar_contatos_agendamento(
            cursor,
            cd_paciente=payload.cd_paciente,
            cd_it_agenda_inicio=payload.cd_it_agenda_central,
            cd_it_agenda_fim=slot_fim,
        )
        row = cursor.execute(
            """
            SELECT i.CD_IT_AGENDA_CENTRAL AS horario_id,
                   i.CD_AGENDA_CENTRAL AS agenda_id,
                   im.CD_MOVIMENTO_AGENDA_CENTRAL AS movimento_id,
                   im.CD_IT_MOVIMENTO_AGENDA_CENTRAL AS item_movimento_id,
                   l.CD_LOG_OPERA_AGENDA AS log_id
              FROM DBAMV.IT_AGENDA_CENTRAL i
              LEFT JOIN DBAMV.IT_MOVIMENTO_AGENDA_CENTRAL im
                ON im.CD_IT_AGENDA_CENTRAL = i.CD_IT_AGENDA_CENTRAL
               AND im.TP_STATUS NOT IN ('E', 'C', 'P', 'T')
              LEFT JOIN DBAMV.LOG_OPERA_AGENDA_CENTRAL l
                ON l.CD_IT_AGENDA_CENTRAL = i.CD_IT_AGENDA_CENTRAL
               AND l.TP_OPERACAO = 'A'
             WHERE i.CD_IT_AGENDA_CENTRAL = :slot
             ORDER BY l.DT_OPERA_AGENDA DESC
            """,
            {'slot': payload.cd_it_agenda_central},
        ).fetchone()
        observacao = _observacao_agendamento(observacao_identificada)
        if observacao:
            atual = cursor.execute(
                """
                SELECT DS_OBSERVACAO,
                       DS_OBSERVACAO_GERAL
                  FROM DBAMV.IT_AGENDA_CENTRAL
                 WHERE CD_IT_AGENDA_CENTRAL = :slot
                """,
                {'slot': payload.cd_it_agenda_central},
            ).fetchone()
            texto_novo = _anexa_observacao(atual[0] if atual else None, observacao)
            texto_geral_novo = _anexa_observacao(
                atual[1] if atual else None, observacao
            )
            cursor.execute(
                """
                UPDATE DBAMV.IT_AGENDA_CENTRAL
                   SET DS_OBSERVACAO = :observacao,
                       DS_OBSERVACAO_GERAL = :observacao_geral
                 WHERE CD_IT_AGENDA_CENTRAL = :slot
                """,
                {
                    'observacao': texto_novo,
                    'observacao_geral': texto_geral_novo,
                    'slot': payload.cd_it_agenda_central,
                },
            )
            if row is not None and row[2] is not None:
                atual_movimento = cursor.execute(
                    """
                    SELECT DS_OBSERVACAO_GERAL
                      FROM DBAMV.MOVIMENTO_AGENDA_CENTRAL
                     WHERE CD_MOVIMENTO_AGENDA_CENTRAL = :movimento
                    """,
                    {'movimento': row[2]},
                ).fetchone()
                cursor.execute(
                    """
                    UPDATE DBAMV.MOVIMENTO_AGENDA_CENTRAL
                       SET DS_OBSERVACAO_GERAL = :observacao
                     WHERE CD_MOVIMENTO_AGENDA_CENTRAL = :movimento
                    """,
                    {
                        'observacao': _anexa_observacao(
                            atual_movimento[0] if atual_movimento else None,
                            observacao,
                        ),
                        'movimento': row[2],
                    },
                )
            connection.commit()
    except HTTPException:
        raise
    except Exception as exc:
        connection.rollback()
        raise HTTPException(
            status_code=HTTPStatus.CONFLICT,
            detail='Falha ao concluir o agendamento no MV.',
        ) from exc
    finally:
        cursor.close()

    if row is None or row[2] is None:
        raise HTTPException(
            status_code=HTTPStatus.SERVICE_UNAVAILABLE,
            detail='O MV confirmou, mas nao retornou o protocolo.',
        )
    _liberar_reserva_horario(
        payload.cd_it_agenda_central, payload.reserva_token
    )
    _registrar_evento_pos_mv(
        engine=postgres_engine,
        usuario_atual=usuario_atual,
        status='agendado',
        cd_paciente=payload.cd_paciente,
        cd_item_agendamento=payload.cd_item_agendamento,
        cd_it_agenda_central=payload.cd_it_agenda_central,
        cd_agenda_central=payload.cd_agenda_central,
        cd_tip_mar=payload.cd_tip_mar,
        protocolo_mv=row[2],
    )
    whatsapp_status, whatsapp_mensagem = _enviar_confirmacao_whatsapp_agendamento(
        session=session,
        payload=payload,
        protocolo=row[2],
    )
    return {
        'status': 'agendado',
        'mensagem': 'Agendamento realizado com sucesso no MV.',
        'protocolo': row[2],
        'movimento_id': row[2],
        'item_movimento_id': row[3],
        'horario_id': row[0],
        'agenda_id': row[1],
        'whatsapp_status': whatsapp_status,
        'whatsapp_mensagem': whatsapp_mensagem,
    }


@router.post(
    '/reagendar',
    status_code=HTTPStatus.OK,
    response_model=AgendamentoReagendado,
)
def reagendar_agendamento(
    payload: ReagendarAgendamentoInput,
    usuario_atual: ValidaUsuarioAtual,
    session: Session = Depends(get_session_oracle),
):
    """Troca o horario em uma unica transacao no SoulMV."""
    garantir_paciente_autorizado(usuario_atual, payload.cd_paciente)
    _garantir_reserva_disponivel(
        payload.cd_it_agenda_central, payload.reserva_token
    )
    if os.getenv('MV_GRAVACAO_HABILITADA', 'false').lower() != 'true':
        raise HTTPException(
            status_code=HTTPStatus.NOT_IMPLEMENTED,
            detail='Gravacao do MV desabilitada neste ambiente.',
        )
    if payload.cd_it_agenda_central_anterior == payload.cd_it_agenda_central:
        raise HTTPException(
            status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
            detail='Selecione um horario diferente do agendamento atual.',
        )

    anterior = session.execute(
        text(
            '''
            SELECT i.CD_PACIENTE,
                   i.CD_ITEM_AGENDAMENTO,
                   (
                       SELECT MAX(im.CD_MOVIMENTO_AGENDA_CENTRAL)
                         FROM DBAMV.IT_MOVIMENTO_AGENDA_CENTRAL im
                        WHERE im.CD_IT_AGENDA_CENTRAL =
                              i.CD_IT_AGENDA_CENTRAL
                          AND im.TP_STATUS NOT IN ('E', 'C', 'P', 'T')
                   ) AS protocolo_mv
              FROM DBAMV.IT_AGENDA_CENTRAL
                   i
             WHERE CD_IT_AGENDA_CENTRAL = :slot
            '''
        ),
        {'slot': payload.cd_it_agenda_central_anterior},
    ).first()
    if anterior is None or anterior[0] != payload.cd_paciente:
        raise HTTPException(
            status_code=HTTPStatus.NOT_FOUND,
            detail='O agendamento atual nao foi encontrado para este paciente.',
        )
    if anterior[1] != payload.cd_item_agendamento:
        raise HTTPException(
            status_code=HTTPStatus.CONFLICT,
            detail='O item do reagendamento difere do agendamento atual.',
        )

    parametros_novo = payload.model_dump()
    parametros_novo.pop('reserva_token', None)
    compativel = session.execute(
        CONSULTA_PRE_VALIDACAO, parametros_novo
    ).first()
    if compativel is None:
        raise HTTPException(
            status_code=HTTPStatus.CONFLICT,
            detail=(
                'O novo horario nao pertence a uma agenda compativel com '
                'este item. Atualize os horarios e tente novamente.'
            ),
        )

    novo = session.execute(
        text(
            '''
            SELECT CD_AGENDA_CENTRAL, CD_ITEM_AGENDAMENTO, CD_PACIENTE,
                   NVL(SN_BLOQUEADO, 'N')
              FROM DBAMV.IT_AGENDA_CENTRAL
             WHERE CD_IT_AGENDA_CENTRAL = :slot
            '''
        ),
        {'slot': payload.cd_it_agenda_central},
    ).first()
    if novo is None:
        raise HTTPException(
            status_code=HTTPStatus.CONFLICT,
            detail='O novo horario nao foi encontrado no MV.',
        )
    if novo[2] is not None or novo[3] == 'S':
        raise HTTPException(
            status_code=HTTPStatus.CONFLICT,
            detail='O novo horario nao esta mais disponivel no MV.',
        )

    connection = session.connection().connection
    cursor = connection.cursor()
    retorno_confirmacao = cursor.var(int)
    row = None
    try:
        # O MV possui uma rotina propria de transferencia. Primeiro o novo
        # horario e confirmado; depois a transferencia nativa libera o slot
        # anterior e vincula o movimento ao novo. Tudo permanece na mesma
        # transacao para que um erro preserve o agendamento original.
        cursor.callproc(
            'DBAMV.PKG_AGENDAMENTO_WEB.PRC_CONCLUI_AGENDAMENTO_WEB',
            [
                payload.cd_paciente,
                payload.cd_item_agendamento,
                'A',
                novo[0],
                payload.cd_it_agenda_central,
                payload.cd_it_agenda_fim or payload.cd_it_agenda_central,
                None,
                payload.cd_tip_mar,
                retorno_confirmacao,
            ],
        )
        if retorno_confirmacao.getvalue() != 1:
            raise HTTPException(
                status_code=HTTPStatus.CONFLICT,
                detail=(
                    'O MV recusou o novo horario. '
                    'O agendamento anterior foi preservado.'
                ),
            )
        cursor.callproc(
            'DBAMV.PKG_AGENDAMENTO_WEB.PRC_REALIZA_TRANSFERENCIA',
            [
                payload.cd_it_agenda_central,
                payload.cd_it_agenda_central_anterior,
            ],
        )
        transferencia = cursor.execute(
            '''
            SELECT anterior.CD_PACIENTE,
                   novo.CD_PACIENTE,
                   novo.CD_ITEM_AGENDAMENTO
              FROM DBAMV.IT_AGENDA_CENTRAL anterior
              JOIN DBAMV.IT_AGENDA_CENTRAL novo
                ON novo.CD_IT_AGENDA_CENTRAL = :slot_novo
             WHERE anterior.CD_IT_AGENDA_CENTRAL = :slot_anterior
            ''',
            {
                'slot_novo': payload.cd_it_agenda_central,
                'slot_anterior': payload.cd_it_agenda_central_anterior,
            },
        ).fetchone()
        if (
            transferencia is None
            or transferencia[0] is not None
            or transferencia[1] != payload.cd_paciente
            or transferencia[2] != payload.cd_item_agendamento
        ):
            raise HTTPException(
                status_code=HTTPStatus.CONFLICT,
                detail=(
                    'O MV nao confirmou a transferencia entre os horarios. '
                    'O agendamento anterior foi preservado.'
                ),
            )
        row = cursor.execute(
            '''
            SELECT i.CD_IT_AGENDA_CENTRAL,
                   i.CD_AGENDA_CENTRAL,
                   im.CD_MOVIMENTO_AGENDA_CENTRAL,
                   im.CD_IT_MOVIMENTO_AGENDA_CENTRAL
              FROM DBAMV.IT_AGENDA_CENTRAL i
              LEFT JOIN DBAMV.IT_MOVIMENTO_AGENDA_CENTRAL im
                ON im.CD_IT_AGENDA_CENTRAL = i.CD_IT_AGENDA_CENTRAL
               AND im.TP_STATUS NOT IN ('E', 'C', 'P', 'T')
             WHERE i.CD_IT_AGENDA_CENTRAL = :slot
             ORDER BY im.CD_IT_MOVIMENTO_AGENDA_CENTRAL DESC
            ''',
            {'slot': payload.cd_it_agenda_central},
        ).fetchone()
        if row is None or row[2] is None:
            raise HTTPException(
                status_code=HTTPStatus.SERVICE_UNAVAILABLE,
                detail=(
                    'O MV nao retornou o protocolo do novo horario. '
                    'O agendamento anterior foi preservado.'
                ),
            )
        connection.commit()
    except HTTPException:
        connection.rollback()
        raise
    except Exception as exc:
        connection.rollback()
        raise HTTPException(
            status_code=HTTPStatus.CONFLICT,
            detail=(
                'Nao foi possivel concluir o reagendamento. '
                'O horario anterior foi preservado.'
            ),
        ) from exc
    finally:
        cursor.close()

    _liberar_reserva_horario(
        payload.cd_it_agenda_central, payload.reserva_token
    )

    _registrar_evento_pos_mv(
        engine=postgres_engine,
        usuario_atual=usuario_atual,
        status='reagendado',
        cd_paciente=payload.cd_paciente,
        cd_item_agendamento=payload.cd_item_agendamento,
        cd_it_agenda_central=payload.cd_it_agenda_central,
        cd_agenda_central=row[1],
        cd_tip_mar=payload.cd_tip_mar,
        protocolo_mv=row[2],
        cd_it_agenda_central_anterior=(
            payload.cd_it_agenda_central_anterior
        ),
        protocolo_mv_anterior=anterior[2],
    )

    whatsapp_status, whatsapp_mensagem = _enviar_confirmacao_whatsapp_agendamento(
        session=session,
        payload=payload,
        protocolo=row[2],
    )
    return {
        'status': 'reagendado',
        'mensagem': 'Agendamento alterado com sucesso no MV.',
        'protocolo': row[2],
        'movimento_id': row[2],
        'item_movimento_id': row[3],
        'horario_id': row[0],
        'agenda_id': row[1],
        'horario_anterior_id': payload.cd_it_agenda_central_anterior,
        'whatsapp_status': whatsapp_status,
        'whatsapp_mensagem': whatsapp_mensagem,
    }


CONSULTA_PACIENTES = text(
    """
    SELECT *
      FROM (
        SELECT p.CD_PACIENTE AS cd_paciente,
               p.NM_PACIENTE AS nm_paciente,
               p.DT_NASCIMENTO AS dt_nascimento,
               p.TP_SEXO AS tp_sexo,
               p.NR_CPF AS nr_cpf,
               p.EMAIL AS email,
               TO_CHAR(p.NR_DDI_CELULAR) AS nr_ddi_celular,
               TO_CHAR(p.NR_DDD_CELULAR) AS nr_ddd_celular,
               TO_CHAR(p.NR_CELULAR) AS nr_celular,
               TO_CHAR(p.NR_DDI_FONE) AS nr_ddi_fone,
               TO_CHAR(p.NR_DDD_FONE) AS nr_ddd_fone,
               TO_CHAR(p.NR_FONE) AS nr_fone,
               TO_CHAR(p.NR_DDI_FONE_COMERCIAL) AS nr_ddi_fone_comercial,
               TO_CHAR(p.NR_DDD_FONE_COMERCIAL) AS nr_ddd_fone_comercial,
               TO_CHAR(p.NR_FONE_COMERCIAL) AS nr_fone_comercial,
               TO_CHAR(p.NR_CEP) AS nr_cep,
               p.DS_ENDERECO AS ds_endereco,
               p.NR_ENDERECO AS nr_endereco,
               p.DS_COMPLEMENTO AS ds_complemento,
               p.NM_BAIRRO AS nm_bairro,
               p.CD_CIDADE AS cd_cidade,
               cid.NM_CIDADE AS nm_cidade,
               cid.CD_UF AS cd_uf
          FROM DBAMV.PACIENTE p
          LEFT JOIN DBAMV.CIDADE cid ON cid.CD_CIDADE = p.CD_CIDADE
         WHERE p.CD_PACIENTE = :codigo
            OR REGEXP_REPLACE(p.NR_CPF, '[^0-9]', '') = :cpf
            OR UPPER(p.NM_PACIENTE) LIKE :nome
         ORDER BY p.NM_PACIENTE, p.CD_PACIENTE
      )
     WHERE ROWNUM <= :limite
    """
)

_CAMPOS_PACIENTE_LOTE = """
    p.CD_PACIENTE AS cd_paciente,
    p.NM_PACIENTE AS nm_paciente,
    TO_CHAR(p.NR_DDD_CELULAR) AS nr_ddd_celular,
    TO_CHAR(p.NR_CELULAR) AS nr_celular,
    TO_CHAR(p.NR_DDD_FONE) AS nr_ddd_fone,
    TO_CHAR(p.NR_FONE) AS nr_fone,
    TO_CHAR(p.NR_DDD_FONE_COMERCIAL) AS nr_ddd_fone_comercial,
    TO_CHAR(p.NR_FONE_COMERCIAL) AS nr_fone_comercial
"""

_FILTRO_PACIENTE_COM_TELEFONE = """
    AND (
        NULLIF(TRIM(TO_CHAR(p.NR_CELULAR)), '') IS NOT NULL
        OR NULLIF(TRIM(TO_CHAR(p.NR_FONE)), '') IS NOT NULL
        OR NULLIF(TRIM(TO_CHAR(p.NR_FONE_COMERCIAL)), '') IS NOT NULL
    )
"""


def _consulta_pacientes_lote(filtro: str) -> TextClause:
    return text(
        f"""
        SELECT *
          FROM (
            SELECT {_CAMPOS_PACIENTE_LOTE}
              FROM DBAMV.PACIENTE p
             WHERE p.CD_PACIENTE > :cursor
               {filtro}
               {_FILTRO_PACIENTE_COM_TELEFONE}
             ORDER BY p.CD_PACIENTE
          )
         WHERE ROWNUM <= :limite_consulta
        """
    )


CONSULTA_PACIENTES_LOTE_CODIGOS = _consulta_pacientes_lote(
    'AND p.CD_PACIENTE BETWEEN :codigo_inicio AND :codigo_fim'
)
CONSULTA_PACIENTES_LOTE_CADASTROS = _consulta_pacientes_lote(
    'AND p.DT_CADASTRO >= :data_inicio '
    'AND p.DT_CADASTRO < :data_fim_exclusiva'
)
CONSULTA_PACIENTES_LOTE_AGENDAMENTOS = _consulta_pacientes_lote(
    """
    AND EXISTS (
        SELECT 1
          FROM DBAMV.IT_AGENDA_CENTRAL i
          LEFT JOIN DBAMV.IT_MOVIMENTO_AGENDA_CENTRAL im
            ON im.CD_IT_AGENDA_CENTRAL = i.CD_IT_AGENDA_CENTRAL
         WHERE i.CD_PACIENTE = p.CD_PACIENTE
           AND i.HR_AGENDA >= :data_inicio
           AND i.HR_AGENDA < :data_fim_exclusiva
           AND NVL(im.TP_STATUS, 'A') NOT IN ('E', 'C', 'P', 'T')
    )
    """
)

CONSULTA_ULTIMOS_ATENDIMENTOS_PACIENTE = text(
    """
    SELECT *
      FROM (
        SELECT a.CD_ATENDIMENTO AS cd_atendimento,
               a.HR_ATENDIMENTO AS horario_atendimento,
               a.TP_ATENDIMENTO AS tipo_atendimento,
               CASE a.TP_ATENDIMENTO
                 WHEN 'A' THEN 'Ambulatorial'
                 WHEN 'E' THEN 'Externo'
                 WHEN 'U' THEN 'Urgência'
                 WHEN 'I' THEN 'Internação'
                 ELSE a.TP_ATENDIMENTO
               END AS ds_tipo_atendimento,
               a.CD_PRESTADOR AS cd_prestador,
               pr.NM_PRESTADOR AS nm_prestador,
               a.CD_CONVENIO AS cd_convenio,
               c.NM_CONVENIO AS nm_convenio,
               a.CD_CON_PLA AS cd_con_pla,
               cp.DS_CON_PLA AS ds_con_pla
          FROM DBAMV.ATENDIME a
          LEFT JOIN DBAMV.PRESTADOR pr
            ON pr.CD_PRESTADOR = a.CD_PRESTADOR
          LEFT JOIN DBAMV.CONVENIO c
            ON c.CD_CONVENIO = a.CD_CONVENIO
          LEFT JOIN DBAMV.CON_PLA cp
            ON cp.CD_CONVENIO = a.CD_CONVENIO
           AND cp.CD_CON_PLA = a.CD_CON_PLA
         WHERE a.CD_PACIENTE = :cd_paciente
           AND a.HR_ATENDIMENTO IS NOT NULL
         ORDER BY a.HR_ATENDIMENTO DESC, a.CD_ATENDIMENTO DESC
      )
     WHERE ROWNUM <= :limite
    """
)

CONSULTA_LINHAS_CUIDADO_PACIENTE = text(
    """
    SELECT *
      FROM (
        SELECT v.CD_ATENDIMENTO AS cd_atendimento,
               v.CD_PACIENTE AS cd_paciente,
               v.CD_DOCUMENTO AS cd_documento,
               v.CD_REGISTRO AS cd_registro,
               v.DS_TIPO_DOCUMENTO AS ds_tipo_documento,
               v.DS_DOCUMENTO AS ds_documento,
               v.TP_STATUS AS tp_status,
               v.DS_CAMPO_FILHO AS ds_campo_filho,
               v.DS_IDENTIFICADOR_FILHO AS ds_identificador_filho,
               v.DS_RESPOSTA AS ds_resposta,
               v.DH_DOCUMENTO AS dh_documento,
               v.DH_FECHAMENTO AS dh_fechamento,
               v.CD_USUARIO_CRIOU AS cd_usuario_criou
          FROM DBAMV.VDIC_PW_RESPOSTA_DOCUMENTO v
         WHERE v.CD_PACIENTE = :cd_paciente
           AND v.CD_DOCUMENTO = 1043
           AND UPPER(v.DS_DOCUMENTO) = 'LC_DAC'
           AND v.TP_STATUS = 'FECHADO'
         ORDER BY v.DH_DOCUMENTO DESC NULLS LAST,
                  v.CD_REGISTRO DESC NULLS LAST,
                  v.CD_CAMPO_FILHO
      )
     WHERE ROWNUM <= 250
    """
)

CONSULTA_ITENS = text(
    """
    SELECT *
      FROM (
        SELECT DISTINCT
               ia.CD_ITEM_AGENDAMENTO AS cd_item_agendamento,
               ia.DS_ITEM_AGENDAMENTO AS ds_item_agendamento,
               ia.CD_EXA_RX AS cd_exa_rx,
               ia.HR_REALIZACAO AS hr_realizacao
          FROM DBAMV.ITEM_AGENDAMENTO ia
          LEFT JOIN DBAMV.AGENDA_CENTRAL_ITEM_AGENDA acia
            ON acia.CD_ITEM_AGENDAMENTO = ia.CD_ITEM_AGENDAMENTO
          LEFT JOIN DBAMV.AGENDA_CENTRAL ac
            ON ac.CD_AGENDA_CENTRAL = acia.CD_AGENDA_CENTRAL
         WHERE (ia.CD_ITEM_AGENDAMENTO = :codigo
            OR UPPER(ia.DS_ITEM_AGENDAMENTO) LIKE :descricao
            OR (:busca_laboratorio = 'S' AND ia.TP_ITEM = 'L'))
           AND NVL(ia.SN_ATIVO, 'S') = 'S'
           AND (
                (:busca_laboratorio = 'S' AND ia.TP_ITEM = 'L')
             OR (ia.TP_ITEM = 'L' AND (ia.CD_ITEM_AGENDAMENTO = :codigo OR UPPER(ia.DS_ITEM_AGENDAMENTO) LIKE :descricao))
             OR (
                   ac.DT_AGENDA >= TRUNC(SYSDATE)
               AND ac.DT_LIBERACAO < SYSDATE
               AND NVL(ac.QT_MARCADOS, 0) < ac.QT_ATENDIMENTO
                )
           )
         ORDER BY ia.DS_ITEM_AGENDAMENTO, ia.CD_ITEM_AGENDAMENTO
      )
     WHERE ROWNUM <= :limite
    """
)

CONSULTA_PROCEDIMENTOS_MV = text(
    """
    SELECT codigo_mv, descricao_mv
      FROM (
        SELECT pf.CD_PRO_FAT AS codigo_mv,
               pf.DS_PRO_FAT AS descricao_mv
          FROM DBAMV.PRO_FAT pf
         WHERE pf.SN_ATIVO = 'S'
           AND (:cursor IS NULL OR pf.CD_PRO_FAT > :cursor)
           AND (pf.CD_PRO_FAT LIKE :codigo
             OR UPPER(pf.DS_PRO_FAT) LIKE :descricao)
         ORDER BY pf.CD_PRO_FAT
      )
     WHERE ROWNUM <= :limite
    """
)

_FILTRO_IDADE_PACIENTE_SQL = """
           AND (
               :cd_paciente IS NULL
               OR ac.NR_IDADE_MINIMA IS NULL
               OR ac.NR_IDADE_MAXIMA IS NULL
               OR NOT EXISTS (
                   SELECT 1
                     FROM DBAMV.PACIENTE pac_idade
                    WHERE pac_idade.CD_PACIENTE = :cd_paciente
                      AND pac_idade.DT_NASCIMENTO IS NOT NULL
               )
               OR EXISTS (
                   SELECT 1
                     FROM DBAMV.PACIENTE pac_idade
                    WHERE pac_idade.CD_PACIENTE = :cd_paciente
                      AND pac_idade.DT_NASCIMENTO IS NOT NULL
                      AND TRUNC(
                          MONTHS_BETWEEN(
                              TRUNC(SYSDATE),
                              TRUNC(pac_idade.DT_NASCIMENTO)
                          ) / 12
                      ) BETWEEN ac.NR_IDADE_MINIMA AND ac.NR_IDADE_MAXIMA
               )
           )
"""


CONSULTA_PRESTADORES_ITEM = text(
    f"""
    SELECT *
      FROM (
        SELECT DISTINCT
               p.CD_PRESTADOR AS cd_prestador,
               p.NM_PRESTADOR AS nm_prestador,
               p.DS_CODIGO_CONSELHO AS ds_codigo_conselho
          FROM DBAMV.AGENDA_CENTRAL_ITEM_AGENDA acia
          JOIN DBAMV.AGENDA_CENTRAL ac
            ON ac.CD_AGENDA_CENTRAL = acia.CD_AGENDA_CENTRAL
          JOIN DBAMV.IT_AGENDA_CENTRAL iac
            ON iac.CD_AGENDA_CENTRAL = ac.CD_AGENDA_CENTRAL
          JOIN DBAMV.PRESTADOR p
            ON p.CD_PRESTADOR = ac.CD_PRESTADOR
         WHERE acia.CD_ITEM_AGENDAMENTO = :cd_item_agendamento
           AND p.TP_SITUACAO = 'A'
           AND ac.DT_AGENDA BETWEEN :data_inicio AND :data_fim
           AND ac.DT_LIBERACAO < SYSDATE
           AND NVL(ac.QT_MARCADOS, 0) < ac.QT_ATENDIMENTO
           AND iac.HR_AGENDA >= SYSDATE
           AND iac.CD_PACIENTE IS NULL
           AND iac.DT_GRAVACAO IS NULL
           AND NVL(iac.SN_BLOQUEADO, 'N') <> 'S'
{_FILTRO_IDADE_PACIENTE_SQL}
         ORDER BY p.NM_PRESTADOR, p.CD_PRESTADOR
      )
     WHERE ROWNUM <= :limite
    """
)

CONSULTA_ITEM_EXIGE_PRESTADOR = text(
    """
    SELECT COUNT(*)
      FROM (
        SELECT iap.CD_PRESTADOR
          FROM DBAMV.ITEM_AGENDAMENTO_PRESTADOR iap
         WHERE iap.CD_ITEM_AGENDAMENTO = :cd_item_agendamento
           AND ROWNUM = 1
        UNION ALL
        SELECT ac.CD_PRESTADOR
          FROM DBAMV.AGENDA_CENTRAL_ITEM_AGENDA acia
          JOIN DBAMV.AGENDA_CENTRAL ac
            ON ac.CD_AGENDA_CENTRAL = acia.CD_AGENDA_CENTRAL
         WHERE acia.CD_ITEM_AGENDAMENTO = :cd_item_agendamento
           AND ac.CD_PRESTADOR IS NOT NULL
           AND ROWNUM = 1
      )
    """
)

CONSULTA_PRE_VALIDACAO = text(
    """
    SELECT iac.CD_IT_AGENDA_CENTRAL AS cd_it_agenda_central,
           iac.CD_AGENDA_CENTRAL AS cd_agenda_central,
           iac.CD_TIP_MAR AS cd_tip_mar,
           iac.HR_AGENDA AS horario,
           iac.CD_PACIENTE AS slot_cd_paciente,
           iac.DT_GRAVACAO AS slot_dt_gravacao,
           NVL(iac.SN_BLOQUEADO, 'N') AS slot_bloqueado,
           CASE
               WHEN iac.HR_AGENDA > SYSDATE THEN 'S'
               ELSE 'N'
           END AS slot_futuro,
           pac.CD_PACIENTE AS cd_paciente,
           pac.NM_PACIENTE AS nm_paciente,
           ia.CD_ITEM_AGENDAMENTO AS cd_item_agendamento,
           ia.DS_ITEM_AGENDAMENTO AS ds_item_agendamento,
           c.CD_CONVENIO AS cd_convenio,
           c.NM_CONVENIO AS nm_convenio,
           cp.CD_CON_PLA AS cd_con_pla,
           cp.DS_CON_PLA AS ds_con_pla,
           ac.CD_PRESTADOR AS cd_prestador,
           p.NM_PRESTADOR AS nm_prestador,
           ua.DS_UNIDADE_ATENDIMENTO AS ds_unidade_atendimento,
           ua.DS_LOCAL_UNIDADE_ATENDIMENTO
               AS ds_local_unidade_atendimento
      FROM DBAMV.IT_AGENDA_CENTRAL iac
      JOIN DBAMV.AGENDA_CENTRAL ac
        ON ac.CD_AGENDA_CENTRAL = iac.CD_AGENDA_CENTRAL
      LEFT JOIN DBAMV.AGENDA_CENTRAL_ITEM_AGENDA acia
        ON acia.CD_AGENDA_CENTRAL = ac.CD_AGENDA_CENTRAL
       AND acia.CD_ITEM_AGENDAMENTO = :cd_item_agendamento
      JOIN DBAMV.ITEM_AGENDAMENTO ia
        ON ia.CD_ITEM_AGENDAMENTO = :cd_item_agendamento
      JOIN DBAMV.PACIENTE pac
        ON pac.CD_PACIENTE = :cd_paciente
      JOIN DBAMV.CONVENIO c
        ON c.CD_CONVENIO = :cd_convenio
      JOIN DBAMV.CON_PLA cp
        ON cp.CD_CONVENIO = c.CD_CONVENIO
       AND cp.CD_CON_PLA = :cd_con_pla
      LEFT JOIN DBAMV.PRESTADOR p
        ON p.CD_PRESTADOR = ac.CD_PRESTADOR
      LEFT JOIN DBAMV.UNIDADE_ATENDIMENTO ua
        ON ua.CD_UNIDADE_ATENDIMENTO = ac.CD_UNIDADE_ATENDIMENTO
      LEFT JOIN DBAMV.RECURSO_CENTRAL rc
        ON rc.CD_RECURSO_CENTRAL = ac.CD_RECURSO_CENTRAL
      LEFT JOIN DBAMV.SETOR st
        ON st.CD_SETOR = ac.CD_SETOR
     WHERE iac.CD_IT_AGENDA_CENTRAL = :cd_it_agenda_central
       AND (
           acia.CD_ITEM_AGENDAMENTO IS NOT NULL
           OR (
               ia.TP_ITEM = 'L'
               AND (
                   ac.TP_AGENDA = 'L'
                   OR UPPER(NVL(rc.DS_RECURSO_CENTRAL, '')) LIKE '%LAB%'
                   OR UPPER(NVL(st.NM_SETOR, '')) LIKE '%LAB%'
               )
           )
       )
       AND (
               :cd_tip_mar IS NULL
               OR EXISTS (
                   SELECT 1
                     FROM DBAMV.AGENDA_CENTRAL_SER_TIPO acst
                    WHERE acst.CD_AGENDA_CENTRAL = ac.CD_AGENDA_CENTRAL
                      AND acst.CD_TIP_MAR = :cd_tip_mar
               )
               OR (
                   NOT EXISTS (
                       SELECT 1
                         FROM DBAMV.AGENDA_CENTRAL_SER_TIPO acst_cfg
                        WHERE acst_cfg.CD_AGENDA_CENTRAL = ac.CD_AGENDA_CENTRAL
                   )
                   AND EXISTS (
                       SELECT 1
                         FROM DBAMV.ESCALA_CENTRAL_SER_TIPO ecst
                        WHERE ecst.CD_ESCALA_CENTRAL = ac.CD_ESCALA_CENTRAL
                          AND ecst.CD_TIP_MAR = :cd_tip_mar
                   )
               )
           )
    """
)

CONSULTA_SLOT_TIPO_COMPATIVEL = text(
    """
    SELECT COUNT(*)
      FROM DBAMV.IT_AGENDA_CENTRAL iac
      JOIN DBAMV.AGENDA_CENTRAL ac
        ON ac.CD_AGENDA_CENTRAL = iac.CD_AGENDA_CENTRAL
     WHERE iac.CD_IT_AGENDA_CENTRAL = :cd_it_agenda_central
       AND (
           EXISTS (
               SELECT 1
                 FROM DBAMV.AGENDA_CENTRAL_SER_TIPO acst
                WHERE acst.CD_AGENDA_CENTRAL = ac.CD_AGENDA_CENTRAL
                  AND acst.CD_TIP_MAR = :cd_tip_mar
           )
           OR (
               NOT EXISTS (
                   SELECT 1
                     FROM DBAMV.AGENDA_CENTRAL_SER_TIPO acst_cfg
                    WHERE acst_cfg.CD_AGENDA_CENTRAL = ac.CD_AGENDA_CENTRAL
               )
               AND EXISTS (
                   SELECT 1
                     FROM DBAMV.ESCALA_CENTRAL_SER_TIPO ecst
                    WHERE ecst.CD_ESCALA_CENTRAL = ac.CD_ESCALA_CENTRAL
                      AND ecst.CD_TIP_MAR = :cd_tip_mar
               )
           )
       )
    """
)

CONSULTA_AGENDAMENTO_DUPLICADO = text(
    """
    SELECT *
      FROM (
        SELECT iac.CD_IT_AGENDA_CENTRAL AS cd_it_agenda_central,
               iac.HR_AGENDA AS horario
          FROM DBAMV.IT_AGENDA_CENTRAL iac
         WHERE iac.CD_PACIENTE = :cd_paciente
           AND iac.CD_ITEM_AGENDAMENTO = :cd_item_agendamento
           AND iac.HR_AGENDA >= SYSDATE
         ORDER BY iac.HR_AGENDA
      )
     WHERE ROWNUM = 1
    """
)

CONSULTA_AGENDAMENTOS_PACIENTE = text(
    """
    SELECT im.CD_MOVIMENTO_AGENDA_CENTRAL AS protocolo,
           im.CD_IT_MOVIMENTO_AGENDA_CENTRAL AS item_movimento_id,
           i.CD_IT_AGENDA_CENTRAL AS cd_it_agenda_central,
           i.CD_AGENDA_CENTRAL AS cd_agenda_central,
           i.CD_ITEM_AGENDAMENTO AS cd_item_agendamento,
           ia.DS_ITEM_AGENDAMENTO AS ds_item_agendamento,
           i.HR_AGENDA AS horario,
           ac.CD_PRESTADOR AS cd_prestador,
           p.NM_PRESTADOR AS nm_prestador,
           ua.DS_UNIDADE_ATENDIMENTO AS ds_unidade_atendimento,
           im.TP_STATUS AS status,
           i.CD_TIP_MAR AS cd_tip_mar,
           i.CD_CONVENIO AS cd_convenio,
           c.NM_CONVENIO AS nm_convenio,
           i.CD_CON_PLA AS cd_con_pla,
           cp.DS_CON_PLA AS ds_con_pla
      FROM DBAMV.IT_AGENDA_CENTRAL i
      JOIN DBAMV.AGENDA_CENTRAL ac
        ON ac.CD_AGENDA_CENTRAL = i.CD_AGENDA_CENTRAL
      JOIN DBAMV.ITEM_AGENDAMENTO ia
        ON ia.CD_ITEM_AGENDAMENTO = i.CD_ITEM_AGENDAMENTO
      LEFT JOIN DBAMV.IT_MOVIMENTO_AGENDA_CENTRAL im
        ON im.CD_IT_AGENDA_CENTRAL = i.CD_IT_AGENDA_CENTRAL
       AND im.TP_STATUS NOT IN ('E', 'C', 'P', 'T')
      LEFT JOIN DBAMV.PRESTADOR p ON p.CD_PRESTADOR = ac.CD_PRESTADOR
      LEFT JOIN DBAMV.UNIDADE_ATENDIMENTO ua
        ON ua.CD_UNIDADE_ATENDIMENTO = ac.CD_UNIDADE_ATENDIMENTO
      LEFT JOIN DBAMV.CONVENIO c
        ON c.CD_CONVENIO = i.CD_CONVENIO
      LEFT JOIN DBAMV.CON_PLA cp
        ON cp.CD_CONVENIO = i.CD_CONVENIO
       AND cp.CD_CON_PLA = i.CD_CON_PLA
     WHERE i.CD_PACIENTE = :cd_paciente
       AND TRUNC(i.HR_AGENDA) >= TRUNC(SYSDATE)
       AND i.CD_IT_AGENDA_PAI IS NULL
     ORDER BY i.HR_AGENDA
    """
)

CONSULTA_RANKING_OPERADORES = text(
    """
    WITH registros AS (
        SELECT i.CD_IT_AGENDA_CENTRAL AS cd_it_agenda_central,
               i.CD_PACIENTE AS cd_paciente,
               TRIM(REGEXP_SUBSTR(
                   COALESCE(i.DS_OBSERVACAO, i.DS_OBSERVACAO_GERAL),
                   'OPERADOR:[[:space:]]*([^|]+)',
                   1, 1, 'i', 1
               )) AS operador
          FROM DBAMV.IT_AGENDA_CENTRAL i
         WHERE i.DT_GRAVACAO >= :data_inicio
           AND i.DT_GRAVACAO < :data_fim_exclusiva
           AND UPPER(COALESCE(
               i.DS_OBSERVACAO,
               i.DS_OBSERVACAO_GERAL,
               ' '
           )) LIKE '%AGENDAMENTO WEB MEU PRONTOCARDIO%'
    ), normalizados AS (
        SELECT cd_it_agenda_central,
               cd_paciente,
               CASE
                 WHEN REGEXP_LIKE(
                     UPPER(operador),
                     '^MARIA (DEYSIANE|DESYSIANE)'
                 ) THEN 'MARIA DEYSIANE NASCIMENTO OLIVEIRA'
                 WHEN UPPER(operador) IN (
                     'MARIA ALINNE FERREIRA GOMES',
                     'MARIA ALINNE FERRREIRA GOMES',
                     'MARIA ALINNE FEREIRA GOMES'
                 ) THEN 'MARIA ALINNE FERREIRA GOMES'
                 WHEN UPPER(operador) IN ('ANDREZA', 'MARIA.ANDREZA')
                   THEN 'ANDREZA'
                 WHEN UPPER(operador) IN ('BRUNA', 'BRUNA ALVES COSTA')
                   THEN 'BRUNA ALVES COSTA'
                 WHEN UPPER(operador) IN ('AGLAICE', 'AGLAIC')
                   THEN 'AGLAICE'
                 WHEN UPPER(operador) IN (
                     'KARLIANE', 'KARLAINE', 'KARLI'
                 ) THEN 'KARLIANE'
                 WHEN UPPER(operador) IN ('LARA', 'LARA3') THEN 'LARA'
                 WHEN operador IS NULL OR REGEXP_LIKE(
                     operador, '^[[:digit:][:space:]]+$'
                 ) THEN 'OPERADOR NAO IDENTIFICADO'
                 ELSE UPPER(operador)
               END AS operador
          FROM registros
    )
    SELECT operador,
           COUNT(*) AS itens_agendados,
           COUNT(DISTINCT cd_paciente) AS pacientes_distintos
      FROM normalizados
     GROUP BY operador
     ORDER BY itens_agendados DESC, operador
    """
)

CONSULTA_HORARIOS = text(
    f"""
    SELECT *
      FROM (
        SELECT iac.CD_IT_AGENDA_CENTRAL AS cd_it_agenda_central,
               iac.CD_AGENDA_CENTRAL AS cd_agenda_central,
               iac.CD_TIP_MAR AS cd_tip_mar,
               ia.CD_ITEM_AGENDAMENTO AS cd_item_agendamento,
               ia.DS_ITEM_AGENDAMENTO AS ds_item_agendamento,
               ac.DT_AGENDA AS data_agenda,
               iac.HR_AGENDA AS horario,
               ac.CD_UNIDADE_ATENDIMENTO AS cd_unidade_atendimento,
               ua.DS_UNIDADE_ATENDIMENTO AS ds_unidade_atendimento,
               ua.DS_LOCAL_UNIDADE_ATENDIMENTO
                   AS ds_local_unidade_atendimento,
               ac.CD_PRESTADOR AS cd_prestador,
               p.NM_PRESTADOR AS nm_prestador
          FROM DBAMV.IT_AGENDA_CENTRAL iac
          JOIN DBAMV.AGENDA_CENTRAL ac
            ON ac.CD_AGENDA_CENTRAL = iac.CD_AGENDA_CENTRAL
          LEFT JOIN DBAMV.ESCALA_CENTRAL esc
            ON esc.CD_ESCALA_CENTRAL = ac.CD_ESCALA_CENTRAL
          LEFT JOIN DBAMV.AGENDA_CENTRAL_ITEM_AGENDA acia
            ON acia.CD_AGENDA_CENTRAL = ac.CD_AGENDA_CENTRAL
           AND acia.CD_ITEM_AGENDAMENTO = :cd_item_agendamento
          JOIN DBAMV.ITEM_AGENDAMENTO ia
            ON ia.CD_ITEM_AGENDAMENTO = :cd_item_agendamento
          LEFT JOIN DBAMV.UNIDADE_ATENDIMENTO ua
            ON ua.CD_UNIDADE_ATENDIMENTO = ac.CD_UNIDADE_ATENDIMENTO
          LEFT JOIN DBAMV.UNIDADE_ATENDIMENTO ua_esc
            ON ua_esc.CD_UNIDADE_ATENDIMENTO = esc.CD_UNIDADE_ATENDIMENTO
          LEFT JOIN DBAMV.PRESTADOR p
            ON p.CD_PRESTADOR = ac.CD_PRESTADOR
          LEFT JOIN DBAMV.PRESTADOR p_esc
            ON p_esc.CD_PRESTADOR = esc.CD_PRESTADOR
          LEFT JOIN DBAMV.RECURSO_CENTRAL rc
            ON rc.CD_RECURSO_CENTRAL = ac.CD_RECURSO_CENTRAL
          LEFT JOIN DBAMV.RECURSO_CENTRAL rc_esc
            ON rc_esc.CD_RECURSO_CENTRAL = esc.CD_RECURSO_CENTRAL
          LEFT JOIN DBAMV.SETOR st
            ON st.CD_SETOR = ac.CD_SETOR
          LEFT JOIN DBAMV.SETOR st_esc
            ON st_esc.CD_SETOR = esc.CD_SETOR
          LEFT JOIN DBAMV.CONVENIO conv_filtro
            ON conv_filtro.CD_CONVENIO = :cd_convenio
         WHERE ia.CD_ITEM_AGENDAMENTO = :cd_item_agendamento
           AND (
               acia.CD_ITEM_AGENDAMENTO IS NOT NULL
               OR (
                   ia.TP_ITEM = 'L'
                   AND (
                       ac.TP_AGENDA = 'L'
                       OR esc.TP_ESCALA = 'L'
                       OR UPPER(NVL(rc.DS_RECURSO_CENTRAL, '')) LIKE '%LAB%'
                       OR UPPER(NVL(rc_esc.DS_RECURSO_CENTRAL, '')) LIKE '%LAB%'
                       OR UPPER(NVL(st.NM_SETOR, '')) LIKE '%LAB%'
                       OR UPPER(NVL(st_esc.NM_SETOR, '')) LIKE '%LAB%'
                   )
               )
           )
           AND ac.DT_AGENDA BETWEEN :data_inicio AND :data_fim
           AND iac.HR_AGENDA >= SYSDATE
           AND iac.CD_PACIENTE IS NULL
           AND iac.DT_GRAVACAO IS NULL
           AND NVL(iac.SN_BLOQUEADO, 'N') <> 'S'
           AND NVL(iac.SN_ENCAIXE, 'N') <> 'S'
           AND NVL(iac.TP_SITUACAO, 'M') <> 'C'
           AND ac.DT_LIBERACAO < SYSDATE
           AND NVL(ac.SN_ATIVO, 'S') <> 'N'
           AND NVL(ac.SN_FALTA, 'N') <> 'S'
           AND NVL(ac.QT_MARCADOS, 0) < ac.QT_ATENDIMENTO
{_FILTRO_IDADE_PACIENTE_SQL}
           AND (:cd_prestador IS NULL
                OR ac.CD_PRESTADOR = :cd_prestador)
           AND (
               :cd_tip_mar IS NULL
               OR EXISTS (
                   SELECT 1
                     FROM DBAMV.AGENDA_CENTRAL_SER_TIPO acst
                    WHERE acst.CD_AGENDA_CENTRAL = ac.CD_AGENDA_CENTRAL
                      AND acst.CD_TIP_MAR = :cd_tip_mar
               )
               OR (
                   NOT EXISTS (
                       SELECT 1
                         FROM DBAMV.AGENDA_CENTRAL_SER_TIPO acst_cfg
                        WHERE acst_cfg.CD_AGENDA_CENTRAL = ac.CD_AGENDA_CENTRAL
                   )
                   AND EXISTS (
                       SELECT 1
                         FROM DBAMV.ESCALA_CENTRAL_SER_TIPO ecst
                        WHERE ecst.CD_ESCALA_CENTRAL = ac.CD_ESCALA_CENTRAL
                          AND ecst.CD_TIP_MAR = :cd_tip_mar
                   )
               )
           )
           AND (:cd_paciente IS NULL
                OR ac.CD_COR_AREA_FAMILIA IS NULL
                OR EXISTS (
                    SELECT 1
                      FROM DBAMV.PACIENTE pac_cor
                     WHERE pac_cor.CD_PACIENTE = :cd_paciente
                       AND pac_cor.CD_COR_AREA_FAMILIA = ac.CD_COR_AREA_FAMILIA
                ))
           AND (:cd_convenio IS NULL
                OR (
                    ia.TP_ITEM = 'L'
                    AND (
                        ac.TP_AGENDA = 'L'
                        OR esc.TP_ESCALA = 'L'
                        OR UPPER(NVL(rc.DS_RECURSO_CENTRAL, '')) LIKE '%LAB%'
                        OR UPPER(NVL(rc_esc.DS_RECURSO_CENTRAL, '')) LIKE '%LAB%'
                        OR UPPER(NVL(st.NM_SETOR, '')) LIKE '%LAB%'
                        OR UPPER(NVL(st_esc.NM_SETOR, '')) LIKE '%LAB%'
                    )
                )
                OR EXISTS (
                    SELECT 1
                      FROM DBAMV.EMPRESA_CONVENIO ec
                     WHERE ec.CD_MULTI_EMPRESA = ac.CD_MULTI_EMPRESA
                       AND ec.CD_CONVENIO = :cd_convenio
                       AND ec.SN_ATIVO = 'S'
                ))
           AND (:cd_convenio IS NULL
                OR (
                    ia.TP_ITEM = 'L'
                    AND (
                        ac.TP_AGENDA = 'L'
                        OR esc.TP_ESCALA = 'L'
                        OR UPPER(NVL(rc.DS_RECURSO_CENTRAL, '')) LIKE '%LAB%'
                        OR UPPER(NVL(rc_esc.DS_RECURSO_CENTRAL, '')) LIKE '%LAB%'
                        OR UPPER(NVL(st.NM_SETOR, '')) LIKE '%LAB%'
                        OR UPPER(NVL(st_esc.NM_SETOR, '')) LIKE '%LAB%'
                    )
                )
                OR NVL(ac.SN_SIA, 'A') = 'A'
                OR (NVL(ac.SN_SIA, 'A') = 'S'
                    AND conv_filtro.TP_CONVENIO = 'A')
                OR (NVL(ac.SN_SIA, 'A') = 'N'
                    AND conv_filtro.TP_CONVENIO IN ('C', 'P')))
           AND (:cd_convenio IS NULL
                OR (
                    ia.TP_ITEM = 'L'
                    AND (
                        ac.TP_AGENDA = 'L'
                        OR esc.TP_ESCALA = 'L'
                        OR UPPER(NVL(rc.DS_RECURSO_CENTRAL, '')) LIKE '%LAB%'
                        OR UPPER(NVL(rc_esc.DS_RECURSO_CENTRAL, '')) LIKE '%LAB%'
                        OR UPPER(NVL(st.NM_SETOR, '')) LIKE '%LAB%'
                        OR UPPER(NVL(st_esc.NM_SETOR, '')) LIKE '%LAB%'
                    )
                )
                OR NOT EXISTS (
                    SELECT 1
                      FROM DBAMV.AGENDA_CENTRAL_CONVENIO acc
                     WHERE acc.CD_AGENDA_CENTRAL = ac.CD_AGENDA_CENTRAL
                )
                OR EXISTS (
                    SELECT 1
                      FROM DBAMV.AGENDA_CENTRAL_CONVENIO acc
                     WHERE acc.CD_AGENDA_CENTRAL = ac.CD_AGENDA_CENTRAL
                       AND acc.CD_CONVENIO = :cd_convenio
                ))
         ORDER BY ac.DT_AGENDA, iac.HR_AGENDA
      )
     WHERE ROWNUM <= :limite
    """
)

CONSULTA_DIAS_DISPONIVEIS_ITEM = text(
    f"""
    SELECT *
      FROM (
        SELECT ac.DT_AGENDA AS data_agenda,
               ia.CD_ITEM_AGENDAMENTO AS cd_item_agendamento,
               MAX(ia.DS_ITEM_AGENDAMENTO) AS ds_item_agendamento,
               COUNT(*) AS total_horarios,
               MIN(iac.HR_AGENDA) AS primeiro_horario
          FROM DBAMV.IT_AGENDA_CENTRAL iac
          JOIN DBAMV.AGENDA_CENTRAL ac
            ON ac.CD_AGENDA_CENTRAL = iac.CD_AGENDA_CENTRAL
          LEFT JOIN DBAMV.ESCALA_CENTRAL esc
            ON esc.CD_ESCALA_CENTRAL = ac.CD_ESCALA_CENTRAL
          LEFT JOIN DBAMV.AGENDA_CENTRAL_ITEM_AGENDA acia
            ON acia.CD_AGENDA_CENTRAL = ac.CD_AGENDA_CENTRAL
           AND acia.CD_ITEM_AGENDAMENTO = :cd_item_agendamento
          JOIN DBAMV.ITEM_AGENDAMENTO ia
            ON ia.CD_ITEM_AGENDAMENTO = :cd_item_agendamento
          LEFT JOIN DBAMV.RECURSO_CENTRAL rc
            ON rc.CD_RECURSO_CENTRAL = ac.CD_RECURSO_CENTRAL
          LEFT JOIN DBAMV.RECURSO_CENTRAL rc_esc
            ON rc_esc.CD_RECURSO_CENTRAL = esc.CD_RECURSO_CENTRAL
          LEFT JOIN DBAMV.SETOR st
            ON st.CD_SETOR = ac.CD_SETOR
          LEFT JOIN DBAMV.SETOR st_esc
            ON st_esc.CD_SETOR = esc.CD_SETOR
          LEFT JOIN DBAMV.CONVENIO conv_filtro
            ON conv_filtro.CD_CONVENIO = :cd_convenio
         WHERE ia.CD_ITEM_AGENDAMENTO = :cd_item_agendamento
           AND (
               acia.CD_ITEM_AGENDAMENTO IS NOT NULL
               OR (
                   ia.TP_ITEM = 'L'
                   AND (
                       ac.TP_AGENDA = 'L'
                       OR esc.TP_ESCALA = 'L'
                       OR UPPER(NVL(rc.DS_RECURSO_CENTRAL, '')) LIKE '%LAB%'
                       OR UPPER(NVL(rc_esc.DS_RECURSO_CENTRAL, '')) LIKE '%LAB%'
                       OR UPPER(NVL(st.NM_SETOR, '')) LIKE '%LAB%'
                       OR UPPER(NVL(st_esc.NM_SETOR, '')) LIKE '%LAB%'
                   )
               )
           )
           AND ac.DT_AGENDA BETWEEN :data_inicio AND :data_fim
           AND iac.HR_AGENDA >= SYSDATE
           AND iac.CD_PACIENTE IS NULL
           AND iac.DT_GRAVACAO IS NULL
           AND NVL(iac.SN_BLOQUEADO, 'N') <> 'S'
           AND NVL(iac.SN_ENCAIXE, 'N') <> 'S'
           AND NVL(iac.TP_SITUACAO, 'M') <> 'C'
           AND ac.DT_LIBERACAO < SYSDATE
           AND NVL(ac.SN_ATIVO, 'S') <> 'N'
           AND NVL(ac.SN_FALTA, 'N') <> 'S'
           AND NVL(ac.QT_MARCADOS, 0) < ac.QT_ATENDIMENTO
{_FILTRO_IDADE_PACIENTE_SQL}
           AND (:cd_prestador IS NULL
                OR ac.CD_PRESTADOR = :cd_prestador)
           AND (
               :cd_tip_mar IS NULL
               OR EXISTS (
                   SELECT 1
                     FROM DBAMV.AGENDA_CENTRAL_SER_TIPO acst
                    WHERE acst.CD_AGENDA_CENTRAL = ac.CD_AGENDA_CENTRAL
                      AND acst.CD_TIP_MAR = :cd_tip_mar
               )
               OR (
                   NOT EXISTS (
                       SELECT 1
                         FROM DBAMV.AGENDA_CENTRAL_SER_TIPO acst_cfg
                        WHERE acst_cfg.CD_AGENDA_CENTRAL = ac.CD_AGENDA_CENTRAL
                   )
                   AND EXISTS (
                       SELECT 1
                         FROM DBAMV.ESCALA_CENTRAL_SER_TIPO ecst
                        WHERE ecst.CD_ESCALA_CENTRAL = ac.CD_ESCALA_CENTRAL
                          AND ecst.CD_TIP_MAR = :cd_tip_mar
                   )
               )
           )
           AND (:cd_paciente IS NULL
                OR ac.CD_COR_AREA_FAMILIA IS NULL
                OR EXISTS (
                    SELECT 1
                      FROM DBAMV.PACIENTE pac_cor
                     WHERE pac_cor.CD_PACIENTE = :cd_paciente
                       AND pac_cor.CD_COR_AREA_FAMILIA = ac.CD_COR_AREA_FAMILIA
                ))
           AND (:cd_convenio IS NULL
                OR (
                    ia.TP_ITEM = 'L'
                    AND (
                        ac.TP_AGENDA = 'L'
                        OR esc.TP_ESCALA = 'L'
                        OR UPPER(NVL(rc.DS_RECURSO_CENTRAL, '')) LIKE '%LAB%'
                        OR UPPER(NVL(rc_esc.DS_RECURSO_CENTRAL, '')) LIKE '%LAB%'
                        OR UPPER(NVL(st.NM_SETOR, '')) LIKE '%LAB%'
                        OR UPPER(NVL(st_esc.NM_SETOR, '')) LIKE '%LAB%'
                    )
                )
                OR EXISTS (
                    SELECT 1
                      FROM DBAMV.EMPRESA_CONVENIO ec
                     WHERE ec.CD_MULTI_EMPRESA = ac.CD_MULTI_EMPRESA
                       AND ec.CD_CONVENIO = :cd_convenio
                       AND ec.SN_ATIVO = 'S'
                ))
           AND (:cd_convenio IS NULL
                OR (
                    ia.TP_ITEM = 'L'
                    AND (
                        ac.TP_AGENDA = 'L'
                        OR esc.TP_ESCALA = 'L'
                        OR UPPER(NVL(rc.DS_RECURSO_CENTRAL, '')) LIKE '%LAB%'
                        OR UPPER(NVL(rc_esc.DS_RECURSO_CENTRAL, '')) LIKE '%LAB%'
                        OR UPPER(NVL(st.NM_SETOR, '')) LIKE '%LAB%'
                        OR UPPER(NVL(st_esc.NM_SETOR, '')) LIKE '%LAB%'
                    )
                )
                OR NVL(ac.SN_SIA, 'A') = 'A'
                OR (NVL(ac.SN_SIA, 'A') = 'S'
                    AND conv_filtro.TP_CONVENIO = 'A')
                OR (NVL(ac.SN_SIA, 'A') = 'N'
                    AND conv_filtro.TP_CONVENIO IN ('C', 'P')))
           AND (:cd_convenio IS NULL
                OR (
                    ia.TP_ITEM = 'L'
                    AND (
                        ac.TP_AGENDA = 'L'
                        OR esc.TP_ESCALA = 'L'
                        OR UPPER(NVL(rc.DS_RECURSO_CENTRAL, '')) LIKE '%LAB%'
                        OR UPPER(NVL(rc_esc.DS_RECURSO_CENTRAL, '')) LIKE '%LAB%'
                        OR UPPER(NVL(st.NM_SETOR, '')) LIKE '%LAB%'
                        OR UPPER(NVL(st_esc.NM_SETOR, '')) LIKE '%LAB%'
                    )
                )
                OR NOT EXISTS (
                    SELECT 1
                      FROM DBAMV.AGENDA_CENTRAL_CONVENIO acc
                     WHERE acc.CD_AGENDA_CENTRAL = ac.CD_AGENDA_CENTRAL
                )
                OR EXISTS (
                    SELECT 1
                      FROM DBAMV.AGENDA_CENTRAL_CONVENIO acc
                     WHERE acc.CD_AGENDA_CENTRAL = ac.CD_AGENDA_CENTRAL
                       AND acc.CD_CONVENIO = :cd_convenio
                ))
         GROUP BY ac.DT_AGENDA, ia.CD_ITEM_AGENDAMENTO
         ORDER BY ac.DT_AGENDA
      )
     WHERE ROWNUM <= :limite_dias
    """
)


def _cpf_final(cpf: str | None) -> str | None:
    if not cpf:
        return None
    digitos = ''.join(caractere for caractere in cpf if caractere.isdigit())
    return digitos[-4:] if digitos else None


def _consulta_horarios_mv(
    session: Session,
    cd_item_agendamento: int,
    data_inicio: date,
    data_fim: date,
    limite: int,
    cd_prestador: int | None = None,
    cd_tip_mar: int | None = None,
    cd_paciente: int | None = None,
    cd_convenio: int | None = None,
    cd_con_pla: int | None = None,
) -> list[dict]:
    rows = (
        session
        .execute(
            CONSULTA_HORARIOS,
            {
                'cd_item_agendamento': cd_item_agendamento,
                'data_inicio': data_inicio,
                'data_fim': data_fim,
                'limite': limite,
                'cd_prestador': cd_prestador,
                'cd_tip_mar': cd_tip_mar,
                'cd_paciente': cd_paciente,
                'cd_convenio': cd_convenio,
                'cd_con_pla': cd_con_pla,
            },
        )
        .mappings()
        .all()
    )
    return _normaliza_horarios_exibicao([dict(row) for row in rows])


def _consulta_dias_disponiveis_item(
    session: Session,
    cd_item_agendamento: int,
    data_inicio: date,
    data_fim: date,
    limite_dias: int,
    cd_prestador: int | None = None,
    cd_tip_mar: int | None = None,
    cd_paciente: int | None = None,
    cd_convenio: int | None = None,
    cd_con_pla: int | None = None,
) -> list[dict]:
    rows = (
        session
        .execute(
            CONSULTA_DIAS_DISPONIVEIS_ITEM,
            {
                'cd_item_agendamento': cd_item_agendamento,
                'data_inicio': data_inicio,
                'data_fim': data_fim,
                'limite_dias': limite_dias,
                'cd_prestador': cd_prestador,
                'cd_tip_mar': cd_tip_mar,
                'cd_paciente': cd_paciente,
                'cd_convenio': cd_convenio,
                'cd_con_pla': cd_con_pla,
            },
        )
        .mappings()
        .all()
    )
    return [dict(row) for row in rows]


def _data_horario(horario: dict) -> date:
    valor = horario.get('data_agenda') or horario.get('horario')
    if isinstance(valor, datetime):
        return valor.date()
    return valor


@router.post(
    '/jornada-integrada/sugestao',
    status_code=HTTPStatus.OK,
    response_model=JornadaIntegradaSugestao,
)
def sugerir_jornada_integrada(
    payload: JornadaIntegradaSugestaoInput,
    usuario_atual: ValidaUsuarioAtual,
    session: Session = Depends(get_session_oracle),
):
    if payload.cd_paciente is not None:
        garantir_paciente_autorizado(usuario_atual, payload.cd_paciente)
    itens_unicos = {}
    for item in payload.itens:
        itens_unicos[item.cd_item_agendamento] = item
    itens = list(itens_unicos.values())
    if len(itens) < 2:
        raise HTTPException(
            status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
            detail='Informe pelo menos dois itens diferentes para a jornada integrada.',
        )

    inicio = payload.data_inicio or date.today()
    fim = payload.data_fim or inicio + timedelta(days=payload.dias_busca)
    if fim < inicio:
        raise HTTPException(
            status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
            detail='data_fim deve ser igual ou posterior a data_inicio.',
        )
    if fim - inicio > timedelta(days=180):
        raise HTTPException(
            status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
            detail='O periodo maximo de busca inteligente e de 180 dias.',
        )

    try:
        dias_por_item_rows: dict[int, list[dict]] = {}
        for item in itens:
            dias_por_item_rows[item.cd_item_agendamento] = _consulta_dias_disponiveis_item(
                session=session,
                cd_item_agendamento=item.cd_item_agendamento,
                data_inicio=inicio,
                data_fim=fim,
                limite_dias=min(payload.dias_busca + 10, 200),
                cd_prestador=item.cd_prestador,
                cd_tip_mar=item.cd_tip_mar,
                cd_paciente=payload.cd_paciente,
                cd_convenio=payload.cd_convenio,
                cd_con_pla=payload.cd_con_pla,
            )
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=HTTPStatus.SERVICE_UNAVAILABLE,
            detail='Nao foi possivel analisar a jornada integrada no MV.',
        ) from exc

    nomes: dict[int, str] = {}
    datas_por_item: dict[int, set[date]] = {}
    primeiras_datas = []
    for item in itens:
        cd_item = item.cd_item_agendamento
        dias_rows = dias_por_item_rows.get(cd_item, [])
        nome = (
            dias_rows[0].get('ds_item_agendamento')
            if dias_rows
            else item.ds_item_agendamento
        ) or f'Item {cd_item}'
        nomes[cd_item] = nome
        datas = {data for data in (_data_horario(h) for h in dias_rows) if data}
        datas_por_item[cd_item] = datas
        primeira = min(datas) if datas else None
        primeiras_datas.append(
            {
                'cd_item_agendamento': cd_item,
                'ds_item_agendamento': nome,
                'primeira_data': primeira,
                'total_datas': len(datas),
                'status': 'com_vaga' if primeira else 'sem_vaga',
            }
        )

    todos_ids = [item.cd_item_agendamento for item in itens]
    todas_datas = sorted(set().union(*datas_por_item.values())) if datas_por_item else []
    candidatas = []
    for dia in todas_datas:
        disponiveis = [cd for cd in todos_ids if dia in datas_por_item.get(cd, set())]
        ausentes = [cd for cd in todos_ids if cd not in disponiveis]
        candidatas.append(
            {
                'data': dia,
                'total_itens': len(disponiveis),
                'itens_disponiveis': disponiveis,
                'itens_ausentes': ausentes,
            }
        )
    candidatas_ordenadas = sorted(
        candidatas,
        key=lambda c: (-c['total_itens'], c['data']),
    )
    datas_comuns = [c['data'] for c in candidatas_ordenadas if c['total_itens'] == len(todos_ids)]
    data_comum = min(datas_comuns) if datas_comuns else None
    melhor = (
        next((c for c in candidatas_ordenadas if c['data'] == data_comum), None)
        if data_comum
        else (candidatas_ordenadas[0] if candidatas_ordenadas else None)
    )
    itens_sem_vaga = [cd for cd in todos_ids if not datas_por_item.get(cd)]
    gargalos = []
    if melhor:
        gargalos = list(dict.fromkeys(melhor['itens_ausentes'] + itens_sem_vaga))
    elif itens_sem_vaga:
        gargalos = itens_sem_vaga

    if data_comum:
        mensagem = (
            f'Existe data comum para todos os {len(todos_ids)} itens: '
            f'{data_comum.strftime("%d/%m/%Y")}.'
        )
    elif melhor:
        ausentes = ', '.join(nomes.get(cd, str(cd)) for cd in gargalos) or 'nenhum'
        mensagem = (
            f'Nao existe data comum para todos os itens no periodo. '
            f'Melhor data: {melhor["data"].strftime("%d/%m/%Y")} '
            f'com {melhor["total_itens"]}/{len(todos_ids)} itens. '
            f'Gargalo: {ausentes}.'
        )
    else:
        mensagem = 'Nenhum horario livre foi encontrado para os itens no periodo.'

    return {
        'data_comum': data_comum,
        'melhor_data': melhor['data'] if melhor else None,
        'melhor_total_itens': melhor['total_itens'] if melhor else 0,
        'total_itens': len(todos_ids),
        'primeiras_datas': primeiras_datas,
        'candidatas': candidatas_ordenadas[:20],
        'itens_sem_vaga': itens_sem_vaga,
        'gargalos': gargalos,
        'mensagem': mensagem,
    }


def _resolver_cidade_cep(
    session: Session,
    *,
    codigo_ibge: str,
    cidade: str,
    uf: str,
) -> dict[str, int | str | None]:
    codigo = int(codigo_ibge) if codigo_ibge.isdigit() else None
    rows = (
        session
        .execute(
            text(
                '''
                SELECT c.CD_CIDADE AS cd_cidade,
                       c.NM_CIDADE AS nm_cidade,
                       c.CD_UF AS cd_uf,
                       CASE WHEN c.CD_IBGE = :codigo_ibge THEN 1 ELSE 0 END
                           AS exata_ibge
                  FROM DBAMV.CIDADE c
                 WHERE (:codigo_ibge IS NOT NULL
                        AND c.CD_IBGE = :codigo_ibge)
                    OR (UPPER(TRIM(c.NM_CIDADE)) = UPPER(TRIM(:cidade))
                        AND UPPER(TRIM(c.CD_UF)) = UPPER(TRIM(:uf)))
                 ORDER BY exata_ibge DESC, c.CD_CIDADE
                 FETCH FIRST 3 ROWS ONLY
                '''
            ),
            {'codigo_ibge': codigo, 'cidade': cidade, 'uf': uf},
        )
        .mappings()
        .all()
    )
    return resolver_cidade_rows([dict(row) for row in rows])


@router.get(
    '/enderecos/cep/{cep}',
    status_code=HTTPStatus.OK,
    response_model=EnderecoCep,
)
def consultar_endereco_por_cep(
    cep: str,
    usuario_atual: ValidaUsuarioAtual,
    session: Session = Depends(get_session_oracle),
):
    del usuario_atual
    try:
        endereco = consultar_viacep(cep)
    except CepErro as exc:
        status = (
            HTTPStatus.SERVICE_UNAVAILABLE
            if 'indisponível' in str(exc)
            else HTTPStatus.UNPROCESSABLE_ENTITY
        )
        raise HTTPException(status_code=status, detail=str(exc)) from exc
    try:
        cidade_mv = _resolver_cidade_cep(
            session,
            codigo_ibge=endereco['codigo_ibge'],
            cidade=endereco['cidade'],
            uf=endereco['uf'],
        )
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=HTTPStatus.SERVICE_UNAVAILABLE,
            detail='Não foi possível localizar a cidade no MV.',
        ) from exc
    return {**endereco, **cidade_mv}


@router.get(
    '/planos-ativos',
    status_code=HTTPStatus.OK,
    response_model=PlanosDisponiveis,
)
def consultar_planos_ativos(
    usuario_atual: ValidaUsuarioAtual,
    session: Session = Depends(get_session_oracle),
):
    del usuario_atual
    try:
        rows = session.execute(CONSULTA_PLANOS_ATIVOS).mappings().all()
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=HTTPStatus.SERVICE_UNAVAILABLE,
            detail='Nao foi possivel consultar os planos no MV.',
        ) from exc
    return {'planos': [dict(row) for row in rows], 'total': len(rows)}


@router.get('/ranking-operadores', status_code=HTTPStatus.OK)
def consultar_ranking_operadores(
    usuario_atual: ValidaUsuarioAtual,
    data_inicio: Annotated[date | None, Query()] = None,
    data_fim: Annotated[date | None, Query()] = None,
    session: Session = Depends(get_session_oracle),
):
    """Ranking interno de itens agendados pelo Meu ProntoCardio."""
    bloquear_operacao_interna_para_paciente(usuario_atual)
    hoje = date.today()
    inicio = data_inicio or hoje.replace(day=1)
    fim = data_fim or hoje
    if fim < inicio:
        raise HTTPException(
            status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
            detail='A data final deve ser igual ou posterior a data inicial.',
        )
    if (fim - inicio).days > 366:
        raise HTTPException(
            status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
            detail='O periodo do ranking deve ter no maximo 367 dias.',
        )
    try:
        rows = (
            session
            .execute(
                CONSULTA_RANKING_OPERADORES,
                {
                    'data_inicio': inicio,
                    'data_fim_exclusiva': fim + timedelta(days=1),
                },
            )
            .mappings()
            .all()
        )
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=HTTPStatus.SERVICE_UNAVAILABLE,
            detail='Nao foi possivel consultar o ranking no MV.',
        ) from exc

    ranking = []
    for posicao, row in enumerate(rows, 1):
        registro = dict(row)
        registro['posicao'] = posicao
        ranking.append(registro)
    return {
        'data_inicio': inicio,
        'data_fim': fim,
        'ranking': ranking,
        'total_operadores': len(ranking),
        'total_itens': sum(item['itens_agendados'] for item in ranking),
    }


@router.get(
    '/tipos-consulta',
    status_code=HTTPStatus.OK,
    response_model=TiposMarcacaoConsulta,
)
def consultar_tipos_consulta(
    usuario_atual: ValidaUsuarioAtual,
    session: Session = Depends(get_session_oracle),
):
    """Retorna os tipos de marcação de consulta cadastrados no MV."""
    del usuario_atual
    try:
        rows = session.execute(CONSULTA_TIPOS_MARCACAO_CONSULTA).mappings().all()
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=HTTPStatus.SERVICE_UNAVAILABLE,
            detail='Nao foi possivel consultar os tipos de consulta no MV.',
        ) from exc
    return {'tipos': [dict(row) for row in rows], 'total': len(rows)}


@router.get(
    '/pacientes',
    status_code=HTTPStatus.OK,
    response_model=PacientesEncontrados,
)
def consultar_pacientes(
    usuario_atual: ValidaUsuarioAtual,
    termo: Annotated[str, Query(min_length=3, max_length=80)],
    limite: Annotated[int, Query(ge=1, le=20)] = 10,
    session: Session = Depends(get_session_oracle),
):
    bloquear_operacao_interna_para_paciente(usuario_atual)
    termo_limpo = termo.strip()
    digitos = ''.join(
        caractere for caractere in termo_limpo if caractere.isdigit()
    )
    codigo = int(termo_limpo) if termo_limpo.isdigit() else -1
    cpf = digitos if len(digitos) == TAMANHO_CPF else '-1'

    try:
        rows = (
            session
            .execute(
                CONSULTA_PACIENTES,
                {
                    'codigo': codigo,
                    'cpf': cpf,
                    'nome': f'%{termo_limpo.upper()}%',
                    'limite': limite,
                },
            )
            .mappings()
            .all()
        )
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=HTTPStatus.SERVICE_UNAVAILABLE,
            detail='Nao foi possivel consultar os pacientes no MV.',
        ) from exc

    pacientes = []
    for row in rows:
        paciente = dict(row)
        nascimento = paciente.get('dt_nascimento')
        if isinstance(nascimento, datetime):
            paciente['dt_nascimento'] = nascimento.date()
        paciente['cpf_final'] = _cpf_final(paciente.pop('nr_cpf', None))
        pacientes.append(paciente)
    return {'pacientes': pacientes, 'total': len(pacientes)}


def _validar_periodo_pacientes_lote(
    data_inicio: date,
    data_fim: date,
) -> None:
    dias = (data_fim - data_inicio).days
    if dias < 0:
        raise HTTPException(
            status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
            detail='data_fim deve ser igual ou posterior a data_inicio.',
        )
    if dias >= LIMITE_DIAS_PACIENTES_LOTE:
        raise HTTPException(
            status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
            detail='O periodo maximo permitido e de 31 dias corridos.',
        )


def _possui_telefone_paciente_lote(paciente: dict) -> bool:
    return any(
        str(paciente.get(campo) or '').strip()
        for campo in ('nr_celular', 'nr_fone', 'nr_fone_comercial')
    )


def _consultar_pacientes_lote(
    *,
    usuario_atual: Usuario | PrincipalPaciente,
    session: Session,
    consulta: TextClause,
    parametros: dict,
    tipo: str,
    inicio_auditoria: str | int,
    fim_auditoria: str | int,
    limite: int,
    cursor: int,
) -> dict:
    exigir_permissao_pacientes_lote(usuario_atual)
    parametros_consulta = {
        **parametros,
        'cursor': cursor,
        'limite_consulta': limite + 1,
    }
    try:
        rows = session.execute(
            consulta,
            parametros_consulta,
        ).mappings().all()
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=HTTPStatus.SERVICE_UNAVAILABLE,
            detail='Nao foi possivel consultar os pacientes em lote no MV.',
        ) from exc

    pacientes_unicos: dict[int, dict] = {}
    campos_publicos = (
        'cd_paciente',
        'nm_paciente',
        'nr_ddd_celular',
        'nr_celular',
        'nr_ddd_fone',
        'nr_fone',
        'nr_ddd_fone_comercial',
        'nr_fone_comercial',
    )
    for row in rows:
        paciente = dict(row)
        if not _possui_telefone_paciente_lote(paciente):
            continue
        codigo = int(paciente['cd_paciente'])
        if codigo not in pacientes_unicos:
            pacientes_unicos[codigo] = {
                campo: paciente.get(campo) for campo in campos_publicos
            }

    candidatos = list(pacientes_unicos.values())
    pacientes = candidatos[:limite]
    proximo_cursor = (
        int(pacientes[-1]['cd_paciente'])
        if len(candidatos) > limite and pacientes
        else None
    )
    identificador_usuario = (
        getattr(usuario_atual, 'email', None)
        or getattr(usuario_atual, 'nome', None)
        or 'desconhecido'
    )
    logger.info(
        'pacientes_lote usuario=%s tipo=%s inicio=%s fim=%s total=%d',
        identificador_usuario,
        tipo,
        inicio_auditoria,
        fim_auditoria,
        len(pacientes),
    )
    return {
        'pacientes': pacientes,
        'total': len(pacientes),
        'proximo_cursor': proximo_cursor,
    }


@router.get(
    '/pacientes/lote/codigos',
    status_code=HTTPStatus.OK,
    response_model=PacientesLoteResultado,
)
def consultar_pacientes_lote_por_codigos(
    usuario_atual: ValidaUsuarioAtual,
    codigo_inicio: Annotated[int, Query(ge=1)],
    codigo_fim: Annotated[int, Query(ge=1)],
    limite: Annotated[int, Query(ge=1, le=200)] = 100,
    cursor: Annotated[int, Query(ge=0)] = 0,
    session: Session = Depends(get_session_oracle),
):
    quantidade = codigo_fim - codigo_inicio + 1
    if quantidade <= 0:
        raise HTTPException(
            status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
            detail='codigo_fim deve ser igual ou maior que codigo_inicio.',
        )
    if quantidade > LIMITE_FAIXA_CODIGOS_PACIENTES:
        raise HTTPException(
            status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
            detail='A faixa maxima permitida e de 5.000 codigos.',
        )
    return _consultar_pacientes_lote(
        usuario_atual=usuario_atual,
        session=session,
        consulta=CONSULTA_PACIENTES_LOTE_CODIGOS,
        parametros={
            'codigo_inicio': codigo_inicio,
            'codigo_fim': codigo_fim,
        },
        tipo='codigos',
        inicio_auditoria=codigo_inicio,
        fim_auditoria=codigo_fim,
        limite=limite,
        cursor=cursor,
    )


def _consultar_pacientes_lote_por_periodo(
    *,
    usuario_atual: Usuario | PrincipalPaciente,
    session: Session,
    consulta: TextClause,
    tipo: str,
    data_inicio: date,
    data_fim: date,
    limite: int,
    cursor: int,
) -> dict:
    _validar_periodo_pacientes_lote(data_inicio, data_fim)
    return _consultar_pacientes_lote(
        usuario_atual=usuario_atual,
        session=session,
        consulta=consulta,
        parametros={
            'data_inicio': datetime.combine(data_inicio, datetime.min.time()),
            'data_fim_exclusiva': datetime.combine(
                data_fim + timedelta(days=1), datetime.min.time()
            ),
        },
        tipo=tipo,
        inicio_auditoria=data_inicio.isoformat(),
        fim_auditoria=data_fim.isoformat(),
        limite=limite,
        cursor=cursor,
    )


@router.get(
    '/pacientes/lote/cadastros',
    status_code=HTTPStatus.OK,
    response_model=PacientesLoteResultado,
)
def consultar_pacientes_lote_por_cadastros(
    usuario_atual: ValidaUsuarioAtual,
    data_inicio: date,
    data_fim: date,
    limite: Annotated[int, Query(ge=1, le=200)] = 100,
    cursor: Annotated[int, Query(ge=0)] = 0,
    session: Session = Depends(get_session_oracle),
):
    return _consultar_pacientes_lote_por_periodo(
        usuario_atual=usuario_atual,
        session=session,
        consulta=CONSULTA_PACIENTES_LOTE_CADASTROS,
        tipo='cadastros',
        data_inicio=data_inicio,
        data_fim=data_fim,
        limite=limite,
        cursor=cursor,
    )


@router.get(
    '/pacientes/lote/agendamentos',
    status_code=HTTPStatus.OK,
    response_model=PacientesLoteResultado,
)
def consultar_pacientes_lote_por_agendamentos(
    usuario_atual: ValidaUsuarioAtual,
    data_inicio: date,
    data_fim: date,
    limite: Annotated[int, Query(ge=1, le=200)] = 100,
    cursor: Annotated[int, Query(ge=0)] = 0,
    session: Session = Depends(get_session_oracle),
):
    return _consultar_pacientes_lote_por_periodo(
        usuario_atual=usuario_atual,
        session=session,
        consulta=CONSULTA_PACIENTES_LOTE_AGENDAMENTOS,
        tipo='agendamentos',
        data_inicio=data_inicio,
        data_fim=data_fim,
        limite=limite,
        cursor=cursor,
    )


@router.post(
    '/pacientes/cadastrar',
    status_code=HTTPStatus.CREATED,
    response_model=PacienteCadastrado,
)
def cadastrar_paciente(
    payload: CadastroPacienteInput,
    usuario_atual: ValidaUsuarioAtual,
    session: Session = Depends(get_session_oracle),
):
    """Cadastra um paciente novo e seus dados de contato/endereco no MV.

    A rota fica separada da busca para que o CPF seja revalidado dentro da
    mesma transacao antes do INSERT. A habilitacao deve ser explicita no
    ambiente (`MV_CADASTRO_PACIENTE_HABILITADO=true`).
    """
    bloquear_operacao_interna_para_paciente(usuario_atual)
    if os.getenv('MV_CADASTRO_PACIENTE_HABILITADO', 'false').lower() != 'true':
        raise HTTPException(
            status_code=HTTPStatus.NOT_IMPLEMENTED,
            detail='Cadastro de paciente desabilitado neste ambiente.',
        )
    cpf = _somente_digitos(payload.nr_cpf) or ''
    if len(cpf) != TAMANHO_CPF:
        raise HTTPException(
            status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
            detail='CPF deve conter 11 digitos.',
        )
    if not _cpf_valido(cpf):
        raise HTTPException(
            status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
            detail='CPF invalido. Confira os numeros antes de cadastrar.',
        )
    cep = _somente_digitos(payload.nr_cep)
    if cep and len(cep) != 8:
        raise HTTPException(
            status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
            detail='CEP deve conter 8 digitos.',
        )
    ddi_celular = _somente_digitos(payload.nr_ddi_celular)
    ddd_celular = _somente_digitos(payload.nr_ddd_celular)
    celular = _somente_digitos(payload.nr_celular)
    ddi_fone = _somente_digitos(payload.nr_ddi_fone)
    ddd_fone = _somente_digitos(payload.nr_ddd_fone)
    fone = _somente_digitos(payload.nr_fone)
    ddi_comercial = _somente_digitos(payload.nr_ddi_fone_comercial)
    ddd_comercial = _somente_digitos(payload.nr_ddd_fone_comercial)
    fone_comercial = _somente_digitos(payload.nr_fone_comercial)
    for numero, ddi, ddd, descricao in (
        (celular, ddi_celular, ddd_celular, 'celular'),
        (fone, ddi_fone, ddd_fone, 'telefone'),
        (fone_comercial, ddi_comercial, ddd_comercial, 'telefone comercial'),
    ):
        if numero and not ddi:
            raise HTTPException(
                status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
                detail=f'Informe o DDI do {descricao}.',
            )
        if numero and not ddd:
            raise HTTPException(
                status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
                detail=f'Informe o DDD do {descricao}.',
            )
    nr_carteira = ' '.join(str(payload.nr_carteira or '').strip().split()) or None
    email = payload.email.strip().lower() if payload.email else None
    nome_paciente = _texto_maiusculo(payload.nm_paciente, 200)
    ds_endereco = _texto_maiusculo(payload.ds_endereco, 200)
    ds_complemento = _texto_maiusculo(payload.ds_complemento, 100)
    nm_bairro = _texto_maiusculo(payload.nm_bairro, 100)
    connection = session.connection().connection
    cursor = connection.cursor()
    try:
        # O MV mantém a empresa em contexto de sessão; sem isso o trigger de
        # integração do cadastro não encontra CONFIG_MVINTEGRA.
        cursor.callproc('DBAMV.PKG_MV2000.ATRIBUI_EMPRESA', [1])
        existente = cursor.execute(
            """
            SELECT CD_PACIENTE, NM_PACIENTE
              FROM DBAMV.PACIENTE
             WHERE REGEXP_REPLACE(NR_CPF, '[^0-9]', '') = :cpf
             FETCH FIRST 1 ROW ONLY
            """,
            {'cpf': cpf},
        ).fetchone()
        plano = cursor.execute(
            """
            SELECT 1
              FROM DBAMV.CONVENIO c
              JOIN DBAMV.CON_PLA cp ON cp.CD_CONVENIO = c.CD_CONVENIO
             WHERE c.CD_CONVENIO = :cd_convenio
               AND cp.CD_CON_PLA = :cd_con_pla
               AND NVL(c.SN_ATIVO, 'S') = 'S'
               AND NVL(cp.SN_ATIVO, 'S') = 'S'
            """,
            {
                'cd_convenio': payload.cd_convenio,
                'cd_con_pla': payload.cd_con_pla,
            },
        ).fetchone()
        if not plano:
            raise HTTPException(
                status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
                detail='Convenio/plano invalido ou inativo no MV.',
            )
        if existente:
            paciente_id = int(existente[0])
            nome_existente = (
                str(existente[1] or payload.nm_paciente).strip().upper()
            )
            carteira = cursor.execute(
                """
                SELECT 1
                  FROM DBAMV.CARTEIRA
                 WHERE CD_PACIENTE = :paciente
                   AND CD_CONVENIO = :cd_convenio
                   AND CD_CON_PLA = :cd_con_pla
                   AND NVL(SN_CARTEIRA_ATIVO, 'S') = 'S'
                 FETCH FIRST 1 ROW ONLY
                """,
                {
                    'paciente': paciente_id,
                    'cd_convenio': payload.cd_convenio,
                    'cd_con_pla': payload.cd_con_pla,
                },
            ).fetchone()
            if not carteira:
                cursor.execute(
                    """
                    INSERT INTO DBAMV.CARTEIRA (
                        CD_CONVENIO, CD_PACIENTE, CD_CON_PLA, NR_CARTEIRA,
                        NM_TITULAR, SN_TITULAR, SN_CARTEIRA_ATIVO
                    ) VALUES (
                        :cd_convenio, :paciente, :cd_con_pla, :nr_carteira,
                        :nm_titular, 'S', 'S'
                    )
                    """,
                    {
                        'cd_convenio': payload.cd_convenio,
                        'paciente': paciente_id,
                        'cd_con_pla': payload.cd_con_pla,
                        'nr_carteira': nr_carteira,
                        'nm_titular': nome_existente,
                    },
                )
            connection.commit()
            return {
                'cd_paciente': paciente_id,
                'nm_paciente': nome_existente,
                'nr_cpf': cpf,
                'mensagem': (
                    'Paciente ja existia no MV. Convenio/plano garantido '
                    'para seguir o agendamento.'
                ),
            }
        operador = getattr(usuario_atual, 'nome', None) or 'API_PRONTOCARDIO'
        cursor.execute(
            """
            INSERT INTO DBAMV.PACIENTE (
                CD_PACIENTE, TP_SITUACAO, SN_ALT_DADOS_ORA_APP,
                SN_RECEBE_CONTATO, SN_VIP, SN_NOTIFICACAO_SMS,
                DT_CADASTRO, DT_CADASTRO_MANUAL, SN_ENDERECO_SEM_NUMERO,
                SN_RUT_FICTICIO, SN_ONCOLOGICO, NM_PACIENTE, NR_CPF,
                DT_NASCIMENTO, TP_SEXO, EMAIL,
                NR_DDI_CELULAR, NR_DDD_CELULAR, NR_CELULAR,
                NR_DDI_FONE, NR_DDD_FONE, NR_FONE,
                NR_DDI_FONE_COMERCIAL, NR_DDD_FONE_COMERCIAL,
                NR_FONE_COMERCIAL,
                DS_ENDERECO, NR_ENDERECO, DS_COMPLEMENTO, NM_BAIRRO,
                NR_CEP, CD_CIDADE, NM_USUARIO, CD_MULTI_EMPRESA
            ) VALUES (
                SEQ_PACIENTE.NEXTVAL, 'N', 'S', 'N', 'N', 'N', SYSDATE,
                SYSDATE, 'N', 'N', 'N', :nm_paciente, :cpf,
                TO_DATE(:dt_nascimento, 'YYYY-MM-DD'), :tp_sexo, :email,
                :nr_ddi_celular, :nr_ddd_celular, :nr_celular,
                :nr_ddi_fone, :nr_ddd_fone, :nr_fone,
                :nr_ddi_fone_comercial, :nr_ddd_fone_comercial,
                :nr_fone_comercial, :ds_endereco, :nr_endereco,
                :ds_complemento, :nm_bairro, :nr_cep, :cd_cidade,
                :nm_usuario, 1
            )
            """,
            {
                'nm_paciente': nome_paciente,
                'cpf': cpf,
                'dt_nascimento': payload.dt_nascimento.isoformat(),
                'tp_sexo': payload.tp_sexo,
                'email': email,
                **binds_contato_paciente(
                    ddi_celular=ddi_celular,
                    ddd_celular=ddd_celular,
                    celular=celular,
                    ddi_fone=ddi_fone,
                    ddd_fone=ddd_fone,
                    fone=fone,
                    ddi_comercial=ddi_comercial,
                    ddd_comercial=ddd_comercial,
                    fone_comercial=fone_comercial,
                ),
                'ds_endereco': ds_endereco,
                'nr_endereco': payload.nr_endereco,
                'ds_complemento': ds_complemento,
                'nm_bairro': nm_bairro,
                'nr_cep': cep,
                'cd_cidade': payload.cd_cidade,
                'nm_usuario': operador[:30],
            },
        )
        paciente_id = cursor.execute(
            'SELECT SEQ_PACIENTE.CURRVAL FROM DUAL'
        ).fetchone()[0]
        if ds_endereco or cep or payload.cd_cidade:
            cursor.execute(
                """
                INSERT INTO DBAMV.ENDERECO_PACIENTE (
                    CD_ENDERECO_PACIENTE, CD_PACIENTE, NR_CEP,
                    DS_ENDERECO, NR_ENDERECO, DS_COMPLEMENTO, NM_BAIRRO,
                    CD_CIDADE, TP_ENDERECO, SN_PADRAO, SN_ENDERECO_EXTERNO
                ) VALUES (
                    SEQ_ENDERECO_PACIENTE.NEXTVAL, :paciente, :nr_cep,
                    :ds_endereco, :nr_endereco, :ds_complemento, :nm_bairro,
                    :cd_cidade, 'R', 'S', 'N'
                )
                """,
                {
                    'paciente': paciente_id,
                    'nr_cep': cep,
                    'ds_endereco': ds_endereco,
                    'nr_endereco': payload.nr_endereco,
                    'ds_complemento': ds_complemento,
                    'nm_bairro': nm_bairro,
                    'cd_cidade': payload.cd_cidade,
                },
            )
            cursor.execute(
                """
                INSERT INTO DBAMV.ENDERECO (
                    CD_ENDERECO, CD_PACIENTE, DS_ENDERECO, NR_ENDERECO,
                    NR_FONE, DS_COMPLEMENTO, NM_BAIRRO, NR_CEP, SN_PADRAO
                ) VALUES (
                    SEQ_ENDERECO.NEXTVAL, :paciente, :ds_endereco,
                    :nr_endereco, :nr_celular, :ds_complemento, :nm_bairro,
                    :nr_cep, 'S'
                )
                """,
                {
                    'paciente': paciente_id,
                    'nr_cep': cep,
                    'ds_endereco': ds_endereco,
                    'nr_endereco': payload.nr_endereco,
                    'ds_complemento': ds_complemento,
                    'nm_bairro': nm_bairro,
                    'nr_celular': celular,
                },
            )
        if celular:
            cursor.execute(
                """
                INSERT INTO DBAMV.CONTATO_PACIENTE (
                    CD_CONTATO_PACIENTE, CD_PACIENTE, NR_DDD, NR_TELEFONE,
                    DS_TIP_COMUN, TP_CONTATO, SN_PADRAO, SN_SMS
                ) VALUES (
                    SEQ_CONTATO_PACIENTE.NEXTVAL, :paciente, :ddd, :telefone,
                    'CELULAR', 'C', 'S', 'N'
                )
                """,
                {
                    'paciente': paciente_id,
                    'ddd': ddd_celular,
                    'telefone': celular,
                },
            )
        if email:
            cursor.execute(
                """
                INSERT INTO DBAMV.OUTROS_CONTATOS_PACIENTE (
                    CD_OUTROS_CONTATOS_PACIENTE, CD_PACIENTE,
                    TP_OUTROS_CONTATO, CONTATO, SN_PADRAO,
                    SN_RECEBE_CONTATO, DS_TIP_COMUN
                ) VALUES (
                    SEQ_OUTROS_CONTATOS_PACIENTE.NEXTVAL, :paciente,
                    'E', :email, 'S', 'N', 'E-MAIL'
                )
                """,
                {'paciente': paciente_id, 'email': email},
            )
        cursor.execute(
            """
            INSERT INTO DBAMV.CARTEIRA (
                CD_CONVENIO, CD_PACIENTE, CD_CON_PLA, NR_CARTEIRA,
                NM_TITULAR, SN_TITULAR, SN_CARTEIRA_ATIVO
            ) VALUES (
                :cd_convenio, :paciente, :cd_con_pla, :nr_carteira,
                :nm_titular, 'S', 'S'
            )
            """,
            {
                'cd_convenio': payload.cd_convenio,
                'paciente': paciente_id,
                'cd_con_pla': payload.cd_con_pla,
                'nr_carteira': nr_carteira,
                'nm_titular': nome_paciente,
            },
        )
        connection.commit()
    except HTTPException:
        connection.rollback()
        raise
    except Exception as exc:
        connection.rollback()
        logger.exception('Falha ao cadastrar paciente no MV')
        detalhe = _erro_oracle_resumido(exc)
        raise HTTPException(
            status_code=HTTPStatus.CONFLICT,
            detail=f'Nao foi possivel cadastrar o paciente no MV. Detalhe: {detalhe}',
        ) from exc
    finally:
        cursor.close()
    return {
        'cd_paciente': int(paciente_id),
        'nm_paciente': nome_paciente,
        'nr_cpf': cpf,
        'mensagem': 'Paciente cadastrado com sucesso no MV.',
    }


@router.put(
    '/pacientes/{cd_paciente}',
    status_code=HTTPStatus.OK,
    response_model=PacienteAtualizado,
)
def atualizar_paciente(
    cd_paciente: int,
    payload: AtualizacaoPacienteInput,
    usuario_atual: ValidaUsuarioAtual,
    session: Session = Depends(get_session_oracle),
):
    """Atualiza dados cadastrais simples do paciente antes do agendamento."""
    garantir_paciente_autorizado(usuario_atual, cd_paciente)
    if os.getenv('MV_GRAVACAO_HABILITADA', 'false').lower() != 'true':
        raise HTTPException(
            status_code=HTTPStatus.NOT_IMPLEMENTED,
            detail='Atualizacao de paciente desabilitada neste ambiente.',
        )
    if cd_paciente <= 0:
        raise HTTPException(
            status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
            detail='Codigo do paciente invalido.',
        )
    if (payload.cd_convenio is None) != (payload.cd_con_pla is None):
        raise HTTPException(
            status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
            detail='Informe convenio e plano juntos.',
        )
    ddi_celular = _somente_digitos(payload.nr_ddi_celular)
    ddd_celular = _somente_digitos(payload.nr_ddd_celular)
    celular = _somente_digitos(payload.nr_celular)
    ddi_fone = _somente_digitos(payload.nr_ddi_fone)
    ddd_fone = _somente_digitos(payload.nr_ddd_fone)
    fone = _somente_digitos(payload.nr_fone)
    ddi_comercial = _somente_digitos(payload.nr_ddi_fone_comercial)
    ddd_comercial = _somente_digitos(payload.nr_ddd_fone_comercial)
    fone_comercial = _somente_digitos(payload.nr_fone_comercial)
    campos_endereco = {
        'nr_cep', 'ds_endereco', 'nr_endereco', 'ds_complemento',
        'nm_bairro', 'cd_cidade', 'nm_cidade', 'cd_uf',
    }
    atualizar_endereco = bool(payload.model_fields_set & campos_endereco)
    cep = _somente_digitos(payload.nr_cep)
    if cep and len(cep) != 8:
        raise HTTPException(
            status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
            detail='CEP deve conter 8 digitos.',
        )
    if atualizar_endereco and payload.cd_cidade is None:
        raise HTTPException(
            status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
            detail='Consulte o CEP e confirme uma cidade valida do MV.',
        )
    ds_endereco = _texto_maiusculo(payload.ds_endereco, 200)
    ds_complemento = _texto_maiusculo(payload.ds_complemento, 100)
    nm_bairro = _texto_maiusculo(payload.nm_bairro, 100)
    for numero, ddi, ddd, descricao in (
        (celular, ddi_celular, ddd_celular, 'celular'),
        (fone, ddi_fone, ddd_fone, 'telefone'),
        (fone_comercial, ddi_comercial, ddd_comercial, 'telefone comercial'),
    ):
        if numero and not ddi:
            raise HTTPException(
                status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
                detail=f'Informe o DDI do {descricao}.',
            )
        if numero and not ddd:
            raise HTTPException(
                status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
                detail=f'Informe o DDD do {descricao}.',
            )
    connection = session.connection().connection
    cursor = connection.cursor()
    etapa_atualizacao = 'inicialização'
    try:
        etapa_atualizacao = 'contexto do MV'
        cursor.callproc('DBAMV.PKG_MV2000.ATRIBUI_EMPRESA', [1])
        etapa_atualizacao = 'localização do paciente'
        paciente = cursor.execute(
            """
            SELECT NM_PACIENTE
              FROM DBAMV.PACIENTE
             WHERE CD_PACIENTE = :cd_paciente
            """,
            {'cd_paciente': cd_paciente},
        ).fetchone()
        if not paciente:
            raise HTTPException(
                status_code=HTTPStatus.NOT_FOUND,
                detail='Paciente nao encontrado no MV.',
            )

        etapa_atualizacao = 'dados cadastrais'
        cursor.execute(
            """
            UPDATE DBAMV.PACIENTE
               SET EMAIL = :email,
                   NR_DDI_CELULAR = :nr_ddi_celular,
                   NR_DDD_CELULAR = :nr_ddd_celular,
                   NR_CELULAR = :nr_celular,
                   NR_DDI_FONE = :nr_ddi_fone,
                   NR_DDD_FONE = :nr_ddd_fone,
                   NR_FONE = :nr_fone,
                   NR_DDI_FONE_COMERCIAL = :nr_ddi_fone_comercial,
                   NR_DDD_FONE_COMERCIAL = :nr_ddd_fone_comercial,
                   NR_FONE_COMERCIAL = :nr_fone_comercial,
                   NR_CEP = CASE WHEN :atualizar_endereco = 1
                                 THEN :nr_cep ELSE NR_CEP END,
                   DS_ENDERECO = CASE WHEN :atualizar_endereco = 1
                                      THEN :ds_endereco ELSE DS_ENDERECO END,
                   NR_ENDERECO = CASE WHEN :atualizar_endereco = 1
                                      THEN CAST(:nr_endereco AS NUMBER)
                                      ELSE NR_ENDERECO END,
                   DS_COMPLEMENTO = CASE WHEN :atualizar_endereco = 1
                                         THEN :ds_complemento
                                         ELSE DS_COMPLEMENTO END,
                   NM_BAIRRO = CASE WHEN :atualizar_endereco = 1
                                    THEN :nm_bairro ELSE NM_BAIRRO END,
                   CD_CIDADE = CASE WHEN :atualizar_endereco = 1
                                    THEN CAST(:cd_cidade AS NUMBER)
                                    ELSE CD_CIDADE END,
                   SN_ALT_DADOS_ORA_APP = 'S'
             WHERE CD_PACIENTE = :cd_paciente
            """,
            {
                'email': payload.email,
                **binds_contato_paciente(
                    ddi_celular=ddi_celular,
                    ddd_celular=ddd_celular,
                    celular=celular,
                    ddi_fone=ddi_fone,
                    ddd_fone=ddd_fone,
                    fone=fone,
                    ddi_comercial=ddi_comercial,
                    ddd_comercial=ddd_comercial,
                    fone_comercial=fone_comercial,
                ),
                'atualizar_endereco': 1 if atualizar_endereco else 0,
                'nr_cep': cep,
                'ds_endereco': ds_endereco,
                'nr_endereco': payload.nr_endereco,
                'ds_complemento': ds_complemento,
                'nm_bairro': nm_bairro,
                'cd_cidade': payload.cd_cidade,
                'cd_paciente': cd_paciente,
            },
        )

        if atualizar_endereco:
            etapa_atualizacao = 'endereço'
            cidade = cursor.execute(
                """
                SELECT 1
                  FROM DBAMV.CIDADE
                 WHERE CD_CIDADE = :cd_cidade
                """,
                {'cd_cidade': payload.cd_cidade},
            ).fetchone()
            if not cidade:
                raise HTTPException(
                    status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
                    detail='Cidade nao encontrada no MV.',
                )
            endereco_paciente = cursor.execute(
                """
                SELECT CD_ENDERECO_PACIENTE
                  FROM DBAMV.ENDERECO_PACIENTE
                 WHERE CD_PACIENTE = :cd_paciente
                 ORDER BY NVL(SN_PADRAO, 'N') DESC,
                          CD_ENDERECO_PACIENTE DESC
                 FETCH FIRST 1 ROW ONLY
                """,
                {'cd_paciente': cd_paciente},
            ).fetchone()
            parametros_endereco = {
                'cd_paciente': cd_paciente,
                'nr_cep': cep,
                'ds_endereco': ds_endereco,
                'nr_endereco': payload.nr_endereco,
                'ds_complemento': ds_complemento,
                'nm_bairro': nm_bairro,
                'cd_cidade': payload.cd_cidade,
            }
            if endereco_paciente:
                cursor.execute(
                    """
                    UPDATE DBAMV.ENDERECO_PACIENTE
                       SET NR_CEP = :nr_cep,
                           DS_ENDERECO = :ds_endereco,
                           NR_ENDERECO = :nr_endereco,
                           DS_COMPLEMENTO = :ds_complemento,
                           NM_BAIRRO = :nm_bairro,
                           CD_CIDADE = :cd_cidade,
                           TP_ENDERECO = 'R',
                           SN_PADRAO = 'S'
                     WHERE CD_ENDERECO_PACIENTE = :cd_endereco
                    """,
                    {
                        **parametros_endereco,
                        'cd_endereco': endereco_paciente[0],
                    },
                )
            else:
                cursor.execute(
                    """
                    INSERT INTO DBAMV.ENDERECO_PACIENTE (
                        CD_ENDERECO_PACIENTE, CD_PACIENTE, NR_CEP,
                        DS_ENDERECO, NR_ENDERECO, DS_COMPLEMENTO, NM_BAIRRO,
                        CD_CIDADE, TP_ENDERECO, SN_PADRAO, SN_ENDERECO_EXTERNO
                    ) VALUES (
                        SEQ_ENDERECO_PACIENTE.NEXTVAL, :cd_paciente, :nr_cep,
                        :ds_endereco, :nr_endereco, :ds_complemento, :nm_bairro,
                        :cd_cidade, 'R', 'S', 'N'
                    )
                    """,
                    parametros_endereco,
                )
            endereco_legado = cursor.execute(
                """
                SELECT CD_ENDERECO
                  FROM DBAMV.ENDERECO
                 WHERE CD_PACIENTE = :cd_paciente
                 ORDER BY NVL(SN_PADRAO, 'N') DESC, CD_ENDERECO DESC
                 FETCH FIRST 1 ROW ONLY
                """,
                {'cd_paciente': cd_paciente},
            ).fetchone()
            parametros_legado = {
                **parametros_endereco,
                'nr_fone': celular,
            }
            if endereco_legado:
                cursor.execute(
                    """
                    UPDATE DBAMV.ENDERECO
                       SET DS_ENDERECO = :ds_endereco,
                           NR_ENDERECO = :nr_endereco,
                           NR_FONE = :nr_fone,
                           DS_COMPLEMENTO = :ds_complemento,
                           NM_BAIRRO = :nm_bairro,
                           NR_CEP = :nr_cep,
                           SN_PADRAO = 'S'
                     WHERE CD_ENDERECO = :cd_endereco
                    """,
                    {**parametros_legado, 'cd_endereco': endereco_legado[0]},
                )
            else:
                cursor.execute(
                    """
                    INSERT INTO DBAMV.ENDERECO (
                        CD_ENDERECO, CD_PACIENTE, DS_ENDERECO, NR_ENDERECO,
                        NR_FONE, DS_COMPLEMENTO, NM_BAIRRO, NR_CEP, SN_PADRAO
                    ) VALUES (
                        SEQ_ENDERECO.NEXTVAL, :cd_paciente, :ds_endereco,
                        :nr_endereco, :nr_fone, :ds_complemento, :nm_bairro,
                        :nr_cep, 'S'
                    )
                    """,
                    parametros_legado,
                )

        if celular:
            etapa_atualizacao = 'contato celular'
            contato = cursor.execute(
                """
                SELECT CD_CONTATO_PACIENTE
                  FROM DBAMV.CONTATO_PACIENTE
                 WHERE CD_PACIENTE = :cd_paciente
                   AND TP_CONTATO = 'C'
                 ORDER BY NVL(SN_PADRAO, 'N') DESC, CD_CONTATO_PACIENTE DESC
                 FETCH FIRST 1 ROW ONLY
                """,
                {'cd_paciente': cd_paciente},
            ).fetchone()
            if contato:
                cursor.execute(
                    """
                    UPDATE DBAMV.CONTATO_PACIENTE
                       SET NR_DDD = :ddd,
                           NR_TELEFONE = :telefone,
                           DS_TIP_COMUN = 'CELULAR',
                           SN_PADRAO = 'S'
                     WHERE CD_CONTATO_PACIENTE = :cd_contato
                    """,
                    {
                        'ddd': ddd_celular,
                        'telefone': celular,
                        'cd_contato': contato[0],
                    },
                )
            else:
                cursor.execute(
                    """
                    INSERT INTO DBAMV.CONTATO_PACIENTE (
                        CD_CONTATO_PACIENTE, CD_PACIENTE, NR_DDD, NR_TELEFONE,
                        DS_TIP_COMUN, TP_CONTATO, SN_PADRAO, SN_SMS
                    ) VALUES (
                        SEQ_CONTATO_PACIENTE.NEXTVAL, :cd_paciente, :ddd,
                        :telefone, 'CELULAR', 'C', 'S', 'N'
                    )
                    """,
                    {
                        'cd_paciente': cd_paciente,
                        'ddd': ddd_celular,
                        'telefone': celular,
                    },
                )

        if payload.email:
            etapa_atualizacao = 'contato por e-mail'
            contato_email = cursor.execute(
                """
                SELECT CD_OUTROS_CONTATOS_PACIENTE
                  FROM DBAMV.OUTROS_CONTATOS_PACIENTE
                 WHERE CD_PACIENTE = :cd_paciente
                   AND TP_OUTROS_CONTATO = 'E'
                 ORDER BY NVL(SN_PADRAO, 'N') DESC, CD_OUTROS_CONTATOS_PACIENTE DESC
                 FETCH FIRST 1 ROW ONLY
                """,
                {'cd_paciente': cd_paciente},
            ).fetchone()
            if contato_email:
                cursor.execute(
                    """
                    UPDATE DBAMV.OUTROS_CONTATOS_PACIENTE
                       SET CONTATO = :email,
                           SN_PADRAO = 'S',
                           DS_TIP_COMUN = 'E-MAIL'
                     WHERE CD_OUTROS_CONTATOS_PACIENTE = :cd_contato
                    """,
                    {'email': payload.email, 'cd_contato': contato_email[0]},
                )
            else:
                cursor.execute(
                    """
                    INSERT INTO DBAMV.OUTROS_CONTATOS_PACIENTE (
                        CD_OUTROS_CONTATOS_PACIENTE, CD_PACIENTE,
                        TP_OUTROS_CONTATO, CONTATO, SN_PADRAO,
                        SN_RECEBE_CONTATO, DS_TIP_COMUN
                    ) VALUES (
                        SEQ_OUTROS_CONTATOS_PACIENTE.NEXTVAL, :cd_paciente,
                        'E', :email, 'S', 'N', 'E-MAIL'
                    )
                    """,
                    {'cd_paciente': cd_paciente, 'email': payload.email},
                )

        if payload.cd_convenio is not None and payload.cd_con_pla is not None:
            etapa_atualizacao = 'convênio e plano'
            plano = cursor.execute(
                """
                SELECT 1
                  FROM DBAMV.CONVENIO c
                  JOIN DBAMV.CON_PLA cp ON cp.CD_CONVENIO = c.CD_CONVENIO
                 WHERE c.CD_CONVENIO = :cd_convenio
                   AND cp.CD_CON_PLA = :cd_con_pla
                   AND NVL(c.SN_ATIVO, 'S') = 'S'
                   AND NVL(cp.SN_ATIVO, 'S') = 'S'
                """,
                {
                    'cd_convenio': payload.cd_convenio,
                    'cd_con_pla': payload.cd_con_pla,
                },
            ).fetchone()
            if not plano:
                raise HTTPException(
                    status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
                    detail='Convenio/plano invalido ou inativo no MV.',
                )
            etapa_atualizacao = 'carteira do convênio'
            carteira = cursor.execute(
                """
                SELECT 1
                  FROM DBAMV.CARTEIRA
                 WHERE CD_PACIENTE = :cd_paciente
                   AND CD_CONVENIO = :cd_convenio
                   AND CD_CON_PLA = :cd_con_pla
                 FETCH FIRST 1 ROW ONLY
                """,
                {
                    'cd_paciente': cd_paciente,
                    'cd_convenio': payload.cd_convenio,
                    'cd_con_pla': payload.cd_con_pla,
                },
            ).fetchone()
            if carteira:
                cursor.execute(
                    """
                    UPDATE DBAMV.CARTEIRA
                       SET NR_CARTEIRA = NVL(:nr_carteira, NR_CARTEIRA),
                           SN_CARTEIRA_ATIVO = 'S'
                     WHERE CD_PACIENTE = :cd_paciente
                       AND CD_CONVENIO = :cd_convenio
                       AND CD_CON_PLA = :cd_con_pla
                    """,
                    {
                        'nr_carteira': payload.nr_carteira,
                        'cd_paciente': cd_paciente,
                        'cd_convenio': payload.cd_convenio,
                        'cd_con_pla': payload.cd_con_pla,
                    },
                )
            else:
                cursor.execute(
                    """
                    INSERT INTO DBAMV.CARTEIRA (
                        CD_CONVENIO, CD_PACIENTE, CD_CON_PLA, NR_CARTEIRA,
                        NM_TITULAR, SN_TITULAR, SN_CARTEIRA_ATIVO
                    ) VALUES (
                        :cd_convenio, :cd_paciente, :cd_con_pla,
                        :nr_carteira, :nm_titular, 'S', 'S'
                    )
                    """,
                    {
                        'cd_convenio': payload.cd_convenio,
                        'cd_paciente': cd_paciente,
                        'cd_con_pla': payload.cd_con_pla,
                        'nr_carteira': payload.nr_carteira,
                        'nm_titular': str(paciente[0]).strip().upper(),
                    },
                )
        etapa_atualizacao = 'confirmação da alteração'
        connection.commit()
    except HTTPException:
        connection.rollback()
        raise
    except Exception as exc:
        connection.rollback()
        detalhe = detalhe_seguro_atualizacao_paciente(
            exc,
            etapa=etapa_atualizacao,
        )
        logger.exception(
            'Falha ao atualizar paciente no MV: cd_paciente=%s etapa=%s',
            cd_paciente,
            etapa_atualizacao,
        )
        raise HTTPException(
            status_code=HTTPStatus.CONFLICT,
            detail=detalhe,
        ) from exc
    finally:
        cursor.close()

    return {
        'cd_paciente': cd_paciente,
        'mensagem': 'Dados do paciente atualizados com sucesso no MV.',
    }


@router.get(
    '/pacientes/{cd_paciente}/agendamentos',
    status_code=HTTPStatus.OK,
    response_model=AgendamentosPaciente,
)
def consultar_agendamentos_paciente(
    usuario_atual: ValidaUsuarioAtual,
    cd_paciente: int,
    session: Session = Depends(get_session_oracle),
):
    garantir_paciente_autorizado(usuario_atual, cd_paciente)
    try:
        rows = (
            session
            .execute(
                CONSULTA_AGENDAMENTOS_PACIENTE,
                {'cd_paciente': cd_paciente},
            )
            .mappings()
            .all()
        )
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=HTTPStatus.SERVICE_UNAVAILABLE,
            detail='Nao foi possivel consultar os agendamentos no MV.',
        ) from exc
    unicos = {}
    for row in rows:
        registro = dict(row)
        chave = registro['cd_it_agenda_central']
        anterior = unicos.get(chave)
        if anterior is None or (
            registro.get('item_movimento_id') or 0
        ) > (anterior.get('item_movimento_id') or 0):
            unicos[chave] = registro
    agendamentos = list(unicos.values())
    return {'agendamentos': agendamentos, 'total': len(agendamentos)}


@router.get(
    '/pacientes/{cd_paciente}/historico',
    status_code=HTTPStatus.OK,
    response_model=HistoricoPaciente,
)
def consultar_historico_paciente(
    usuario_atual: ValidaUsuarioAtual,
    cd_paciente: int,
    session: Session = Depends(get_session_oracle),
):
    """Retorna os ultimos atendimentos registrados no MV para o paciente."""
    garantir_paciente_autorizado(usuario_atual, cd_paciente)
    if cd_paciente <= 0:
        raise HTTPException(
            status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
            detail='Codigo do paciente invalido.',
        )
    try:
        rows = (
            session
            .execute(
                CONSULTA_ULTIMOS_ATENDIMENTOS_PACIENTE,
                {'cd_paciente': cd_paciente, 'limite': 3},
            )
            .mappings()
            .all()
        )
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=HTTPStatus.SERVICE_UNAVAILABLE,
            detail='Nao foi possivel consultar o historico do paciente no MV.',
        ) from exc

    atendimentos = [dict(row) for row in rows]
    if not atendimentos:
        return {'ultimo_atendimento': None, 'atendimentos': [], 'total': 0}
    return {
        'ultimo_atendimento': atendimentos[0],
        'atendimentos': atendimentos,
        'total': len(atendimentos),
    }


@router.get(
    '/pacientes/{cd_paciente}/linhas-cuidado',
    status_code=HTTPStatus.OK,
    response_model=LinhasCuidadoPaciente,
)
def consultar_linhas_cuidado_paciente(
    usuario_atual: ValidaUsuarioAtual,
    cd_paciente: int,
    session: Session = Depends(get_session_oracle),
):
    """Retorna linhas de cuidado registradas para o paciente no MV."""
    garantir_paciente_autorizado(usuario_atual, cd_paciente)
    if cd_paciente <= 0:
        raise HTTPException(
            status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
            detail='Codigo do paciente invalido.',
        )

    try:
        rows = (
            session
            .execute(
                CONSULTA_LINHAS_CUIDADO_PACIENTE,
                {'cd_paciente': cd_paciente},
            )
            .mappings()
            .all()
        )
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=HTTPStatus.SERVICE_UNAVAILABLE,
            detail='Nao foi possivel consultar as linhas de cuidado do paciente no MV.',
        ) from exc

    documentos = {}
    ordem = []
    for row in rows:
        cd_registro = row.get('cd_registro')
        chave = cd_registro or f"{row.get('cd_atendimento')}:{row.get('dh_documento')}"
        if chave not in documentos:
            documentos[chave] = {
                'cd_documento': row.get('cd_documento'),
                'cd_registro': cd_registro,
                'cd_atendimento': row.get('cd_atendimento'),
                'ds_documento': row.get('ds_documento'),
                'ds_tipo_documento': row.get('ds_tipo_documento'),
                'tp_status': row.get('tp_status'),
                'dh_documento': row.get('dh_documento'),
                'dh_fechamento': row.get('dh_fechamento'),
                'cd_usuario_criou': row.get('cd_usuario_criou'),
                'respostas': [],
            }
            ordem.append(chave)

        resposta = row.get('ds_resposta')
        campo = row.get('ds_campo_filho')
        identificador = row.get('ds_identificador_filho')
        if resposta is None or str(resposta).strip().lower() == 'null':
            continue
        if identificador and str(identificador).upper().startswith('PAR_'):
            continue
        documentos[chave]['respostas'].append(
            {
                'campo': campo,
                'identificador': identificador,
                'resposta': str(resposta),
            }
        )

    linhas = [documentos[chave] for chave in ordem[:5]]
    return {'linhas': linhas, 'total': len(linhas)}


@router.post(
    '/{cd_it_agenda_central}/cancelar',
    status_code=HTTPStatus.OK,
    response_model=AgendamentoCancelado,
)
def cancelar_agendamento(
    cd_it_agenda_central: int,
    payload: CancelarAgendamentoInput,
    usuario_atual: ValidaUsuarioAtual,
    session: Session = Depends(get_session_oracle),
):
    if os.getenv('MV_GRAVACAO_HABILITADA', 'false').lower() != 'true':
        raise HTTPException(
            status_code=HTTPStatus.NOT_IMPLEMENTED,
            detail='Gravacao do MV desabilitada neste ambiente.',
        )
    try:
        dados_whatsapp_cancelamento = (
            session.execute(
                CONSULTA_DADOS_CANCELAMENTO_WHATSAPP,
                {'cd_it_agenda_central': cd_it_agenda_central},
            )
            .mappings()
            .first()
        )
        dados_whatsapp_cancelamento = (
            dict(dados_whatsapp_cancelamento)
            if dados_whatsapp_cancelamento
            else None
        )
    except SQLAlchemyError:
        dados_whatsapp_cancelamento = None
    if isinstance(usuario_atual, PrincipalPaciente):
        if not dados_whatsapp_cancelamento:
            raise HTTPException(
                status_code=HTTPStatus.NOT_FOUND,
                detail=(
                    'Agendamento não encontrado para o paciente autenticado.'
                ),
            )
        garantir_paciente_autorizado(
            usuario_atual,
            int(dados_whatsapp_cancelamento['cd_paciente']),
        )

    connection = session.connection().connection
    cursor = connection.cursor()
    retorno = cursor.var(str, 10)
    try:
        cursor.callproc(
            'DBAMV.PKG_AGENDAMENTO_WEB.PRC_EXCLUIR_AGD_WEB',
            [cd_it_agenda_central, payload.motivo, retorno],
        )
        codigo = retorno.getvalue()
        if codigo not in ('S', '1', 'OK', None):
            raise HTTPException(
                status_code=HTTPStatus.CONFLICT,
                detail=f'O MV recusou o cancelamento (retorno {codigo}).',
            )
        connection.commit()
    except HTTPException:
        raise
    except Exception as exc:
        connection.rollback()
        raise HTTPException(
            status_code=HTTPStatus.CONFLICT,
            detail='Falha ao cancelar o agendamento no MV.',
        ) from exc
    finally:
        cursor.close()

    if dados_whatsapp_cancelamento:
        _registrar_evento_pos_mv(
            engine=postgres_engine,
            usuario_atual=usuario_atual,
            status='cancelado',
            cd_paciente=int(
                dados_whatsapp_cancelamento['cd_paciente']
            ),
            cd_item_agendamento=int(
                dados_whatsapp_cancelamento['cd_item_agendamento']
            ),
            cd_it_agenda_central=cd_it_agenda_central,
            cd_agenda_central=int(
                dados_whatsapp_cancelamento['cd_agenda_central']
            ),
            cd_tip_mar=None,
            protocolo_mv=dados_whatsapp_cancelamento.get('protocolo'),
        )

    whatsapp_status, whatsapp_mensagem = _enviar_cancelamento_whatsapp_agendamento(
        dados_whatsapp_cancelamento,
    )

    return {
        'status': 'cancelado',
        'mensagem': 'Agendamento cancelado com sucesso no MV.',
        'horario_id': cd_it_agenda_central,
        'retorno_mv': codigo,
        'whatsapp_status': whatsapp_status,
        'whatsapp_mensagem': whatsapp_mensagem,
    }


@router.get(
    '/itens',
    status_code=HTTPStatus.OK,
    response_model=ItensAgendamentoEncontrados,
)
def consultar_itens(
    usuario_atual: ValidaUsuarioAtual,
    termo: Annotated[str, Query(min_length=2, max_length=80)],
    limite: Annotated[int, Query(ge=1, le=50)] = 20,
    session: Session = Depends(get_session_oracle),
):
    del usuario_atual
    termo_limpo = termo.strip()
    termo_normalizado = _normaliza_busca(termo_limpo)
    codigo = int(termo_limpo) if termo_limpo.isdigit() else -1
    busca_laboratorio = (
        'S'
        if termo_normalizado in {'LAB', 'LABORATORIO'}
        or termo_normalizado.startswith('LABORATOR')
        else 'N'
    )

    try:
        rows = (
            session
            .execute(
                CONSULTA_ITENS,
                {
                    'codigo': codigo,
                    'descricao': f'%{termo_limpo.upper()}%',
                    'busca_laboratorio': busca_laboratorio,
                    'limite': limite,
                },
            )
            .mappings()
            .all()
        )
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=HTTPStatus.SERVICE_UNAVAILABLE,
            detail='Nao foi possivel consultar os itens no MV.',
        ) from exc

    itens = []
    for row in rows:
        item = dict(row)
        realizacao = item.pop('hr_realizacao', None)
        item['duracao_minutos'] = (
            realizacao.hour * 60 + realizacao.minute
            if isinstance(realizacao, datetime)
            else None
        )
        itens.append(item)
    return {'itens': itens, 'total': len(itens)}


@router.get(
    '/procedimentos-mv',
    status_code=HTTPStatus.OK,
    response_model=PaginaProcedimentosMv,
)
def consultar_procedimentos_mv(
    usuario_atual: ValidaUsuarioAtual,
    termo: Annotated[str, Query(min_length=2, max_length=80)],
    cursor: Annotated[str | None, Query(max_length=20)] = None,
    limite: Annotated[int, Query(ge=1, le=50)] = 20,
    session: Session = Depends(get_session_oracle),
):
    del usuario_atual
    termo_limpo = termo.strip().upper()
    try:
        rows = (
            session.execute(
                CONSULTA_PROCEDIMENTOS_MV,
                {
                    'codigo': f'{termo_limpo}%',
                    'descricao': f'%{termo_limpo}%',
                    'cursor': cursor,
                    'limite': limite + 1,
                },
            )
            .mappings()
            .all()
        )
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=HTTPStatus.SERVICE_UNAVAILABLE,
            detail='Nao foi possivel consultar os procedimentos no MV.',
        ) from exc

    tem_proxima = len(rows) > limite
    itens = [dict(row) for row in rows[:limite]]
    proximo_cursor = itens[-1]['codigo_mv'] if tem_proxima else None
    return {'itens': itens, 'proximo_cursor': proximo_cursor}


@router.get(
    '/itens/{cd_item_agendamento}/orientacoes',
    status_code=HTTPStatus.OK,
    response_model=OrientacaoExame,
)
def consultar_orientacoes_item(
    usuario_atual: ValidaUsuarioAtual,
    cd_item_agendamento: int,
    session: Session = Depends(get_session_oracle),
):
    """Retorna as orientacoes/preparo de exame vinculadas ao item de agenda."""
    del usuario_atual
    if cd_item_agendamento <= 0:
        raise HTTPException(
            status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
            detail='Codigo do item invalido.',
        )

    try:
        item = session.execute(
            text(
                """
                SELECT ia.CD_EXA_RX AS cd_exa_rx,
                       er.DS_EXA_RX AS ds_exa_rx
                  FROM DBAMV.ITEM_AGENDAMENTO ia
                  LEFT JOIN DBAMV.EXA_RX er
                    ON er.CD_EXA_RX = ia.CD_EXA_RX
                 WHERE ia.CD_ITEM_AGENDAMENTO = :cd_item_agendamento
                """
            ),
            {'cd_item_agendamento': cd_item_agendamento},
        ).mappings().first()
        if item is None:
            raise HTTPException(
                status_code=HTTPStatus.NOT_FOUND,
                detail='Item de agendamento nao encontrado no MV.',
            )
        if item['cd_exa_rx'] is None:
            return {
                'cd_item_agendamento': cd_item_agendamento,
                'cd_exa_rx': None,
                'ds_exa_rx': None,
                'orientacoes': [],
                'total': 0,
            }

        rows = (
            session.execute(
                text(
                    """
                    SELECT DS_ORIENTACAO AS ds_orientacao
                      FROM DBAMV.EMPRESA_ORIENTACOES_EXA_RX
                     WHERE CD_EXA_RX = :cd_exa_rx
                       AND (CD_MULTI_EMPRESA = 1 OR CD_MULTI_EMPRESA IS NULL)
                       AND DS_ORIENTACAO IS NOT NULL
                     ORDER BY NVL(CD_MULTI_EMPRESA, 0)
                    """
                ),
                {'cd_exa_rx': item['cd_exa_rx']},
            )
            .mappings()
            .all()
        )
    except HTTPException:
        raise
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=HTTPStatus.SERVICE_UNAVAILABLE,
            detail='Nao foi possivel consultar as orientacoes do exame no MV.',
        ) from exc

    orientacoes = [
        row['ds_orientacao'].strip()
        for row in rows
        if row['ds_orientacao'] and row['ds_orientacao'].strip()
    ]
    return {
        'cd_item_agendamento': cd_item_agendamento,
        'cd_exa_rx': item['cd_exa_rx'],
        'ds_exa_rx': item['ds_exa_rx'],
        'orientacoes': orientacoes,
        'total': len(orientacoes),
    }


@router.get('/regras')
def consultar_regras_agendamento(
    usuario_atual: ValidaUsuarioAtual,
    cd_item_agendamento: Annotated[int, Query(gt=0)],
    cd_convenio: Annotated[int, Query(gt=0)],
    cd_con_pla: Annotated[int, Query(gt=0)],
    session: Session = Depends(get_session_oracle),
):
    """Retorna sinalizacoes de autorizacao e filtros usados pela Central do MV."""
    del usuario_atual
    consulta = text('''
        SELECT c.NM_CONVENIO AS nm_convenio,
               c.SN_GUIA AS sn_guia,
               c.TP_AUTORIZ_CENTRAL_AGENDAMENTO AS tp_autoriz_central,
               c.SN_OBRIGA_PLANO_AGENDA AS sn_obriga_plano,
               cp.DS_CON_PLA AS ds_con_pla,
               cs.SN_VERIFICA_PROIBICAO_AGD AS sn_verifica_proibicao,
               cs.SN_VALIDA_CONVENIO_ITEM AS sn_valida_convenio_item,
               cs.SN_AGENDAMENTO_WEB AS sn_agendamento_web
          FROM DBAMV.CONVENIO c
          JOIN DBAMV.CON_PLA cp ON cp.CD_CONVENIO = c.CD_CONVENIO
          CROSS JOIN DBAMV.CONFIG_SCMA cs
         WHERE c.CD_CONVENIO = :cd_convenio
           AND cp.CD_CON_PLA = :cd_con_pla
           AND cs.CD_MULTI_EMPRESA = 1
    ''')
    item = session.execute(
        text('SELECT DS_ITEM_AGENDAMENTO FROM DBAMV.ITEM_AGENDAMENTO WHERE CD_ITEM_AGENDAMENTO = :item'),
        {'item': cd_item_agendamento},
    ).scalar()
    row = session.execute(
        consulta,
        {'cd_convenio': cd_convenio, 'cd_con_pla': cd_con_pla},
    ).mappings().first()
    if not row:
        raise HTTPException(status_code=HTTPStatus.NOT_FOUND, detail='Convenio/plano nao encontrado no MV.')
    dados = dict(row)
    alertas = [
        'O MV pode aplicar proibicoes por item, convenio, empresa e data.',
        'A disponibilidade exibida depende da unidade, setor, recurso e modalidade da agenda.',
    ]
    if dados['sn_guia'] == 'S' or dados['tp_autoriz_central'] not in (None, 'N'):
        alertas.insert(0, 'Este convenio exige autorizacao/guia para o agendamento.')
    if dados['sn_verifica_proibicao'] == 'S':
        alertas.insert(0, 'A Central do MV esta configurada para verificar proibicoes de agendamento.')
    return {
        'item': {'cd_item_agendamento': cd_item_agendamento, 'ds_item_agendamento': item},
        'convenio': dados,
        'alertas': alertas,
    }



CONSULTA_CONVENIOS_PACIENTE = text(
    """
    SELECT cd_convenio, nm_convenio, cd_con_pla, ds_con_pla,
           ultimo_atendimento
      FROM (
        SELECT x.*,
               ROW_NUMBER() OVER (
                   PARTITION BY x.cd_convenio, x.cd_con_pla
                   ORDER BY x.ultimo_atendimento DESC NULLS LAST
               ) AS ordem
          FROM (
            SELECT c.CD_CONVENIO AS cd_convenio,
                   c.NM_CONVENIO AS nm_convenio,
                   cp.CD_CON_PLA AS cd_con_pla,
                   cp.DS_CON_PLA AS ds_con_pla,
                   a.HR_ATENDIMENTO AS ultimo_atendimento
              FROM DBAMV.ATENDIME a
              JOIN DBAMV.CONVENIO c ON c.CD_CONVENIO = a.CD_CONVENIO
              JOIN DBAMV.CON_PLA cp
                ON cp.CD_CONVENIO = a.CD_CONVENIO
               AND cp.CD_CON_PLA = a.CD_CON_PLA
             WHERE a.CD_PACIENTE = :cd_paciente
            UNION ALL
            SELECT c.CD_CONVENIO, c.NM_CONVENIO, cp.CD_CON_PLA,
                   cp.DS_CON_PLA, NULL
              FROM DBAMV.CARTEIRA ca
              JOIN DBAMV.CONVENIO c ON c.CD_CONVENIO = ca.CD_CONVENIO
              JOIN DBAMV.CON_PLA cp
                ON cp.CD_CONVENIO = ca.CD_CONVENIO
               AND cp.CD_CON_PLA = ca.CD_CON_PLA
             WHERE ca.CD_PACIENTE = :cd_paciente
               AND NVL(ca.SN_CARTEIRA_ATIVO, 'S') = 'S'
          ) x
      )
     WHERE ordem = 1
     ORDER BY ultimo_atendimento DESC, nm_convenio, ds_con_pla
    """
)

@router.get(
    '/pacientes/{cd_paciente}/convenios',
    status_code=HTTPStatus.OK,
    response_model=ConveniosPaciente,
)
def consultar_convenios_paciente(
    usuario_atual: ValidaUsuarioAtual,
    cd_paciente: int,
    session: Session = Depends(get_session_oracle),
):
    garantir_paciente_autorizado(usuario_atual, cd_paciente)
    if cd_paciente <= 0:
        raise HTTPException(
            status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
            detail='Codigo do paciente invalido.',
        )

    try:
        rows = (
            session
            .execute(
                CONSULTA_CONVENIOS_PACIENTE,
                {'cd_paciente': cd_paciente},
            )
            .mappings()
            .all()
        )
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=HTTPStatus.SERVICE_UNAVAILABLE,
            detail='Nao foi possivel consultar os convenios no MV.',
        ) from exc

    convenios = [dict(row) for row in rows]
    return {'convenios': convenios, 'total': len(convenios)}


@router.get(
    '/itens/{cd_item_agendamento}/prestadores',
    status_code=HTTPStatus.OK,
    response_model=PrestadoresAgendamento,
)
def consultar_prestadores_item(  # noqa: PLR0913
    usuario_atual: ValidaUsuarioAtual,
    cd_item_agendamento: int,
    limite: Annotated[int, Query(ge=1, le=100)] = 50,
    data_inicio: date | None = None,
    data_fim: date | None = None,
    cd_paciente: Annotated[int | None, Query(gt=0)] = None,
    session: Session = Depends(get_session_oracle),
):
    if cd_paciente is not None:
        garantir_paciente_autorizado(usuario_atual, cd_paciente)
    if cd_item_agendamento <= 0:
        raise HTTPException(
            status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
            detail='Codigo do item invalido.',
        )

    inicio = data_inicio or date.today()
    fim = data_fim or inicio + timedelta(days=30)
    if fim < inicio or fim - inicio > timedelta(days=90):
        raise HTTPException(
            status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
            detail='Periodo de consulta de prestadores invalido.',
        )

    try:
        rows = (
            session
            .execute(
                CONSULTA_PRESTADORES_ITEM,
                {
                    'cd_item_agendamento': cd_item_agendamento,
                    'limite': limite,
                    'data_inicio': inicio,
                    'data_fim': fim,
                    'cd_paciente': cd_paciente,
                },
            )
            .mappings()
            .all()
        )
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=HTTPStatus.SERVICE_UNAVAILABLE,
            detail='Nao foi possivel consultar os prestadores no MV.',
        ) from exc

    prestadores = [dict(row) for row in rows]
    descricao_item = session.scalar(
        text('SELECT DS_ITEM_AGENDAMENTO FROM DBAMV.ITEM_AGENDAMENTO WHERE CD_ITEM_AGENDAMENTO = :item'),
        {'item': cd_item_agendamento},
    ) or ''
    eh_consulta = 'CONSULTA' in descricao_item.upper()
    if not eh_consulta:
        prestadores = []
    exige_prestador = bool(prestadores) and eh_consulta
    if not exige_prestador:
        exige_prestador = eh_consulta and bool(
            session.scalar(
                CONSULTA_ITEM_EXIGE_PRESTADOR,
                {'cd_item_agendamento': cd_item_agendamento},
            )
        )
    return {
        'prestadores': prestadores,
        'total': len(prestadores),
        'exige_prestador': exige_prestador,
    }


@router.post(
    '/pre-validar',
    status_code=HTTPStatus.OK,
    response_model=PreValidacaoAgendamento,
)
def pre_validar_agendamento(
    payload: PreValidacaoAgendamentoInput,
    usuario_atual: ValidaUsuarioAtual,
    session: Session = Depends(get_session_oracle),
):
    garantir_paciente_autorizado(usuario_atual, payload.cd_paciente)
    _garantir_reserva_disponivel(
        payload.cd_it_agenda_central, payload.reserva_token
    )
    parametros = payload.model_dump()
    parametros.pop('reserva_token', None)

    try:
        row = (
            session
            .execute(CONSULTA_PRE_VALIDACAO, parametros)
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise HTTPException(
                status_code=HTTPStatus.CONFLICT,
                detail=(
                    'O horario nao corresponde aos dados selecionados. '
                    'Atualize a disponibilidade.'
                ),
            )

        dados = dict(row)
        if (
            dados['slot_cd_paciente'] is not None
            or dados['slot_dt_gravacao'] is not None
            or dados['slot_bloqueado'] == 'S'
            or dados['slot_futuro'] != 'S'
        ):
            raise HTTPException(
                status_code=HTTPStatus.CONFLICT,
                detail='O horario nao esta mais disponivel.',
            )

        prestador_agenda = dados['cd_prestador']
        if prestador_agenda is not None and payload.cd_prestador is None:
            raise HTTPException(
                status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
                detail='Selecione o medico/prestador da agenda.',
            )
        if payload.cd_prestador != prestador_agenda:
            raise HTTPException(
                status_code=HTTPStatus.CONFLICT,
                detail='O horario pertence a outro medico/prestador.',
            )

        duplicado = (
            session
            .execute(
                CONSULTA_AGENDAMENTO_DUPLICADO,
                {
                    'cd_paciente': payload.cd_paciente,
                    'cd_item_agendamento': payload.cd_item_agendamento,
                },
            )
            .mappings()
            .first()
        )
    except HTTPException:
        raise
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=HTTPStatus.SERVICE_UNAVAILABLE,
            detail='Nao foi possivel pre-validar o agendamento no MV.',
        ) from exc

    alertas = []
    if duplicado:
        alertas.append(
            'O paciente ja possui agendamento futuro para este item.'
        )

    for campo in (
        'slot_cd_paciente',
        'slot_dt_gravacao',
        'slot_bloqueado',
    ):
        dados.pop(campo)
    dados['pode_agendar'] = not duplicado
    dados['alertas'] = alertas
    dados['agendamento_existente_slot'] = (
        duplicado['cd_it_agenda_central'] if duplicado else None
    )
    dados['agendamento_existente_horario'] = (
        duplicado['horario'] if duplicado else None
    )
    return dados


@router.get(
    '/horarios',
    status_code=HTTPStatus.OK,
    response_model=HorariosDisponiveis,
)
def consultar_horarios(  # noqa: PLR0913
    usuario_atual: ValidaUsuarioAtual,
    cd_item_agendamento: Annotated[int, Query(gt=0)],
    data_inicio: date | None = None,
    data_fim: date | None = None,
    limite: Annotated[int, Query(ge=1, le=200)] = 50,
    cd_prestador: Annotated[int | None, Query(gt=0)] = None,
    cd_tip_mar: Annotated[int | None, Query(gt=0)] = None,
    cd_paciente: Annotated[int | None, Query(gt=0)] = None,
    cd_convenio: Annotated[int | None, Query(gt=0)] = None,
    cd_con_pla: Annotated[int | None, Query(gt=0)] = None,
    reserva_token: Annotated[
        str | None, Query(min_length=16, max_length=100)
    ] = None,
    session: Session = Depends(get_session_oracle),
):
    del usuario_atual
    inicio = data_inicio or date.today()
    fim = data_fim or inicio + timedelta(days=30)

    if fim < inicio:
        raise HTTPException(
            status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
            detail='data_fim deve ser igual ou posterior a data_inicio.',
        )
    if fim - inicio > timedelta(days=90):
        raise HTTPException(
            status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
            detail='O periodo maximo de consulta e de 90 dias.',
        )

    try:
        rows = (
            session
            .execute(
                CONSULTA_HORARIOS,
                {
                    'cd_item_agendamento': cd_item_agendamento,
                    'data_inicio': inicio,
                    'data_fim': fim,
                    'limite': limite,
                    'cd_prestador': cd_prestador,
                    'cd_tip_mar': cd_tip_mar,
                    'cd_paciente': cd_paciente,
                    'cd_convenio': cd_convenio,
                    'cd_con_pla': cd_con_pla,
                },
            )
            .mappings()
            .all()
        )
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=HTTPStatus.SERVICE_UNAVAILABLE,
            detail='Nao foi possivel consultar os horarios no MV.',
        ) from exc

    horarios = _normaliza_horarios_exibicao([dict(row) for row in rows])
    horarios = _filtrar_reservas_horarios(horarios, reserva_token)
    for horario in horarios:
        unidade = (horario.get('ds_unidade_atendimento') or '').strip().upper()
        if unidade == 'CLINICA PRONTOCARDIO SAUDE':
            horario['ds_unidade_atendimento'] = 'CLÍNICA DIAGNÓSTICA 01'
        elif unidade == 'UNIDADE DIAGNOSTICO 2':
            horario['ds_unidade_atendimento'] = 'CLÍNICA DIAGNÓSTICA 02'
    return {'horarios': horarios, 'total': len(horarios)}


@router.get(
    '/pacientes/{cd_paciente}/historico-agendamentos',
    status_code=HTTPStatus.OK,
    response_model=HistoricoAgendamentosInterno,
)
def consultar_historico_agendamentos_interno(  # noqa: PLR0913
    usuario_atual: Annotated[
        Usuario | SimpleNamespace,
        Depends(exigir_usuario_historico_agendamento),
    ],
    cd_paciente: int,
    data_inicio: Annotated[date | None, Query()] = None,
    data_fim: Annotated[date | None, Query()] = None,
    pagina: Annotated[int, Query(ge=1)] = 1,
    limite: Annotated[int, Query(ge=1, le=50)] = 20,
    status: Annotated[str | None, Query(max_length=30)] = None,
    origem: OrigemAgendamento | None = None,
    oracle: Session = Depends(get_session_oracle),
    postgres: Session = Depends(get_session_postgres),
):
    """Retorna histórico interno do MV; observações não saem deste endpoint."""
    if not settings.AGENDAMENTO_HISTORICO_INTERNO:
        raise HTTPException(
            status_code=HTTPStatus.NOT_FOUND,
            detail='Recurso de histórico interno não habilitado.',
        )
    if cd_paciente <= 0:
        raise HTTPException(
            status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
            detail='Código do paciente inválido.',
        )
    try:
        return RepositorioHistoricoAgendamento(oracle, postgres).listar(
            cd_paciente=cd_paciente,
            data_inicio=data_inicio,
            data_fim=data_fim,
            pagina=pagina,
            limite=limite,
            status=status,
            origem=origem,
            operador=usuario_atual,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=HTTPStatus.SERVICE_UNAVAILABLE,
            detail='Não foi possível consultar o histórico de agendamentos no MV.',
        ) from exc
