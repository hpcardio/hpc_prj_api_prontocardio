# ruff: noqa
# Recuperado para o Git em 24/11/2026: em produção desde 23/11 fora do
# versionamento (container api-prontocardio-reconciliation-*-v11).
from datetime import date, datetime, timezone
from decimal import Decimal
from http import HTTPStatus
import os
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app_prontocardio.database import get_session_oracle
from app_prontocardio.models import Usuario
from app_prontocardio.security import valida_token_usuario_atual
from app_prontocardio.reconciliacao_origem_mv import (
    AlteracaoOrigemMvError,
    ConflitoOrigemMv,
    ProtecaoFinanceiraOrigemMv,
    aplicar_alteracao_origem_mv,
    pre_validar_alteracao_origem_mv,
)


router = APIRouter(
    prefix='/reconciliacao-rede',
    tags=['reconciliacao-rede'],
)
SessionOracle = Annotated[Session, Depends(get_session_oracle)]
UsuarioAtual = Annotated[Usuario, Depends(valida_token_usuario_atual)]


class CodigoNome(BaseModel):
    codigo: int | str | None = None
    nome: str | None = None


class Convenio(CodigoNome):
    plano: str | None = None


class OrigemMv(BaseModel):
    codigo: int | None = None
    descricao: str | None = None


class ProtecaoFinanceira(BaseModel):
    conta_fechada: bool | None
    auditoria_pendente: bool | None
    liberada: bool | None
    remessa_ativa: bool | None
    paga: bool | None
    versao: str
    atualizada_em: datetime


class AtendimentoReconciliacao(BaseModel):
    atendimento: int
    paciente: CodigoNome
    data_atendimento: datetime | None = None
    tipo_atendimento: str | None = None
    valor_bruto: Decimal | None = None
    convenio: Convenio
    origem_mv: OrigemMv
    origem_agendamento: Literal['MV', 'NAO_IDENTIFICADA']
    evidencia_agendamento: str | None = None
    usuarios_agendamento: list[str] = []
    observacoes_agendamento: list[str] = []
    clinica_atual: CodigoNome | None = None
    clinica_sugerida: CodigoNome | None = None
    estado_reconciliacao: str | None = None
    evidencias: list[str] = []
    protecao_financeira: ProtecaoFinanceira


class PaginaReconciliacao(BaseModel):
    items: list[AtendimentoReconciliacao]
    next_cursor: str | None = None
    total: int
    total_valor_bruto: Decimal
    atendimentos_filtrados: list[int]
    atualizado_em: datetime
    fonte_status: Literal['online']


class ListaConveniosReconciliacao(BaseModel):
    items: list[CodigoNome]


class AlteracaoOrigemMvInput(BaseModel):
    origem_anterior: int
    origem_nova: int
    justificativa: str
    solicitante: str
    aprovador: str | None = None
    chave_idempotencia: str


def _write_origem_habilitada() -> bool:
    return os.getenv('RECONCILIACAO_ORIGEM_MV_WRITE_ENABLED', '').strip().lower() in {
        '1', 'true', 'yes', 'sim', 'on'
    }


def _validar_metadados_alteracao(payload: AlteracaoOrigemMvInput) -> None:
    if len(payload.justificativa.strip()) < 10:
        raise HTTPException(
            status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
            detail='A justificativa deve ter pelo menos 10 caracteres.',
        )
    if not payload.solicitante.strip() or not payload.chave_idempotencia.strip():
        raise HTTPException(
            status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
            detail='Solicitante e chave de idempotência são obrigatórios.',
        )


def _traduzir_erro_alteracao(exc: AlteracaoOrigemMvError) -> HTTPException:
    if isinstance(exc, ProtecaoFinanceiraOrigemMv):
        return HTTPException(status_code=HTTPStatus.CONFLICT, detail=str(exc))
    if isinstance(exc, ConflitoOrigemMv):
        return HTTPException(status_code=HTTPStatus.CONFLICT, detail=str(exc))
    return HTTPException(status_code=HTTPStatus.UNPROCESSABLE_ENTITY, detail=str(exc))


