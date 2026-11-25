from datetime import date, datetime
from http import HTTPStatus
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app_prontocardio.agenda_medica_models import AgendaMedicaTratativaPaciente
from app_prontocardio.database import get_session_oracle, get_session_postgres
from app_prontocardio.models import Usuario
from app_prontocardio.routers.agendamentos import ValidaUsuarioAtual
from app_prontocardio.security import valida_token_usuario_atual
from app_prontocardio.unidades import nome_unidade_publica

router = APIRouter(prefix='/operacional', tags=['operacional'])
SessionOracle = Annotated[Session, Depends(get_session_oracle)]
SessionPostgres = Annotated[Session, Depends(get_session_postgres)]
UsuarioOperacional = Annotated[Usuario, Depends(valida_token_usuario_atual)]
FUSO_HORARIO_BRASIL = ZoneInfo('America/Sao_Paulo')
StatusConfirmacao = Literal[
    'confirmado',
    'nao_confirmado',
    'sem_resposta',
    'cancelamento_solicitado',
    'reagendamento_solicitado',
]
STATUS_ASSUNTO = {
    'confirmado': 'CONFIRMAÇÃO VIA WHATSAPP',
    'nao_confirmado': 'NÃO CONFIRMADO VIA WHATSAPP',
    'sem_resposta': 'SEM RESPOSTA VIA WHATSAPP',
    'cancelamento_solicitado': 'CANCELAMENTO SOLICITADO VIA WHATSAPP',
    'reagendamento_solicitado': 'REAGENDAMENTO SOLICITADO VIA WHATSAPP',
}
INTERVALO_MAXIMO_CONFIRMACOES_DIAS = 31
TELAS_CONFIRMACAO_LEITURA = {
    'agenda_medica_atendente',
    'agenda_medica_admin',
    'agenda_medica_responsavel',
}
TELAS_CONFIRMACAO_ESCRITA = {
    'agenda_medica_atendente',
    'agenda_medica_admin',
}


class ConfirmacaoContatoInput(BaseModel):
    status: StatusConfirmacao
    id_mensagem_externa: str = Field(
        min_length=1,
        max_length=200,
        pattern=r'^[A-Za-z0-9._:-]+$',
    )
    data_resposta: datetime | None = None
    observacao: str | None = Field(default=None, max_length=1000)
    motivo_correcao: str | None = Field(default=None, min_length=5, max_length=1000)


class TratativaPacienteInput(BaseModel):
    status: Literal['EM_TRATATIVA', 'CONCLUIDA', 'CANCELADA', 'FALHA']
    motivo: str | None = Field(default=None, max_length=1000)
    cd_it_agenda_destino: int | None = Field(default=None, gt=0)


def _exigir_acesso_confirmacoes(
    usuario: Usuario, *, escrita: bool = False
) -> Usuario:
    if usuario.perfil in {'ti', 'administrador'}:
        return usuario
    permitidas = TELAS_CONFIRMACAO_ESCRITA if escrita else TELAS_CONFIRMACAO_LEITURA
    if not permitidas.intersection(usuario.telas_permitidas or []):
        raise HTTPException(
            status_code=HTTPStatus.FORBIDDEN,
            detail='Usuário sem permissão para confirmações de pacientes.',
        )
    return usuario


def _validar_periodo_confirmacoes(data_inicio: date, data_fim: date) -> None:
    if data_fim < data_inicio:
        raise HTTPException(
            status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
            detail='A data final deve ser igual ou posterior à data inicial.',
        )
    if (data_fim - data_inicio).days >= INTERVALO_MAXIMO_CONFIRMACOES_DIAS:
        raise HTTPException(
            status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
            detail='O período máximo de consulta é de 31 dias.',
        )


def montar_resposta_confirmacoes(rows):
    agendamentos = [dict(row) for row in rows]
    for agendamento in agendamentos:
        agendamento['ds_unidade_atendimento'] = nome_unidade_publica(
            agendamento.get('cd_unidade_atendimento'),
            agendamento.get('ds_unidade_atendimento'),
        )
    total_agendamentos = len(agendamentos)
    total_pacientes = len(
        {
            row['cd_paciente']
            for row in agendamentos
            if row.get('cd_paciente') is not None
        }
    )
    return {
        'agendamentos': agendamentos,
        'total': total_agendamentos,
        'total_agendamentos': total_agendamentos,
        'total_pacientes': total_pacientes,
    }


def montar_resumo_confirmacoes(rows, data_inicio: date, data_fim: date):
    grupos = [dict(row) for row in rows]
    campos_numericos = (
        'total_agendamentos',
        'confirmados',
        'cancelamentos_solicitados',
        'pendentes',
        'encaixes',
        'horarios_regulares',
        'presencas_registradas',
        'atendidos',
        'sem_registro_presenca',
        'compareceram',
        'faltas_registradas',
        'sem_desfecho_comparecimento',
    )
    totais = {campo: 0 for campo in campos_numericos}
    for grupo in grupos:
        grupo['ds_unidade_atendimento'] = nome_unidade_publica(
            grupo.get('cd_unidade_atendimento'),
            grupo.get('ds_unidade_atendimento'),
        )
        for campo in campos_numericos:
            grupo[campo] = int(grupo.get(campo) or 0)
            totais[campo] += grupo[campo]
    totais['taxa_confirmacao'] = round(
        100 * totais['confirmados'] / totais['total_agendamentos'], 1
    ) if totais['total_agendamentos'] else 0.0
    desfechos = totais['compareceram'] + totais['faltas_registradas']
    totais['taxa_comparecimento'] = round(
        100 * totais['compareceram'] / desfechos, 1
    ) if desfechos else None
    totais['taxa_absenteismo'] = round(
        100 * totais['faltas_registradas'] / desfechos, 1
    ) if desfechos else None
    return {
        'consultado_em': datetime.now(FUSO_HORARIO_BRASIL).isoformat(),
        'data_inicio': data_inicio.isoformat(),
        'data_fim': data_fim.isoformat(),
        'totais': totais,
        'grupos': grupos,
    }


