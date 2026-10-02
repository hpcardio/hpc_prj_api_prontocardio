from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from hashlib import sha256
from http import HTTPStatus
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from app_prontocardio.database import get_session_oracle
from app_prontocardio.models import Usuario
from app_prontocardio.security import (
    valida_token_usuario_atual,
    valida_usuario_ti,
)

router = APIRouter(prefix='/repasse-medico', tags=['repasse-medico'])
SessionOracle = Annotated[Session, Depends(get_session_oracle)]
UsuarioAtual = Annotated[Usuario, Depends(valida_token_usuario_atual)]
UsuarioTi = Annotated[Usuario, Depends(valida_usuario_ti)]


class EnvioRepasseInput(BaseModel):
    competencia: date
    data_pagamento: date
    empresa: int = Field(gt=0)
    token_confirmacao: str = Field(min_length=64, max_length=64)


PREVIEW_QUERY = text("""
SELECT rp.cd_repasse,
       rp.cd_prestador,
       NVL(rp.cd_prestador_repasse, rp.cd_prestador)
           AS cd_prestador_destino,
       MAX(NVL(pd.nm_prestador, po.nm_prestador))
           AS nm_prestador_destino,
       NVL(rc.cd_procedimento, rc.cd_pro_fat) AS cd_pro_fat,
       MAX(NVL(rc.ds_procedimento, rc.ds_pro_fat)) AS ds_procedimento,
       CASE
           WHEN COUNT(rc.cd_repasse_consolidado) = 0 THEN 1
           ELSE COUNT(DISTINCT rc.cd_repasse_consolidado)
       END AS quantidade,
       CASE
           WHEN COUNT(rc.cd_repasse_consolidado) = 0
               THEN MAX(NVL(rp.vl_repasse, 0))
           ELSE SUM(NVL(rc.vl_repasse, 0))
       END AS valor_repasse,
       CASE
           WHEN COUNT(rc.cd_repasse_consolidado) = 0
               THEN MAX(NVL(rp.vl_desconto, 0))
           ELSE SUM(NVL(rc.vl_glosa, 0))
       END AS valor_desconto
  FROM dbamv.repasse_prestador rp
  JOIN dbamv.repasse r
    ON r.cd_repasse = rp.cd_repasse
  JOIN dbamv.prestador po
    ON po.cd_prestador = rp.cd_prestador
  LEFT JOIN dbamv.prestador pd
    ON pd.cd_prestador = rp.cd_prestador_repasse
  LEFT JOIN dbamv.repasse_consolidado rc
    ON rc.cd_repasse = rp.cd_repasse
   AND rc.cd_prestador = rp.cd_prestador
 WHERE rp.cd_con_pag IS NULL
   AND r.cd_multi_empresa = :empresa
   AND r.dt_competencia >= :competencia
   AND r.dt_competencia < ADD_MONTHS(:competencia, 1)
   AND (
       :cd_prestador IS NULL
       OR NVL(rp.cd_prestador_repasse, rp.cd_prestador) = :cd_prestador
   )
 GROUP BY rp.cd_repasse,
          rp.cd_prestador,
          NVL(rp.cd_prestador_repasse, rp.cd_prestador),
          NVL(rc.cd_procedimento, rc.cd_pro_fat)
 ORDER BY nm_prestador_destino, ds_procedimento, rp.cd_repasse
""")

PRESTADOR_POR_CPF_QUERY = text("""
SELECT cd_prestador, nm_prestador
  FROM dbamv.prestador
 WHERE REGEXP_REPLACE(NVL(nr_cpf_cgc, ''), '[^0-9]', '') = :cpf_prestador
   AND NVL(tp_situacao, 'A') = 'A'
 ORDER BY cd_prestador
""")

EXECUTAR_PROCEDURE = text("""
BEGIN
    DBAMV.PGTO_REP_GERAL(
        :competencia,
        :data_pagamento,
        :empresa
    );
END;
""")


def _decimal(valor) -> Decimal:
    return Decimal(str(valor or 0)).quantize(Decimal('0.01'))


def _eh_teste_ergometrico(descricao: str | None) -> bool:
    texto = (descricao or '').upper()
    return 'TESTE ERGOMETRICO' in texto or 'TESTE ERGOMÉTRICO' in texto


def _token_snapshot(competencia: date, empresa: int, rows: list[dict]) -> str:
    dados = {
        'competencia': competencia.replace(day=1).isoformat(),
        'empresa': empresa,
        'linhas': [
            {
                chave: str(row.get(chave) or '')
                for chave in (
                    'cd_repasse',
                    'cd_prestador',
                    'cd_prestador_destino',
                    'cd_pro_fat',
                    'quantidade',
                    'valor_repasse',
                    'valor_desconto',
                )
            }
            for row in rows
        ],
    }
    serializado = json.dumps(
        dados, ensure_ascii=True, sort_keys=True, separators=(',', ':')
    )
    return sha256(serializado.encode()).hexdigest()