@router.post('/atendimentos/{atendimento}/origem/pre-validar')
def pre_validar_origem_atendimento(
    atendimento: int,
    payload: AlteracaoOrigemMvInput,
    session: SessionOracle,
    _usuario: UsuarioAtual,
):
    _validar_metadados_alteracao(payload)
    try:
        return pre_validar_alteracao_origem_mv(
            session,
            atendimento=atendimento,
            origem_anterior=payload.origem_anterior,
            origem_nova=payload.origem_nova,
        )
    except AlteracaoOrigemMvError as exc:
        raise _traduzir_erro_alteracao(exc) from exc


@router.post('/atendimentos/{atendimento}/origem/aplicar')
def aplicar_origem_atendimento(
    atendimento: int,
    payload: AlteracaoOrigemMvInput,
    session: SessionOracle,
    _usuario: UsuarioAtual,
):
    _validar_metadados_alteracao(payload)
    if not _write_origem_habilitada():
        raise HTTPException(
            status_code=HTTPStatus.SERVICE_UNAVAILABLE,
            detail='Alteração de origem no MV ainda não está habilitada.',
        )
    if not payload.aprovador or not payload.aprovador.strip():
        raise HTTPException(
            status_code=HTTPStatus.FORBIDDEN,
            detail='A aprovação administrativa é obrigatória.',
        )
    try:
        return aplicar_alteracao_origem_mv(
            session,
            atendimento=atendimento,
            origem_anterior=payload.origem_anterior,
            origem_nova=payload.origem_nova,
        )
    except AlteracaoOrigemMvError as exc:
        raise _traduzir_erro_alteracao(exc) from exc


def _offset(cursor: str | None) -> int:
    if cursor is None:
        return 0
    try:
        value = int(cursor)
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
            detail='Cursor invalido.',
        ) from exc
    if value < 0:
        raise HTTPException(
            status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
            detail='Cursor invalido.',
        )
    return value


def _conta_fechada(value: object) -> bool | None:
    if value is None:
        return None
    normalized = str(value).strip().upper()
    if normalized in {'S', 'SIM', '1', 'TRUE', 'FECHADA'}:
        return True
    if normalized in {'N', 'NAO', 'NÃO', '0', 'FALSE', 'ABERTA'}:
        return False
    return None


def _value(row: object, key: str) -> object:
    mapping = row if isinstance(row, dict) else row
    return mapping.get(key, mapping.get(key.lower()))


TIPOS_ATENDIMENTO_MV = {
    'AMBULATORIAL': 'A',
    'URGENCIA': 'U',
    'INTERNACAO': 'I',
    'EXAME': 'E',
}
TIPOS_ATENDIMENTO_LABEL = {codigo: nome for nome, codigo in TIPOS_ATENDIMENTO_MV.items()}


@router.get('/convenios', response_model=ListaConveniosReconciliacao)
def listar_convenios_reconciliacao(
    session: SessionOracle,
    _usuario: UsuarioAtual,
    data_inicio: Annotated[date, Query()],
    data_fim: Annotated[date, Query()],
):
    dias = data_fim.toordinal() - data_inicio.toordinal() + 1
    if dias < 1:
        raise HTTPException(
            status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
            detail='A data final deve ser igual ou posterior à data inicial.',
        )
    if dias > 93:
        raise HTTPException(
            status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
            detail='O período máximo para consulta é de 93 dias.',
        )
    sql = text('''
        SELECT f.COD_CONVENIO CODIGO,
               NVL(TRIM(f.NOME_CONVENIO), 'Convênio ' || f.COD_CONVENIO) NOME
          FROM DBAMV.HPC_VFAT_TOTAL3 f
         WHERE f.DT_ATEND >= :data_inicio
           AND f.DT_ATEND < :data_fim_exclusiva
           AND f.COD_CONVENIO IS NOT NULL
         GROUP BY f.COD_CONVENIO, f.NOME_CONVENIO
         ORDER BY NOME, CODIGO
    ''')
    try:
        rows = session.execute(sql, {
            'data_inicio': data_inicio,
            'data_fim_exclusiva': date.fromordinal(data_fim.toordinal() + 1),
        }).mappings().all()
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=HTTPStatus.BAD_GATEWAY,
            detail='Falha ao consultar convênios no MV.',
        ) from exc
    unicos: dict[int, str] = {}
    for row in rows:
        codigo = int(_value(row, 'CODIGO'))
        unicos.setdefault(codigo, str(_value(row, 'NOME') or f'Convênio {codigo}'))
    return {'items': [{'codigo': codigo, 'nome': nome} for codigo, nome in unicos.items()]}