@router.get('/agendamentos/confirmacoes/resumo')
def consultar_resumo_confirmacoes_agendamentos(
    usuario: UsuarioOperacional,
    session: SessionOracle,
    data_inicio: date,
    data_fim: date,
):
    """Indicadores agregados de confirmação, encaixe e presença no MV."""
    _exigir_acesso_confirmacoes(usuario)
    _validar_periodo_confirmacoes(data_inicio, data_fim)
    rows = session.execute(
        text(
            """
            WITH contatos AS (
                SELECT rc.CD_REGISTRO_VINCULADO,
                       rc.DS_ASSUNTO,
                       ROW_NUMBER() OVER (
                           PARTITION BY rc.CD_REGISTRO_VINCULADO
                           ORDER BY
                               TRUNC(rc.DT_CONTATO)
                               + (rc.HR_CONTATO - TRUNC(rc.HR_CONTATO)) DESC,
                               rc.CD_REGISTRO_CONTATO DESC
                       ) AS ordem
                  FROM DBAMV.REGISTRO_CONTATO rc
                 WHERE rc.TP_ACAO = 'AS'
            ), dados AS (
                SELECT TO_CHAR(TRUNC(iac.HR_AGENDA), 'YYYY-MM-DD') AS data,
                       ua.CD_UNIDADE_ATENDIMENTO AS cd_unidade_atendimento,
                       ua.DS_UNIDADE_ATENDIMENTO AS ds_unidade_atendimento,
                       ac.CD_SETOR AS cd_setor,
                       s.NM_SETOR AS nm_setor,
                       iac.CD_ESPECIALID AS cd_especialid,
                       e.DS_ESPECIALID AS ds_especialid,
                       pr.CD_PRESTADOR AS cd_prestador,
                       pr.NM_PRESTADOR AS nm_prestador,
                       iac.CD_ITEM_AGENDAMENTO AS cd_item_agendamento,
                       ia.DS_ITEM_AGENDAMENTO AS ds_item_agendamento,
                       NVL(iac.SN_ENCAIXE, 'N') AS sn_encaixe,
                       NVL(iac.SN_ATENDIDO, 'N') AS sn_atendido,
                       iac.TP_PRESENCA_FALTA AS tp_presenca_falta,
                       CASE
                           WHEN rc.DS_ASSUNTO IS NULL THEN 'pendente'
                           WHEN UPPER(rc.DS_ASSUNTO) LIKE '%REAGEND%'
                               THEN 'reagendamento_solicitado'
                           WHEN UPPER(rc.DS_ASSUNTO) LIKE '%CANCEL%'
                               THEN 'cancelamento_solicitado'
                           WHEN UPPER(rc.DS_ASSUNTO) LIKE '%SEM RESPOSTA%'
                               THEN 'sem_resposta'
                           WHEN UPPER(rc.DS_ASSUNTO) LIKE '%NÃO CONFIRM%'
                             OR UPPER(rc.DS_ASSUNTO) LIKE '%NAO CONFIRM%'
                               THEN 'nao_confirmado'
                           WHEN UPPER(rc.DS_ASSUNTO) LIKE '%CONFIRM%'
                               THEN 'confirmado'
                           ELSE 'contato_registrado'
                       END AS status_confirmacao
                  FROM DBAMV.IT_AGENDA_CENTRAL iac
                  JOIN DBAMV.AGENDA_CENTRAL ac
                    ON ac.CD_AGENDA_CENTRAL = iac.CD_AGENDA_CENTRAL
                  LEFT JOIN DBAMV.UNIDADE_ATENDIMENTO ua
                    ON ua.CD_UNIDADE_ATENDIMENTO = ac.CD_UNIDADE_ATENDIMENTO
                  LEFT JOIN DBAMV.SETOR s ON s.CD_SETOR = ac.CD_SETOR
                  LEFT JOIN DBAMV.ESPECIALID e
                    ON e.CD_ESPECIALID = iac.CD_ESPECIALID
                  LEFT JOIN DBAMV.PRESTADOR pr
                    ON pr.CD_PRESTADOR = ac.CD_PRESTADOR
                  LEFT JOIN DBAMV.ITEM_AGENDAMENTO ia
                    ON ia.CD_ITEM_AGENDAMENTO = iac.CD_ITEM_AGENDAMENTO
                  LEFT JOIN contatos rc
                    ON rc.CD_REGISTRO_VINCULADO = iac.CD_IT_AGENDA_CENTRAL
                   AND rc.ordem = 1
                 WHERE iac.CD_PACIENTE IS NOT NULL
                   AND NVL(iac.TP_SITUACAO, 'M') <> 'C'
                   AND iac.CD_IT_AGENDA_PAI IS NULL
                   AND iac.HR_AGENDA >= :data_inicio
                   AND iac.HR_AGENDA < :data_fim + 1
            )
            SELECT data,
                   cd_unidade_atendimento,
                   ds_unidade_atendimento,
                   cd_setor,
                   nm_setor,
                   cd_especialid,
                   ds_especialid,
                   cd_prestador,
                   nm_prestador,
                   cd_item_agendamento,
                   ds_item_agendamento,
                   COUNT(*) AS total_agendamentos,
                   SUM(
                       CASE WHEN status_confirmacao = 'confirmado'
                           THEN 1 ELSE 0 END
                   ) AS confirmados,
                   SUM(
                       CASE WHEN status_confirmacao =
                           'cancelamento_solicitado' THEN 1 ELSE 0 END
                   ) AS cancelamentos_solicitados,
                   SUM(
                       CASE WHEN status_confirmacao NOT IN (
                           'confirmado', 'cancelamento_solicitado'
                       ) THEN 1 ELSE 0 END
                   ) AS pendentes,
                   SUM(
                       CASE WHEN sn_encaixe = 'S' THEN 1 ELSE 0 END
                   ) AS encaixes,
                   SUM(
                       CASE WHEN sn_encaixe <> 'S' THEN 1 ELSE 0 END
                   ) AS horarios_regulares,
                   SUM(
                       CASE WHEN tp_presenca_falta = 'P' THEN 1 ELSE 0 END
                   ) AS presencas_registradas,
                   SUM(
                       CASE WHEN sn_atendido = 'S' THEN 1 ELSE 0 END
                   ) AS atendidos,
                   SUM(
                       CASE WHEN tp_presenca_falta IS NULL THEN 1 ELSE 0 END
                   ) AS sem_registro_presenca,
                   SUM(
                       CASE
                           WHEN tp_presenca_falta = 'P' OR sn_atendido = 'S'
                           THEN 1 ELSE 0
                       END
                   ) AS compareceram,
                   SUM(
                       CASE WHEN tp_presenca_falta = 'F' THEN 1 ELSE 0 END
                   ) AS faltas_registradas,
                   SUM(
                       CASE
                           WHEN NVL(tp_presenca_falta, 'N') NOT IN ('P', 'F')
                            AND sn_atendido <> 'S'
                           THEN 1 ELSE 0
                       END
                   ) AS sem_desfecho_comparecimento
              FROM dados
             GROUP BY data,
                      cd_unidade_atendimento,
                      ds_unidade_atendimento,
                      cd_setor,
                      nm_setor,
                      cd_especialid,
                      ds_especialid,
                      cd_prestador,
                      nm_prestador,
                      cd_item_agendamento,
                      ds_item_agendamento
             ORDER BY data, ds_unidade_atendimento, nm_prestador
            """
        ),
        {'data_inicio': data_inicio, 'data_fim': data_fim},
    ).mappings().all()
    return montar_resumo_confirmacoes(rows, data_inicio, data_fim)