def montar_preview(
    session: Session,
    competencia: date,
    empresa: int,
    cpf_prestador: str | None = None,
) -> dict:
    competencia = competencia.replace(day=1)
    filtro_prestador = None
    cd_prestador = None
    if cpf_prestador:
        cpf_normalizado = ''.join(char for char in cpf_prestador if char.isdigit())
        prestadores = [
            dict(row)
            for row in session.execute(
                PRESTADOR_POR_CPF_QUERY,
                {'cpf_prestador': cpf_normalizado},
            ).mappings().all()
        ]
        if not prestadores:
            raise HTTPException(
                status_code=HTTPStatus.NOT_FOUND,
                detail='CPF do médico não localizado entre os prestadores ativos do MV.',
            )
        if len(prestadores) > 1:
            raise HTTPException(
                status_code=HTTPStatus.CONFLICT,
                detail='CPF associado a mais de um prestador ativo no MV.',
            )
        cd_prestador = prestadores[0]['cd_prestador']
        filtro_prestador = {
            'codigo_prestador': cd_prestador,
            'prestador': prestadores[0]['nm_prestador'],
        }
    rows = [
        dict(row)
        for row in session.execute(
            PREVIEW_QUERY,
            {
                'competencia': competencia,
                'empresa': empresa,
                'cd_prestador': cd_prestador,
            },
        )
        .mappings()
        .all()
    ]
    itens = []
    excluidos_quantidade = 0
    excluidos_valor = Decimal('0')
    total = Decimal('0')
    for row in rows:
        bruto = _decimal(row.get('valor_repasse'))
        desconto = _decimal(row.get('valor_desconto'))
        liquido = bruto - desconto
        if _eh_teste_ergometrico(row.get('ds_procedimento')):
            excluidos_quantidade += int(row.get('quantidade') or 0)
            excluidos_valor += liquido
            continue
        total += liquido
        itens.append(
            {
                'codigo_repasse': row.get('cd_repasse'),
                'codigo_prestador': row.get('cd_prestador_destino'),
                'prestador': row.get('nm_prestador_destino'),
                'codigo_procedimento': row.get('cd_pro_fat'),
                'procedimento': row.get('ds_procedimento')
                or 'Sem detalhamento',
                'quantidade': int(row.get('quantidade') or 0),
                'valor_bruto': bruto,
                'valor_desconto': desconto,
                'valor_liquido': liquido,
            }
        )
    return {
        'competencia': competencia,
        'empresa': empresa,
        'itens': itens,
        'total_liquido': total,
        'bloqueado': excluidos_quantidade > 0,
        'motivo_bloqueio': (
            'Há teste ergométrico no lote pendente do MV. A procedure '
            'existente processa a competência inteira e não permite '
            'excluir somente esse exame.'
            if excluidos_quantidade
            else None
        ),
        'excluidos_teste_ergometrico': {
            'quantidade': excluidos_quantidade,
            'valor': excluidos_valor,
        },
        'token_confirmacao': _token_snapshot(competencia, empresa, rows),
        'filtro_prestador': filtro_prestador,
    }


@router.get('/preview')
def consultar_preview(
    usuario: UsuarioAtual,
    session: SessionOracle,
    competencia: date = Query(),
    empresa: int = Query(default=1, gt=0),
    cpf_prestador: str | None = Query(default=None, min_length=11),
):
    return montar_preview(session, competencia, empresa, cpf_prestador)


@router.post('/enviar')
def enviar_repasse(
    payload: EnvioRepasseInput,
    usuario: UsuarioTi,
    session: SessionOracle,
):
    preview = montar_preview(session, payload.competencia, payload.empresa)
    if preview['token_confirmacao'] != payload.token_confirmacao:
        raise HTTPException(
            status_code=HTTPStatus.CONFLICT,
            detail=(
                'O lote pendente mudou desde a conferência. Atualize o '
                'extrato antes de confirmar novamente.'
            ),
        )
    if preview['bloqueado']:
        raise HTTPException(
            status_code=HTTPStatus.CONFLICT,
            detail=preview['motivo_bloqueio'],
        )
    if not preview['itens']:
        raise HTTPException(
            status_code=HTTPStatus.CONFLICT,
            detail='Não há repasses pendentes para enviar nesta competência.',
        )

    try:
        session.execute(
            EXECUTAR_PROCEDURE,
            {
                'competencia': payload.competencia.replace(day=1),
                'data_pagamento': payload.data_pagamento,
                'empresa': payload.empresa,
            },
        )
        session.commit()
    except Exception:
        session.rollback()
        raise
    return {
        'status': 'enviado',
        'competencia': payload.competencia.replace(day=1),
        'data_pagamento': payload.data_pagamento,
        'empresa': payload.empresa,
        'total_liquido_conferido': preview['total_liquido'],
        'procedimento': 'DBAMV.PGTO_REP_GERAL',
    }