@router.get('/atendimentos', response_model=PaginaReconciliacao)
def listar_atendimentos_reconciliacao(
    session: SessionOracle,
    _usuario: UsuarioAtual,
    data_inicio: Annotated[date, Query()],
    data_fim: Annotated[date, Query()],
    busca: Annotated[str | None, Query(max_length=120)] = None,
    atendimento: Annotated[int | None, Query(gt=0)] = None,
    convenio: Annotated[int | None, Query()] = None,
    origem_mv: Annotated[int | None, Query()] = None,
    origens_mv: Annotated[str | None, Query(max_length=500)] = None,
    clinica_origens_mv: Annotated[str | None, Query(max_length=500)] = None,
    clinica_atendimentos: Annotated[str | None, Query(max_length=12000)] = None,
    origem_agendamento: Annotated[Literal['PRONTOCARDIO_REDE', 'MV', 'NAO_IDENTIFICADA'] | None, Query()] = None,
    agendamentos_rede_atendimentos: Annotated[str | None, Query(max_length=12000)] = None,
    clinica_id: Annotated[int | None, Query()] = None,
    tipo_atendimento: Annotated[str | None, Query(max_length=60)] = None,
    situacao_conta: Annotated[
        Literal['aberta', 'fechada'] | None,
        Query(),
    ] = None,
    estado_reconciliacao: Annotated[str | None, Query()] = None,
    cursor: Annotated[str | None, Query()] = None,
    limite: Annotated[int, Query(ge=1, le=100)] = 50,
    ordenacao: Annotated[
        Literal['data_desc_atendimento_desc'],
        Query(),
    ] = 'data_desc_atendimento_desc',
):
    del clinica_id, estado_reconciliacao, ordenacao
    if data_fim < data_inicio:
        raise HTTPException(
            status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
            detail='data_fim deve ser maior ou igual a data_inicio.',
        )

    clauses = ['f.DT_ATEND >= :data_inicio', 'f.DT_ATEND < :data_fim_exclusiva']
    params: dict[str, object] = {
        'data_inicio': data_inicio,
        'data_fim_exclusiva': date.fromordinal(data_fim.toordinal() + 1),
    }
    if busca:
        clauses.append(
            '(UPPER(f.NOME_PACIENTE) LIKE :busca OR '
            'TO_CHAR(f.COD_ATEND) LIKE :busca)'
        )
        params['busca'] = f'%{busca.strip().upper()}%'
    if atendimento is not None:
        clauses.append('f.COD_ATEND = :atendimento')
        params['atendimento'] = atendimento
    if convenio is not None:
        clauses.append('f.COD_CONVENIO = :convenio')
        params['convenio'] = convenio
    if origem_mv is not None:
        clauses.append('f.CD_ORI_ATE = :origem_mv')
        params['origem_mv'] = origem_mv
    if origens_mv:
        try:
            codigos_origem = sorted({int(item.strip()) for item in origens_mv.split(',') if item.strip()})
        except ValueError as exc:
            raise HTTPException(status_code=HTTPStatus.UNPROCESSABLE_ENTITY, detail='origens_mv invalido.') from exc
        if not codigos_origem or len(codigos_origem) > 50:
            raise HTTPException(status_code=HTTPStatus.UNPROCESSABLE_ENTITY, detail='origens_mv invalido.')
        binds = []
        for index, codigo in enumerate(codigos_origem):
            key = f'origem_lista_{index}'
            binds.append(f':{key}')
            params[key] = codigo
        clauses.append(f"f.CD_ORI_ATE IN ({', '.join(binds)})")
    if clinica_origens_mv or clinica_atendimentos:
        grupos_clinica = []
        if clinica_origens_mv:
            try:
                codigos = sorted({int(item.strip()) for item in clinica_origens_mv.split(',') if item.strip()})
            except ValueError as exc:
                raise HTTPException(status_code=HTTPStatus.UNPROCESSABLE_ENTITY, detail='clinica_origens_mv invalido.') from exc
            if not codigos or len(codigos) > 50:
                raise HTTPException(status_code=HTTPStatus.UNPROCESSABLE_ENTITY, detail='clinica_origens_mv invalido.')
            binds = []
            for index, codigo in enumerate(codigos):
                key = f'clinica_origem_{index}'
                binds.append(f':{key}')
                params[key] = codigo
            grupos_clinica.append(f"f.CD_ORI_ATE IN ({', '.join(binds)})")
        if clinica_atendimentos:
            try:
                atendimentos_clinica = sorted({int(item.strip()) for item in clinica_atendimentos.split(',') if item.strip()})
            except ValueError as exc:
                raise HTTPException(status_code=HTTPStatus.UNPROCESSABLE_ENTITY, detail='clinica_atendimentos invalido.') from exc
            if not atendimentos_clinica or len(atendimentos_clinica) > 1000:
                raise HTTPException(status_code=HTTPStatus.UNPROCESSABLE_ENTITY, detail='clinica_atendimentos invalido.')
            binds = []
            for index, codigo in enumerate(atendimentos_clinica):
                key = f'clinica_atendimento_{index}'
                binds.append(f':{key}')
                params[key] = codigo
            grupos_clinica.append(f"f.COD_ATEND IN ({', '.join(binds)})")
        clauses.append(f"({' OR '.join(grupos_clinica)})")
    atendimentos_rede = []
    if agendamentos_rede_atendimentos:
        try:
            atendimentos_rede = sorted({int(item.strip()) for item in agendamentos_rede_atendimentos.split(',') if item.strip()})
        except ValueError as exc:
            raise HTTPException(status_code=HTTPStatus.UNPROCESSABLE_ENTITY, detail='agendamentos_rede_atendimentos invalido.') from exc
        if len(atendimentos_rede) > 1000:
            raise HTTPException(status_code=HTTPStatus.UNPROCESSABLE_ENTITY, detail='agendamentos_rede_atendimentos invalido.')
    binds_rede = []
    for index, codigo in enumerate(atendimentos_rede):
        key = f'agenda_rede_atendimento_{index}'
        binds_rede.append(f':{key}')
        params[key] = codigo
    agenda_mv = 'EXISTS (SELECT 1 FROM DBAMV.IT_AGENDA_CENTRAL iac WHERE iac.CD_ATENDIMENTO = f.COD_ATEND)'
    fora_rede = f"f.COD_ATEND NOT IN ({', '.join(binds_rede)})" if binds_rede else '1 = 1'
    if origem_agendamento == 'PRONTOCARDIO_REDE':
        clauses.append(f"f.COD_ATEND IN ({', '.join(binds_rede)})" if binds_rede else '1 = 0')
    elif origem_agendamento == 'MV':
        clauses.extend([agenda_mv, fora_rede])
    elif origem_agendamento == 'NAO_IDENTIFICADA':
        clauses.extend([f'NOT {agenda_mv}', fora_rede])
    if tipo_atendimento:
        tipo_atendimento_mv = TIPOS_ATENDIMENTO_MV.get(tipo_atendimento.strip().upper())
        if tipo_atendimento_mv is None:
            raise HTTPException(
                status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
                detail='tipo_atendimento invalido.',
            )
        clauses.append('a.TP_ATENDIMENTO = :tipo_atendimento_mv')
        params['tipo_atendimento_mv'] = tipo_atendimento_mv
    if situacao_conta == 'fechada':
        clauses.append("UPPER(TRIM(f.CONTA_FECHADA)) IN ('S', 'SIM', '1', 'TRUE', 'FECHADA')")
    elif situacao_conta == 'aberta':
        clauses.append("UPPER(TRIM(f.CONTA_FECHADA)) IN ('N', 'NAO', 'NÃO', '0', 'FALSE', 'ABERTA')")

    where = ' AND '.join(clauses)
    offset = _offset(cursor)
    params.update({'inicio': offset + 1, 'fim': offset + limite})
    summary_sql = text(f'''
        SELECT f.COD_ATEND, SUM(NVL(f.VL_CONTA, 0)) VALOR_BRUTO
          FROM DBAMV.HPC_VFAT_TOTAL3 f
          JOIN DBAMV.ATENDIME a ON a.CD_ATENDIMENTO = f.COD_ATEND
         WHERE {where}
         GROUP BY f.COD_ATEND
    ''')
    items_sql = text(f'''
        SELECT * FROM (
            SELECT base.*, ROW_NUMBER() OVER (
                ORDER BY base.DT_ATEND DESC NULLS LAST,
                         base.COD_ATEND DESC
            ) rn
              FROM (
                SELECT
                    f.COD_ATEND,
                    MAX(f.COD_PAC) COD_PAC,
                    MAX(f.NOME_PACIENTE) NOME_PACIENTE,
                    MAX(f.DT_ATEND) DT_ATEND,
                    MAX(a.TP_ATENDIMENTO) TP_ATENDIMENTO,
                    SUM(NVL(f.VL_CONTA, 0)) VALOR_BRUTO,
                    MAX(f.COD_CONVENIO) COD_CONVENIO,
                    MAX(f.NOME_CONVENIO) NOME_CONVENIO,
                    MAX(f.CD_ORI_ATE) CD_ORI_ATE,
                    MAX(f.DS_ORI_ATE) DS_ORI_ATE,
                    MAX(f.CONTA_FECHADA) CONTA_FECHADA,
                    CASE WHEN EXISTS (
                        SELECT 1
                          FROM DBAMV.IT_AGENDA_CENTRAL iac
                         WHERE iac.CD_ATENDIMENTO = f.COD_ATEND
                    ) THEN 1 ELSE 0 END TEM_AGENDA_MV
                  FROM DBAMV.HPC_VFAT_TOTAL3 f
                  JOIN DBAMV.ATENDIME a ON a.CD_ATENDIMENTO = f.COD_ATEND
                 WHERE {where}
                 GROUP BY f.COD_ATEND
              ) base
        ) WHERE rn BETWEEN :inicio AND :fim
    ''')

    try:
        summary_rows = session.execute(summary_sql, params).mappings().all()
        atendimentos_filtrados = [int(_value(row, 'COD_ATEND')) for row in summary_rows]
        total = len(atendimentos_filtrados)
        total_valor_bruto = sum(
            (Decimal(str(_value(row, 'VALOR_BRUTO') or 0)) for row in summary_rows),
            Decimal('0'),
        )
        rows = session.execute(items_sql, params).mappings().all()
        codigos_pagina = [int(_value(row, 'COD_ATEND')) for row in rows]
        agenda_por_atendimento: dict[int, dict[str, set[str]]] = {}
        if codigos_pagina:
            agenda_binds = []
            agenda_params: dict[str, object] = {}
            for index, codigo in enumerate(codigos_pagina):
                key = f'agenda_detalhe_{index}'
                agenda_binds.append(f':{key}')
                agenda_params[key] = codigo
            agenda_details_sql = text(f'''
                SELECT DISTINCT
                       iac.CD_ATENDIMENTO,
                       TRIM(iac.CD_USUARIO) CD_USUARIO,
                       TRIM(iac.DS_OBSERVACAO) DS_OBSERVACAO,
                       TRIM(iac.DS_OBSERVACAO_GERAL) DS_OBSERVACAO_GERAL
                  FROM DBAMV.IT_AGENDA_CENTRAL iac
                 WHERE iac.CD_ATENDIMENTO IN ({', '.join(agenda_binds)})
            ''')
            agenda_rows = session.execute(
                agenda_details_sql,
                agenda_params,
            ).mappings().all()
            for agenda_row in agenda_rows:
                codigo = int(_value(agenda_row, 'CD_ATENDIMENTO'))
                detalhes = agenda_por_atendimento.setdefault(
                    codigo,
                    {'usuarios': set(), 'observacoes': set()},
                )
                usuario = _value(agenda_row, 'CD_USUARIO')
                if usuario and str(usuario).strip():
                    detalhes['usuarios'].add(str(usuario).strip())
                for campo in ('DS_OBSERVACAO', 'DS_OBSERVACAO_GERAL'):
                    observacao = _value(agenda_row, campo)
                    if observacao and str(observacao).strip():
                        detalhes['observacoes'].add(str(observacao).strip())
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=HTTPStatus.BAD_GATEWAY,
            detail='Falha ao consultar atendimentos no MV.',
        ) from exc

    now = datetime.now(timezone.utc)
    items = []
    for row in rows:
        tem_agenda_mv = bool(_value(row, 'TEM_AGENDA_MV'))
        detalhes_agendamento = agenda_por_atendimento.get(
            int(_value(row, 'COD_ATEND')),
            {'usuarios': set(), 'observacoes': set()},
        )
        items.append({
            'atendimento': _value(row, 'COD_ATEND'),
            'paciente': {
                'codigo': _value(row, 'COD_PAC'),
                'nome': _value(row, 'NOME_PACIENTE'),
            },
            'data_atendimento': _value(row, 'DT_ATEND'),
            'tipo_atendimento': TIPOS_ATENDIMENTO_LABEL.get(
                str(_value(row, 'TP_ATENDIMENTO') or '').strip().upper(),
                _value(row, 'TP_ATENDIMENTO'),
            ),
            'valor_bruto': _value(row, 'VALOR_BRUTO'),
            'convenio': {
                'codigo': _value(row, 'COD_CONVENIO'),
                'nome': _value(row, 'NOME_CONVENIO'),
                'plano': None,
            },
            'origem_mv': {
                'codigo': _value(row, 'CD_ORI_ATE'),
                'descricao': _value(row, 'DS_ORI_ATE'),
            },
            'origem_agendamento': 'MV' if tem_agenda_mv else 'NAO_IDENTIFICADA',
            'evidencia_agendamento': (
                'Atendimento vinculado a uma agenda do MV.'
                if tem_agenda_mv else None
            ),
            'usuarios_agendamento': sorted(detalhes_agendamento['usuarios']),
            'observacoes_agendamento': sorted(
                detalhes_agendamento['observacoes']
            ),
            'clinica_atual': None,
            'clinica_sugerida': None,
            'estado_reconciliacao': None,
            'evidencias': [],
            'protecao_financeira': {
                'conta_fechada': _conta_fechada(
                    _value(row, 'CONTA_FECHADA')
                ),
                'auditoria_pendente': None,
                'liberada': None,
                'remessa_ativa': None,
                'paga': None,
                'versao': 'hpc_vfat_total3-v1',
                'atualizada_em': now,
            },
        })

    next_offset = offset + len(items)
    return {
        'items': items,
        'next_cursor': str(next_offset) if next_offset < total else None,
        'total': total,
        'total_valor_bruto': total_valor_bruto,
        'atendimentos_filtrados': atendimentos_filtrados,
        'atualizado_em': now,
        'fonte_status': 'online',
    }