@router.get('/agendamentos/confirmacoes')
def consultar_confirmacoes_agendamentos(
    usuario: UsuarioOperacional,
    session: SessionOracle,
    data_inicio: date,
    data_fim: date,
    status_confirmacao: str | None = Query(
        default=None,
        pattern=(
            '^(pendente|confirmado|nao_confirmado|sem_resposta|'
            'cancelamento_solicitado|reagendamento_solicitado|'
            'contato_registrado)$'
        ),
    ),
    limite: int = Query(default=500, ge=1, le=1000),
):
    """Lista agendamentos e a última confirmação registrada no MV."""
    _exigir_acesso_confirmacoes(usuario)
    _validar_periodo_confirmacoes(data_inicio, data_fim)
    params: dict[str, object] = {
        'data_inicio': data_inicio,
        'data_fim': data_fim,
        'limite': limite,
    }
    filtro_status = ''
    if status_confirmacao:
        filtro_status = 'AND status_confirmacao = :status_confirmacao'
        params['status_confirmacao'] = status_confirmacao

    rows = session.execute(
        text(
            f"""
            WITH contatos AS (
                SELECT rc.CD_REGISTRO_CONTATO,
                       rc.CD_REGISTRO_VINCULADO,
                       rc.DT_CONTATO,
                       rc.HR_CONTATO,
                       rc.CD_USUARIO,
                       rc.DS_ASSUNTO,
                       rc.DS_CONTATO,
                       ROW_NUMBER() OVER (
                           PARTITION BY rc.CD_REGISTRO_VINCULADO
                           ORDER BY
                               TRUNC(rc.DT_CONTATO)
                               + (
                                   rc.HR_CONTATO
                                   - TRUNC(rc.HR_CONTATO)
                               ) DESC,
                                    rc.CD_REGISTRO_CONTATO DESC
                       ) AS ordem
                  FROM DBAMV.REGISTRO_CONTATO rc
                 WHERE rc.TP_ACAO = 'AS'
            ), dados AS (
                SELECT iac.CD_IT_AGENDA_CENTRAL AS cd_it_agenda_central,
                       iac.CD_AGENDA_CENTRAL AS cd_agenda_central,
                       iac.CD_PACIENTE AS cd_paciente,
                       p.NM_PACIENTE AS nm_paciente,
                       NVL(p.NR_DDD_CELULAR, iac.NR_DDD_CELULAR) AS nr_ddd_celular,
                       NVL(p.NR_CELULAR, iac.NR_CELULAR) AS nr_celular,
                       iac.HR_AGENDA AS horario,
                       iac.CD_ITEM_AGENDAMENTO AS cd_item_agendamento,
                       ia.DS_ITEM_AGENDAMENTO AS ds_item_agendamento,
                       pr.CD_PRESTADOR AS cd_prestador,
                       pr.NM_PRESTADOR AS nm_prestador,
                       ua.CD_UNIDADE_ATENDIMENTO AS cd_unidade_atendimento,
                       ua.DS_UNIDADE_ATENDIMENTO AS ds_unidade_atendimento,
                       rc.CD_REGISTRO_CONTATO AS cd_registro_contato,
                       rc.DT_CONTATO AS data_contato,
                       rc.HR_CONTATO AS hora_contato,
                       rc.CD_USUARIO AS usuario_contato,
                       rc.DS_ASSUNTO AS assunto_contato,
                       rc.DS_CONTATO AS detalhe_contato,
                       CASE
                           WHEN rc.CD_REGISTRO_CONTATO IS NULL THEN 'pendente'
                           WHEN UPPER(rc.DS_ASSUNTO) LIKE '%REAGEND%'
                               THEN 'reagendamento_solicitado'
                           WHEN UPPER(rc.DS_ASSUNTO) LIKE '%CANCEL%'
                               THEN 'cancelamento_solicitado'
                           WHEN UPPER(rc.DS_ASSUNTO) LIKE '%SEM RESPOSTA%'
                               THEN 'sem_resposta'
                           WHEN UPPER(rc.DS_ASSUNTO) LIKE '%NÃO CONFIRM%'
                             OR UPPER(rc.DS_ASSUNTO) LIKE '%NAO CONFIRM%'
                               THEN 'nao_confirmado'
                           WHEN UPPER(rc.DS_ASSUNTO) LIKE '%CONFIRM%'
                               THEN 'confirmado'
                           ELSE 'contato_registrado'
                       END AS status_confirmacao
                  FROM DBAMV.IT_AGENDA_CENTRAL iac
                  JOIN DBAMV.AGENDA_CENTRAL ac
                    ON ac.CD_AGENDA_CENTRAL = iac.CD_AGENDA_CENTRAL
                  LEFT JOIN DBAMV.PACIENTE p
                    ON p.CD_PACIENTE = iac.CD_PACIENTE
                  LEFT JOIN DBAMV.ITEM_AGENDAMENTO ia
                    ON ia.CD_ITEM_AGENDAMENTO = iac.CD_ITEM_AGENDAMENTO
                  LEFT JOIN DBAMV.PRESTADOR pr
                    ON pr.CD_PRESTADOR = ac.CD_PRESTADOR
                  LEFT JOIN DBAMV.UNIDADE_ATENDIMENTO ua
                    ON ua.CD_UNIDADE_ATENDIMENTO = ac.CD_UNIDADE_ATENDIMENTO
                  LEFT JOIN contatos rc
                    ON rc.CD_REGISTRO_VINCULADO = iac.CD_IT_AGENDA_CENTRAL
                   AND rc.ordem = 1
                 WHERE iac.CD_PACIENTE IS NOT NULL
                   AND NVL(iac.TP_SITUACAO, 'M') <> 'C'
                   AND iac.CD_IT_AGENDA_PAI IS NULL
                   AND iac.HR_AGENDA >= :data_inicio
                   AND iac.HR_AGENDA < :data_fim + 1
            )
            SELECT *
              FROM dados
             WHERE 1 = 1
               {filtro_status}
             ORDER BY horario
             FETCH FIRST :limite ROWS ONLY
            """
        ),
        params,
    ).mappings().all()
    return montar_resposta_confirmacoes(rows)


