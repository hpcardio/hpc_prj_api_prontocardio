from __future__ import annotations

import os
from datetime import date
from decimal import Decimal
from http import HTTPStatus
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.orm import Session

from app_prontocardio.database import get_session_oracle
from app_prontocardio.models import Usuario
from app_prontocardio.security import valida_token_usuario_atual

router = APIRouter(prefix='/angio-tc', tags=['angio-tc'])
SessionOracle = Annotated[Session, Depends(get_session_oracle)]
UsuarioAtual = Annotated[Usuario, Depends(valida_token_usuario_atual)]

PROCEDIMENTO_ANGIO_TC = '41001230'
PRESTADORES_ANGIO_TC = frozenset({245, 356, 895, 1255})
COMPETENCIA_INICIAL = date(2026, 7, 1)

PRESTADOR_POR_CPF_QUERY = text("""
SELECT cd_prestador, nm_prestador
  FROM dbamv.prestador
 WHERE REGEXP_REPLACE(NVL(nr_cpf_cgc, ''), '[^0-9]', '') = :cpf_prestador
   AND NVL(tp_situacao, 'A') = 'A'
   AND cd_prestador IN (245, 356, 895, 1255)
 ORDER BY cd_prestador
""")

LANCAMENTOS_QUERY = text("""
SELECT rc.cd_repasse_consolidado,
       NVL(rp.cd_prestador_repasse, rp.cd_prestador)
           AS cd_prestador_destino,
       NVL(pd.nm_prestador, po.nm_prestador) AS nm_prestador_destino,
       rc.cd_atendimento,
       rc.nm_paciente,
       NVL(rc.hr_lancamento, rc.dt_lancamento) AS dt_lancamento,
       NVL(rc.cd_procedimento, rc.cd_pro_fat) AS cd_pro_fat,
       NVL(rc.ds_procedimento, rc.ds_pro_fat) AS ds_procedimento,
       rc.cd_convenio,
       rc.nm_convenio,
       rc.cd_remessa,
       rf.nr_remessa_convenio,
       NVL(rc.vl_total_conta, 0) AS vl_total_conta
  FROM dbamv.repasse_prestador rp
  JOIN dbamv.repasse r
    ON r.cd_repasse = rp.cd_repasse
  JOIN dbamv.prestador po
    ON po.cd_prestador = rp.cd_prestador
  LEFT JOIN dbamv.prestador pd
    ON pd.cd_prestador = rp.cd_prestador_repasse
  JOIN dbamv.repasse_consolidado rc
    ON rc.cd_repasse = rp.cd_repasse
   AND rc.cd_prestador = rp.cd_prestador
  LEFT JOIN dbamv.remessa_fatura rf
    ON rf.cd_remessa = rc.cd_remessa
 WHERE rp.cd_con_pag IS NULL
   AND r.cd_multi_empresa = :empresa
   AND r.dt_competencia >= :competencia
   AND r.dt_competencia < ADD_MONTHS(:competencia, 1)
   AND NVL(rc.cd_procedimento, rc.cd_pro_fat) = '41001230'
   AND NVL(rp.cd_prestador_repasse, rp.cd_prestador)
       IN (245, 356, 895, 1255)
   AND (
       :cd_prestador IS NULL
       OR NVL(rp.cd_prestador_repasse, rp.cd_prestador) = :cd_prestador
   )
 ORDER BY nm_prestador_destino,
          rc.dt_lancamento,
          rc.cd_repasse_consolidado
""")


def angio_tc_dedicado_ativo() -> bool:
    return os.getenv(
        'ANGIO_TC_DEDICATED_EXTRACT_ENABLED', ''
    ).strip().casefold() in {'1', 'true', 'yes', 'on'}


def _valor_decimal(valor) -> str:
    return format(Decimal(str(valor or 0)).quantize(Decimal('0.01')), '.2f')


def _resolver_prestador(
    session: Session, cpf_prestador: str | None
) -> tuple[int | None, dict | None]:
    if not cpf_prestador:
        return None, None
    cpf_normalizado = ''.join(
        char for char in cpf_prestador if char.isdigit()
    )
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
            detail='CPF não localizado entre os prestadores AngioTC ativos.',
        )
    if len(prestadores) > 1:
        raise HTTPException(
            status_code=HTTPStatus.CONFLICT,
            detail='CPF associado a mais de um prestador AngioTC ativo.',
        )
    prestador = prestadores[0]
    codigo = int(prestador['cd_prestador'])
    return codigo, {
        'codigo_prestador': codigo,
        'prestador': prestador['nm_prestador'],
    }


def montar_lancamentos(
    session: Session,
    competencia: date,
    empresa: int,
    cpf_prestador: str | None = None,
) -> dict:
    competencia = competencia.replace(day=1)
    if competencia < COMPETENCIA_INICIAL:
        return {
            'competencia': competencia,
            'empresa': empresa,
            'prestador_filtro': None,
            'itens': [],
        }

    cd_prestador, prestador_filtro = _resolver_prestador(
        session, cpf_prestador
    )
    rows = [
        dict(row)
        for row in session.execute(
            LANCAMENTOS_QUERY,
            {
                'competencia': competencia,
                'empresa': empresa,
                'cd_prestador': cd_prestador,
            },
        ).mappings().all()
    ]
    itens = []
    for row in rows:
        codigo_procedimento = str(row.get('cd_pro_fat') or '')
        codigo_prestador = int(row.get('cd_prestador_destino') or 0)
        if (
            codigo_procedimento != PROCEDIMENTO_ANGIO_TC
            or codigo_prestador not in PRESTADORES_ANGIO_TC
        ):
            continue
        codigo_consolidado = row.get('cd_repasse_consolidado')
        motivo_excecao = (
            None
            if codigo_consolidado is not None
            else 'CD_REPASSE_CONSOLIDADO ausente.'
        )
        itens.append({
            'codigo_repasse_consolidado': codigo_consolidado,
            'codigo_prestador': codigo_prestador,
            'prestador': row.get('nm_prestador_destino'),
            'codigo_atendimento': row.get('cd_atendimento'),
            'paciente': row.get('nm_paciente'),
            'data_atendimento': row.get('dt_lancamento'),
            'codigo_procedimento': codigo_procedimento,
            'procedimento': row.get('ds_procedimento') or 'Sem detalhamento',
            'codigo_convenio': row.get('cd_convenio'),
            'convenio': row.get('nm_convenio'),
            'codigo_remessa': row.get('cd_remessa'),
            'numero_remessa': row.get('nr_remessa_convenio'),
            'situacao_remessa': (
                'inside' if row.get('cd_remessa') is not None else 'outside'
            ),
            'valor_faturado': _valor_decimal(row.get('vl_total_conta')),
            'elegivel_financeiro': codigo_consolidado is not None,
            'motivo_excecao': motivo_excecao,
        })
    return {
        'competencia': competencia,
        'empresa': empresa,
        'prestador_filtro': prestador_filtro,
        'itens': itens,
    }


@router.get('/lancamentos')
def consultar_lancamentos(
    usuario: UsuarioAtual,
    session: SessionOracle,
    competencia: date = Query(),
    empresa: int = Query(default=1, gt=0),
    cpf_prestador: str | None = Query(default=None, min_length=11),
):
    return montar_lancamentos(
        session, competencia, empresa, cpf_prestador
    )
