import os
import secrets
from datetime import date, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session

from app_prontocardio.database import get_session_oracle

router = APIRouter(prefix='/integracoes/prontolaudo', tags=['prontolaudo'])
SessionOracle = Annotated[Session, Depends(get_session_oracle)]


def _validar_token(
    token: Annotated[str | None, Header(alias='X-Integration-Token')] = None,
) -> None:
    esperado = os.getenv('PRONTOLAUDO_INTEGRATION_TOKEN', '')
    if not esperado:
        raise HTTPException(status_code=503, detail='Integração não configurada.')
    if not token or not secrets.compare_digest(token, esperado):
        raise HTTPException(status_code=401, detail='Credencial inválida.')


def _idade(nascimento: date | datetime | None) -> int | None:
    if nascimento is None:
        return None
    nascimento_data = nascimento.date() if isinstance(nascimento, datetime) else nascimento
    hoje = date.today()
    return hoje.year - nascimento_data.year - (
        (hoje.month, hoje.day) < (nascimento_data.month, nascimento_data.day)
    )


def _unidade(tipo: str | None, unidade: str | None, origem: str | None) -> str | None:
    descricao = f'{unidade or ""} {origem or ""}'.upper()
    if 'UTI' in descricao or 'TERAPIA INTENSIVA' in descricao:
        return 'uti'
    if tipo in {'E', 'U'} or 'EMERG' in descricao or 'URG' in descricao:
        return 'emergencia'
    if tipo == 'I' or bool(unidade):
        return 'internado'
    return None


@router.get('/pedidos/{cd_ped_rx}', dependencies=[Depends(_validar_token)])
def consultar_pedido(cd_ped_rx: int, session: SessionOracle):
    row = session.execute(
        text(
            '''
            SELECT pr.CD_PED_RX AS pedido,
                   a.CD_ATENDIMENTO AS atendimento,
                   p.NM_PACIENTE AS nome,
                   p.DT_NASCIMENTO AS nascimento,
                   c.NM_CONVENIO AS convenio,
                   pr.HR_PEDIDO AS data_pedido,
                   a.TP_ATENDIMENTO AS tipo_atendimento,
                   ui.DS_UNID_INT AS unidade_internacao,
                   o.DS_ORI_ATE AS origem_atendimento
              FROM DBAMV.PED_RX pr
              JOIN DBAMV.ATENDIME a ON a.CD_ATENDIMENTO = pr.CD_ATENDIMENTO
              JOIN DBAMV.PACIENTE p ON p.CD_PACIENTE = a.CD_PACIENTE
              LEFT JOIN DBAMV.CONVENIO c
                ON c.CD_CONVENIO = NVL(pr.CD_CONVENIO, a.CD_CONVENIO)
              LEFT JOIN DBAMV.LEITO l ON l.CD_LEITO = a.CD_LEITO
              LEFT JOIN DBAMV.UNID_INT ui ON ui.CD_UNID_INT = l.CD_UNID_INT
              LEFT JOIN DBAMV.ORI_ATE o ON o.CD_ORI_ATE = a.CD_ORI_ATE
             WHERE pr.CD_PED_RX = :pedido
            '''
        ),
        {'pedido': cd_ped_rx},
    ).mappings().one_or_none()

    if row is None:
        raise HTTPException(status_code=404, detail='Pedido não encontrado no MV.')

    data_pedido = row['data_pedido']
    return {
        'pedido': str(row['pedido']),
        'atendimento': row['atendimento'],
        'nome': row['nome'],
        'idade': _idade(row['nascimento']),
        'convenio': row['convenio'],
        'data': data_pedido.date().isoformat() if data_pedido else '',
        'unidade': _unidade(
            row['tipo_atendimento'],
            row['unidade_internacao'],
            row['origem_atendimento'],
        ),
        'unidadeDescricao': row['unidade_internacao'] or row['origem_atendimento'],
    }