@router.put('/agendamentos/{cd_it_agenda_central}/confirmacao-contato')
def registrar_confirmacao_contato(
    cd_it_agenda_central: int,
    payload: ConfirmacaoContatoInput,
    usuario: UsuarioOperacional,
    session: SessionOracle,
    session_postgres: SessionPostgres,
):
    """Registra no MV a resposta recebida por uma integração de contato."""
    _exigir_acesso_confirmacoes(usuario, escrita=True)
    marcador = f'[ID_EXTERNO:{payload.id_mensagem_externa}]'
    tratativa = None
    try:
        agendamento = session.execute(
            text(
                """
                SELECT iac.CD_IT_AGENDA_CENTRAL AS cd_it_agenda_central,
                       iac.CD_PACIENTE AS cd_paciente,
                       p.NM_PACIENTE AS nm_paciente,
                       iac.HR_AGENDA AS horario
                  FROM DBAMV.IT_AGENDA_CENTRAL iac
                  JOIN DBAMV.PACIENTE p
                    ON p.CD_PACIENTE = iac.CD_PACIENTE
                 WHERE iac.CD_IT_AGENDA_CENTRAL = :cd_it_agenda_central
                 FOR UPDATE
                """
            ),
            {'cd_it_agenda_central': cd_it_agenda_central},
        ).mappings().one_or_none()
        if agendamento is None:
            raise HTTPException(
                status_code=HTTPStatus.NOT_FOUND,
                detail=(
                    'Agendamento não encontrado ou sem paciente vinculado '
                    'no MV.'
                ),
            )

        atual = session.execute(
            text(
                """
                SELECT DS_ASSUNTO AS assunto_contato
                  FROM DBAMV.REGISTRO_CONTATO
                 WHERE CD_REGISTRO_VINCULADO = :cd_it_agenda_central
                   AND TP_ACAO = 'AS'
                 ORDER BY TRUNC(DT_CONTATO)
                          + (HR_CONTATO - TRUNC(HR_CONTATO)) DESC,
                          CD_REGISTRO_CONTATO DESC
                 FETCH FIRST 1 ROW ONLY
                """
            ),
            {'cd_it_agenda_central': cd_it_agenda_central},
        ).mappings().one_or_none()
        assunto_atual = atual['assunto_contato'] if atual else None
        assunto_novo = STATUS_ASSUNTO[payload.status]
        eh_correcao = bool(assunto_atual and assunto_atual != assunto_novo)
        if eh_correcao and not payload.motivo_correcao:
            raise HTTPException(
                status_code=HTTPStatus.CONFLICT,
                detail=(
                    'Já existe uma resposta diferente. Informe '
                    'motivo_correcao para manter a trilha de auditoria.'
                ),
            )

        existente = session.execute(
            text(
                """
                SELECT CD_REGISTRO_CONTATO AS cd_registro_contato,
                       DS_ASSUNTO AS assunto_contato,
                       DS_CONTATO AS detalhe_contato
                  FROM DBAMV.REGISTRO_CONTATO
                 WHERE CD_REGISTRO_VINCULADO = :cd_it_agenda_central
                   AND TP_ACAO = 'AS'
                   AND INSTR(DS_CONTATO, :marcador) > 0
                 ORDER BY CD_REGISTRO_CONTATO DESC
                 FETCH FIRST 1 ROW ONLY
                """
            ),
            {
                'cd_it_agenda_central': cd_it_agenda_central,
                'marcador': marcador,
            },
        ).mappings().one_or_none()
        if existente:
            session.rollback()
            if existente['assunto_contato'] != STATUS_ASSUNTO[payload.status]:
                raise HTTPException(
                    status_code=HTTPStatus.CONFLICT,
                    detail=(
                        'O identificador externo já foi utilizado com outro '
                        'status de confirmação.'
                    ),
                )
            return {
                'cd_it_agenda_central': cd_it_agenda_central,
                'cd_registro_contato': existente['cd_registro_contato'],
                'status_confirmacao': payload.status,
                'salvo_no_mv': True,
                'criado': False,
                'idempotente': True,
            }


        if payload.status == 'cancelamento_solicitado' or eh_correcao:
            if payload.status == 'cancelamento_solicitado':
                tratativa = session_postgres.scalar(
                    select(AgendaMedicaTratativaPaciente).where(
                        AgendaMedicaTratativaPaciente.cd_it_agenda_central
                        == cd_it_agenda_central,
                        AgendaMedicaTratativaPaciente.tipo == 'NAO_VIRA',
                        AgendaMedicaTratativaPaciente.status.in_(
                            ('PENDENTE', 'EM_TRATATIVA')
                        ),
                    )
                )
            if tratativa is None:
                tratativa = AgendaMedicaTratativaPaciente(
                    cd_it_agenda_central=cd_it_agenda_central,
                    cd_agenda_central=None,
                    solicitacao_id=None,
                    tipo=(
                        'NAO_VIRA'
                        if payload.status == 'cancelamento_solicitado'
                        else 'CORRECAO_CONFIRMACAO'
                    ),
                    status='EM_TRATATIVA',
                    cd_it_agenda_destino=None,
                    valor_anterior=assunto_atual,
                    valor_novo=assunto_novo,
                    motivo=payload.motivo_correcao or payload.observacao,
                    criado_por_id=usuario.id,
                    criado_por_nome=usuario.nome,
                    concluido_por_id=None,
                    concluido_por_nome=None,
                    concluido_em=None,
                )
                session_postgres.add(tratativa)
                session_postgres.commit()
                session_postgres.refresh(tratativa)

        cd_registro_contato = session.execute(
            text('SELECT DBAMV.SEQ_REGISTRO_CONTATO.NEXTVAL FROM DUAL')
        ).scalar_one()
        cd_usuario_mv = session.execute(
            text("SELECT SYS_CONTEXT('USERENV', 'SESSION_USER') FROM DUAL")
        ).scalar_one()
        data_resposta = payload.data_resposta or datetime.now().astimezone()
        partes_detalhe = [
            STATUS_ASSUNTO[payload.status].capitalize() + '.',
            marcador,
            '[CANAL:WHATSAPP]',
            f'[USUARIO_API:{usuario.nome.strip().upper()}]',
            f'[DATA_RESPOSTA:{data_resposta.isoformat()}]',
        ]
        if payload.observacao:
            partes_detalhe.append(payload.observacao.strip())
        detalhe = ' '.join(partes_detalhe)
        session.execute(
            text(
                """
                INSERT INTO DBAMV.REGISTRO_CONTATO (
                    CD_PACIENTE,
                    CD_REGISTRO_CONTATO,
                    CD_REGISTRO_VINCULADO,
                    DT_CONTATO,
                    HR_CONTATO,
                    CD_USUARIO,
                    DS_ASSUNTO,
                    DS_CONTATO,
                    TP_ACAO,
                    NM_PACIENTE
                ) VALUES (
                    :cd_paciente,
                    :cd_registro_contato,
                    :cd_it_agenda_central,
                    TRUNC(SYSDATE),
                    SYSDATE,
                    :cd_usuario,
                    :ds_assunto,
                    :ds_contato,
                    'AS',
                    :nm_paciente
                )
                """
            ),
            {
                'cd_paciente': agendamento['cd_paciente'],
                'cd_registro_contato': cd_registro_contato,
                'cd_it_agenda_central': cd_it_agenda_central,
                'cd_usuario': str(cd_usuario_mv).strip().upper()[:30],
                'ds_assunto': STATUS_ASSUNTO[payload.status],
                'ds_contato': detalhe[:4000],
                'nm_paciente': agendamento['nm_paciente'],
            },
        )
        session.commit()
        if tratativa is not None:
            tratativa.status = (
                'PENDENTE'
                if payload.status == 'cancelamento_solicitado'
                else 'CONCLUIDA'
            )
            if tratativa.status == 'CONCLUIDA':
                tratativa.concluido_por_id = usuario.id
                tratativa.concluido_por_nome = usuario.nome
                tratativa.concluido_em = datetime.now(FUSO_HORARIO_BRASIL).replace(
                    tzinfo=None
                )
            session_postgres.commit()
        if payload.status == 'confirmado' and eh_correcao:
            fila_cancelamento = session_postgres.scalar(
                select(AgendaMedicaTratativaPaciente).where(
                    AgendaMedicaTratativaPaciente.cd_it_agenda_central
                    == cd_it_agenda_central,
                    AgendaMedicaTratativaPaciente.tipo == 'NAO_VIRA',
                    AgendaMedicaTratativaPaciente.status.in_(
                        ('PENDENTE', 'EM_TRATATIVA', 'FALHA')
                    ),
                )
            )
            if fila_cancelamento is not None:
                fila_cancelamento.status = 'CANCELADA'
                fila_cancelamento.concluido_por_id = usuario.id
                fila_cancelamento.concluido_por_nome = usuario.nome
                fila_cancelamento.concluido_em = datetime.now(
                    FUSO_HORARIO_BRASIL
                ).replace(tzinfo=None)
                session_postgres.commit()
        return {
            'cd_it_agenda_central': cd_it_agenda_central,
            'cd_registro_contato': cd_registro_contato,
            'status_confirmacao': payload.status,
            'salvo_no_mv': True,
            'criado': True,
            'idempotente': False,
            'tratativa_id': tratativa.id if tratativa else None,
        }
    except HTTPException:
        session.rollback()
        raise
    except SQLAlchemyError as exc:
        session.rollback()
        if tratativa is not None:
            tratativa.status = 'FALHA'
            session_postgres.commit()
        raise HTTPException(
            status_code=HTTPStatus.SERVICE_UNAVAILABLE,
            detail='Não foi possível registrar a confirmação no MV.',
        ) from exc


def _tratativa_dict(item: AgendaMedicaTratativaPaciente) -> dict:
    return {
        'id': item.id,
        'cd_it_agenda_central': item.cd_it_agenda_central,
        'cd_agenda_central': item.cd_agenda_central,
        'solicitacao_id': item.solicitacao_id,
        'tipo': item.tipo,
        'status': item.status,
        'cd_it_agenda_destino': item.cd_it_agenda_destino,
        'valor_anterior': item.valor_anterior,
        'valor_novo': item.valor_novo,
        'motivo': item.motivo,
        'criado_por': item.criado_por_nome,
        'criado_em': item.criado_em.isoformat() if item.criado_em else None,
        'concluido_por': item.concluido_por_nome,
        'concluido_em': (
            item.concluido_em.isoformat() if item.concluido_em else None
        ),
    }


@router.get('/agendamentos/tratativas')
def listar_tratativas_paciente(
    usuario: UsuarioOperacional,
    session: SessionPostgres,
    tipo: Literal['NAO_VIRA', 'REMANEJAMENTO', 'CORRECAO_CONFIRMACAO'] | None = None,
    status: Literal['PENDENTE', 'EM_TRATATIVA', 'CONCLUIDA', 'CANCELADA', 'FALHA'] | None = None,
    limite: int = Query(default=100, ge=1, le=500),
):
    _exigir_acesso_confirmacoes(usuario)
    consulta = select(AgendaMedicaTratativaPaciente).order_by(
        AgendaMedicaTratativaPaciente.id.desc()
    )
    if tipo:
        consulta = consulta.where(AgendaMedicaTratativaPaciente.tipo == tipo)
    if status:
        consulta = consulta.where(AgendaMedicaTratativaPaciente.status == status)
    itens = session.scalars(consulta.limit(limite)).all()
    return {'tratativas': [_tratativa_dict(item) for item in itens]}


@router.put('/agendamentos/tratativas/{tratativa_id}')
def atualizar_tratativa_paciente(
    tratativa_id: int,
    payload: TratativaPacienteInput,
    usuario: UsuarioOperacional,
    session: SessionPostgres,
    session_oracle: SessionOracle,
):
    _exigir_acesso_confirmacoes(usuario, escrita=True)
    item = session.get(AgendaMedicaTratativaPaciente, tratativa_id)
    if item is None:
        raise HTTPException(
            status_code=HTTPStatus.NOT_FOUND,
            detail='Tratativa não encontrada.',
        )
    if item.status in {'CONCLUIDA', 'CANCELADA'}:
        raise HTTPException(
            status_code=HTTPStatus.CONFLICT,
            detail='Esta tratativa já foi encerrada.',
        )
    if (
        item.tipo == 'REMANEJAMENTO'
        and payload.status == 'CONCLUIDA'
        and payload.cd_it_agenda_destino is None
    ):
        raise HTTPException(
            status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
            detail='Informe o horário de destino do remanejamento.',
        )
    if payload.status == 'CONCLUIDA' and item.tipo in {
        'NAO_VIRA',
        'REMANEJAMENTO',
    }:
        origem = session_oracle.execute(
            text(
                """
                SELECT CD_PACIENTE AS cd_paciente,
                       NVL(TP_SITUACAO, 'M') AS tp_situacao
                  FROM DBAMV.IT_AGENDA_CENTRAL
                 WHERE CD_IT_AGENDA_CENTRAL = :cd_it
                """
            ),
            {'cd_it': item.cd_it_agenda_central},
        ).mappings().one_or_none()
        origem_liberada = origem is None or (
            origem['cd_paciente'] is None or origem['tp_situacao'] == 'C'
        )
        if not origem_liberada:
            raise HTTPException(
                status_code=HTTPStatus.CONFLICT,
                detail=(
                    'O horário original ainda está ocupado no SoulMV. '
                    'Conclua o cancelamento ou remanejamento antes de '
                    'encerrar a tratativa.'
                ),
            )
        if item.tipo == 'REMANEJAMENTO':
            destino = session_oracle.execute(
                text(
                    """
                    SELECT CD_PACIENTE AS cd_paciente,
                           NVL(TP_SITUACAO, 'M') AS tp_situacao
                      FROM DBAMV.IT_AGENDA_CENTRAL
                     WHERE CD_IT_AGENDA_CENTRAL = :cd_it
                    """
                ),
                {'cd_it': payload.cd_it_agenda_destino},
            ).mappings().one_or_none()
            if destino is None or destino['cd_paciente'] is None or destino['tp_situacao'] == 'C':
                raise HTTPException(
                    status_code=HTTPStatus.CONFLICT,
                    detail='O horário de destino não está ocupado no SoulMV.',
                )
    item.status = payload.status
    item.cd_it_agenda_destino = payload.cd_it_agenda_destino
    if payload.motivo:
        item.motivo = payload.motivo.strip()
    if payload.status in {'CONCLUIDA', 'CANCELADA'}:
        item.concluido_por_id = usuario.id
        item.concluido_por_nome = usuario.nome
        item.concluido_em = datetime.now(FUSO_HORARIO_BRASIL).replace(tzinfo=None)
    session.commit()
    session.refresh(item)
    return _tratativa_dict(item)



@router.get('/agendamentos')
def consultar_agendamentos(
    usuario: ValidaUsuarioAtual,
    session: SessionOracle,
    cd_paciente: int | None = Query(default=None, gt=0),
    modalidade: str | None = Query(default=None, pattern='^(consulta|exame)$'),
    cd_prestador: int | None = Query(default=None, gt=0),
    cd_unidade_atendimento: int | None = Query(default=None, gt=0),
    data_inicio: date | None = None,
    data_fim: date | None = None,
    limite: int = Query(default=100, ge=1, le=500),
):
    """Consulta marcações do MV com paciente, item, prestador e situação."""
    del usuario
    filtros = ['1 = 1']
    params: dict[str, object] = {'limite': limite}
    if cd_paciente:
        filtros.append('iac.CD_PACIENTE = :cd_paciente')
        params['cd_paciente'] = cd_paciente
    if cd_prestador:
        filtros.append('ac.CD_PRESTADOR = :cd_prestador')
        params['cd_prestador'] = cd_prestador
    if cd_unidade_atendimento:
        filtros.append('ac.CD_UNIDADE_ATENDIMENTO = :cd_unidade_atendimento')
        params['cd_unidade_atendimento'] = cd_unidade_atendimento
    if modalidade == 'consulta':
        filtros.append('ac.CD_PRESTADOR IS NOT NULL')
    elif modalidade == 'exame':
        filtros.append('ac.CD_PRESTADOR IS NULL')
    if data_inicio:
        filtros.append('iac.HR_AGENDA >= :data_inicio')
        params['data_inicio'] = data_inicio
    if data_fim:
        filtros.append('iac.HR_AGENDA < :data_fim + 1')
        params['data_fim'] = data_fim
    rows = session.execute(text(f'''
        SELECT iac.CD_IT_AGENDA_CENTRAL AS cd_it_agenda_central,
               iac.CD_AGENDA_CENTRAL AS cd_agenda_central,
               iac.CD_PACIENTE AS cd_paciente,
               p.NM_PACIENTE AS nm_paciente,
               iac.HR_AGENDA AS horario,
               ia.CD_ITEM_AGENDAMENTO AS cd_item_agendamento,
               ia.DS_ITEM_AGENDAMENTO AS ds_item_agendamento,
               tm.DS_TIP_MAR AS ds_tip_mar,
               pr.CD_PRESTADOR AS cd_prestador,
               pr.NM_PRESTADOR AS nm_prestador,
               ac.CD_UNIDADE_ATENDIMENTO AS cd_unidade_atendimento,
               ua.DS_UNIDADE_ATENDIMENTO AS ds_unidade_atendimento,
               ua.DS_LOCAL_UNIDADE_ATENDIMENTO AS ds_local_unidade_atendimento,
               CASE WHEN ac.CD_PRESTADOR IS NOT NULL THEN 'consulta' ELSE 'exame' END AS modalidade_agenda,
               iac.CD_TIP_MAR AS cd_tip_mar,
               iac.TP_SITUACAO AS status
          FROM DBAMV.IT_AGENDA_CENTRAL iac
          JOIN DBAMV.AGENDA_CENTRAL ac ON ac.CD_AGENDA_CENTRAL = iac.CD_AGENDA_CENTRAL
          JOIN DBAMV.AGENDA_CENTRAL_ITEM_AGENDA acia
            ON acia.CD_AGENDA_CENTRAL = ac.CD_AGENDA_CENTRAL
          LEFT JOIN DBAMV.PACIENTE p ON p.CD_PACIENTE = iac.CD_PACIENTE
          LEFT JOIN DBAMV.ITEM_AGENDAMENTO ia
            ON ia.CD_ITEM_AGENDAMENTO = acia.CD_ITEM_AGENDAMENTO
          LEFT JOIN DBAMV.TIP_MAR tm
           ON tm.CD_TIP_MAR = iac.CD_TIP_MAR
           LEFT JOIN DBAMV.PRESTADOR pr ON pr.CD_PRESTADOR = ac.CD_PRESTADOR
           LEFT JOIN DBAMV.UNIDADE_ATENDIMENTO ua
             ON ua.CD_UNIDADE_ATENDIMENTO = ac.CD_UNIDADE_ATENDIMENTO
         WHERE {' AND '.join(filtros)}
         ORDER BY iac.HR_AGENDA
         FETCH FIRST :limite ROWS ONLY
    '''), params).mappings().all()
    return {'agendamentos': [dict(row) for row in rows], 'total': len(rows)}


@router.get('/atendimentos')
def consultar_atendimentos(
    usuario: ValidaUsuarioAtual,
    session: SessionOracle,
    cd_paciente: int | None = Query(default=None, gt=0),
    tipo_atendimento: str | None = Query(default=None, pattern='^[AEUI]$'),
    data_inicio: date | None = None,
    data_fim: date | None = None,
    limite: int = Query(default=100, ge=1, le=500),
):
    """Consulta atendimentos realizados no MV para controle de comparecimento."""
    del usuario
    filtros = ['1 = 1']
    params: dict[str, object] = {'limite': limite}
    if cd_paciente:
        filtros.append('a.CD_PACIENTE = :cd_paciente')
        params['cd_paciente'] = cd_paciente
    if tipo_atendimento:
        filtros.append('a.TP_ATENDIMENTO = :tipo_atendimento')
        params['tipo_atendimento'] = tipo_atendimento
    if data_inicio:
        filtros.append('a.HR_ATENDIMENTO >= :data_inicio')
        params['data_inicio'] = data_inicio
    if data_fim:
        filtros.append('a.HR_ATENDIMENTO < :data_fim + 1')
        params['data_fim'] = data_fim
    rows = session.execute(text(f'''
        SELECT a.CD_ATENDIMENTO AS cd_atendimento,
               a.CD_PACIENTE AS cd_paciente,
               p.NM_PACIENTE AS nm_paciente,
               a.HR_ATENDIMENTO AS horario_atendimento,
               a.TP_ATENDIMENTO AS tipo_atendimento,
               a.CD_PRESTADOR AS cd_prestador,
               pr.NM_PRESTADOR AS nm_prestador,
               a.DT_ALTA AS data_alta
          FROM DBAMV.ATENDIME a
          LEFT JOIN DBAMV.PACIENTE p ON p.CD_PACIENTE = a.CD_PACIENTE
          LEFT JOIN DBAMV.PRESTADOR pr ON pr.CD_PRESTADOR = a.CD_PRESTADOR
         WHERE {' AND '.join(filtros)}
         ORDER BY a.HR_ATENDIMENTO DESC
         FETCH FIRST :limite ROWS ONLY
    '''), params).mappings().all()
    return {'atendimentos': [dict(row) for row in rows], 'total': len(rows)}


@router.get('/absenteismo')
def consultar_absenteismo(
    usuario: ValidaUsuarioAtual,
    session: SessionOracle,
    cd_paciente: int | None = Query(default=None, gt=0),
    modalidade: str | None = Query(default=None, pattern='^(consulta|exame)$'),
    cd_prestador: int | None = Query(default=None, gt=0),
    cd_unidade_atendimento: int | None = Query(default=None, gt=0),
    data_inicio: date | None = None,
    data_fim: date | None = None,
    limite: int = Query(default=500, ge=1, le=1000),
):
    """Cruza marcações com atendimentos ambulatoriais (TP_ATENDIMENTO = A).

    A comparação é feita pelo paciente e pelo dia da agenda. Uma marcação
    com pelo menos um atendimento ambulatorial no mesmo dia é considerada
    comparecida; as demais são retornadas como absenteísmo potencial.
    """
    del usuario
    filtros = ['iac.CD_PACIENTE IS NOT NULL']
    params: dict[str, object] = {'limite': limite}
    if cd_paciente:
        filtros.append('iac.CD_PACIENTE = :cd_paciente')
        params['cd_paciente'] = cd_paciente
    if cd_prestador:
        filtros.append('ac.CD_PRESTADOR = :cd_prestador')
        params['cd_prestador'] = cd_prestador
    if cd_unidade_atendimento:
        filtros.append('ac.CD_UNIDADE_ATENDIMENTO = :cd_unidade_atendimento')
        params['cd_unidade_atendimento'] = cd_unidade_atendimento
    if modalidade == 'consulta':
        filtros.append('ac.CD_PRESTADOR IS NOT NULL')
    elif modalidade == 'exame':
        filtros.append('ac.CD_PRESTADOR IS NULL')
    if data_inicio:
        filtros.append('iac.HR_AGENDA >= :data_inicio')
        params['data_inicio'] = data_inicio
    if data_fim:
        filtros.append('iac.HR_AGENDA < :data_fim + 1')
        params['data_fim'] = data_fim
    rows = session.execute(text(f'''
        SELECT iac.CD_IT_AGENDA_CENTRAL AS cd_it_agenda_central,
               iac.CD_AGENDA_CENTRAL AS cd_agenda_central,
               iac.CD_PACIENTE AS cd_paciente,
               p.NM_PACIENTE AS nm_paciente,
               iac.HR_AGENDA AS horario,
               ia.CD_ITEM_AGENDAMENTO AS cd_item_agendamento,
               ia.DS_ITEM_AGENDAMENTO AS ds_item_agendamento,
               tm.DS_TIP_MAR AS ds_tip_mar,
               pr.CD_PRESTADOR AS cd_prestador,
               pr.NM_PRESTADOR AS nm_prestador,
               ac.CD_UNIDADE_ATENDIMENTO AS cd_unidade_atendimento,
               ua.DS_UNIDADE_ATENDIMENTO AS ds_unidade_atendimento,
               ua.DS_LOCAL_UNIDADE_ATENDIMENTO AS ds_local_unidade_atendimento,
               CASE WHEN ac.CD_PRESTADOR IS NOT NULL THEN 'consulta' ELSE 'exame' END AS modalidade_agenda,
               iac.TP_SITUACAO AS status,
               CASE WHEN EXISTS (
                   SELECT 1
                     FROM DBAMV.ATENDIME a
                    WHERE a.CD_PACIENTE = iac.CD_PACIENTE
                       AND a.TP_ATENDIMENTO = CASE
                           WHEN ac.CD_PRESTADOR IS NOT NULL THEN 'A' ELSE 'E' END
                      AND TRUNC(a.HR_ATENDIMENTO) = TRUNC(iac.HR_AGENDA)
               ) THEN 'Compareceu' ELSE 'Possível absenteísmo' END AS situacao_absenteismo
          FROM DBAMV.IT_AGENDA_CENTRAL iac
          JOIN DBAMV.AGENDA_CENTRAL ac
            ON ac.CD_AGENDA_CENTRAL = iac.CD_AGENDA_CENTRAL
          JOIN DBAMV.AGENDA_CENTRAL_ITEM_AGENDA acia
            ON acia.CD_AGENDA_CENTRAL = ac.CD_AGENDA_CENTRAL
          LEFT JOIN DBAMV.PACIENTE p
            ON p.CD_PACIENTE = iac.CD_PACIENTE
          LEFT JOIN DBAMV.ITEM_AGENDAMENTO ia
            ON ia.CD_ITEM_AGENDAMENTO = acia.CD_ITEM_AGENDAMENTO
          LEFT JOIN DBAMV.TIP_MAR tm
            ON tm.CD_TIP_MAR = iac.CD_TIP_MAR
          LEFT JOIN DBAMV.PRESTADOR pr
            ON pr.CD_PRESTADOR = ac.CD_PRESTADOR
          LEFT JOIN DBAMV.UNIDADE_ATENDIMENTO ua
            ON ua.CD_UNIDADE_ATENDIMENTO = ac.CD_UNIDADE_ATENDIMENTO
         WHERE {' AND '.join(filtros)}
         ORDER BY iac.HR_AGENDA
         FETCH FIRST :limite ROWS ONLY
    '''), params).mappings().all()
    items = [dict(row) for row in rows]
    faltas = sum(
        1 for item in items
        if item['situacao_absenteismo'] == 'Possível absenteísmo'
    )
    comparecimentos = len(items) - faltas
    return {
        'absenteismos': items,
        'total_agendados': len(items),
        'total_compareceram': comparecimentos,
        'total_absenteismo': faltas,
        'taxa_absenteismo': round(faltas / len(items) * 100, 2)
        if items else 0,
    }
