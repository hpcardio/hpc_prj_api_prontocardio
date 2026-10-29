# ruff: noqa: E501, PLR0913

import re
from base64 import urlsafe_b64decode, urlsafe_b64encode
from binascii import Error as Base64Error
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from http import HTTPStatus
from math import ceil
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import bindparam, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app_prontocardio.database import get_session_oracle, get_session_postgres
from app_prontocardio.models import Usuario
from app_prontocardio.payables_priority import (
    age_level,
    build_matrix,
    classification,
)
from app_prontocardio.security import valida_token_usuario_atual

router = APIRouter(
    prefix='/app_glosas/financeiro/contas-a-pagar', tags=['contas-a-pagar']
)
SessionPostgres = Annotated[Session, Depends(get_session_postgres)]
SessionOracle = Annotated[Session, Depends(get_session_oracle)]
UsuarioAtual = Annotated[Usuario, Depends(valida_token_usuario_atual)]
CENTAVOS = Decimal('0.01')
STATUS_VALIDOS = {'PENDENTE', 'CONTATO', 'NEGOCIACAO', 'ACORDADO'}
FUSO_HOSPITAL = ZoneInfo('America/Sao_Paulo')
TOTAL_QUADRANTES = 25
MAX_IDENTIFICADOR_SEM_CADASTRO = 2000


def _hoje_hospital() -> date:
    return datetime.now(FUSO_HOSPITAL).date()


class TratamentoInput(BaseModel):
    critico: bool = False
    pagamento_imediato: Decimal = Field(default=Decimal('0'), ge=0)
    status: Literal['PENDENTE', 'CONTATO', 'NEGOCIACAO', 'ACORDADO'] = (
        'PENDENTE'
    )
    responsavel: str | None = Field(default=None, max_length=150)
    proxima_acao: str | None = Field(default=None, max_length=255)
    data_proxima_acao: date | None = None
    condicao_negociada: str | None = Field(default=None, max_length=4000)
    observacao: str | None = Field(default=None, max_length=4000)

    @field_validator(
        'responsavel', 'proxima_acao', 'condicao_negociada', 'observacao'
    )
    @classmethod
    def normalizar_texto(cls, value):
        return str(value).strip() or None if value is not None else None


class PagamentoTituloInput(BaseModel):
    data_pagamento: date
    valor_pago: Decimal = Field(gt=0, max_digits=15, decimal_places=2)
    banco: str = Field(min_length=1, max_length=150)
    agencia: str = Field(min_length=1, max_length=30)
    numero_conta: str = Field(min_length=1, max_length=50)
    observacao: str | None = Field(default=None, max_length=4000)

    @field_validator('banco', 'agencia', 'numero_conta')
    @classmethod
    def normalizar_origem_recurso(cls, value, info):
        value = str(value).strip()
        if not value:
            rotulos = {
                'banco': 'nome do banco',
                'agencia': 'agência',
                'numero_conta': 'número da conta',
            }
            raise ValueError(
                f'Informe {rotulos.get(info.field_name, info.field_name)} '
                'de origem do recurso.'
            )
        return value

    @field_validator('observacao')
    @classmethod
    def normalizar_observacao(cls, value):
        return str(value).strip() or None if value is not None else None


class CriticidadeFornecedorInput(BaseModel):
    critico: bool | None = None
    nivel_criticidade: int | None = Field(
        default=None, ge=1, le=5, strict=True
    )
    justificativa: str | None = Field(default=None, max_length=2000)

    @model_validator(mode='after')
    def validar_classificacao(self):
        if self.nivel_criticidade is None and self.critico is None:
            raise ValueError('Informe a classificação.')
        if self.nivel_criticidade is not None:
            if not self.justificativa or not self.justificativa.strip():
                raise ValueError('Informe a justificativa da classificação.')
            self.justificativa = self.justificativa.strip()
            self.critico = self.nivel_criticidade > 1
        return self


# NULL supplier codes are grouped by the MV payee name. Unnamed records
# remain separate by account (or parcel), never in one shared NULL bucket.
REFERENCIA_SEM_CADASTRO_SQL = """CASE WHEN v.codigo_do_fornecedor IS NULL THEN
    CASE WHEN TRIM(v.nome_fornecedor) IS NOT NULL
         THEN 'nome:' || v.nome_fornecedor
         WHEN v.codigo_do_contas_a_pagar IS NOT NULL
         THEN 'conta:' || TO_CHAR(v.codigo_do_contas_a_pagar)
         ELSE 'parcela:' || TO_CHAR(v.codigo_parcela_pk)
    END
END"""
SEM_CADASTRO_PREFIX = 'sem-cadastro-'


def _identidade_fornecedor(row):
    if row['codigo_fornecedor'] is not None:
        return int(row['codigo_fornecedor'])
    referencia = row['referencia_sem_cadastro']
    return SEM_CADASTRO_PREFIX + urlsafe_b64encode(
        referencia.encode('utf-8')
    ).decode('ascii').rstrip('=')


ORACLE_FORNECEDORES_QUERY = text(f"""
WITH pagamentos_distintos AS (
    SELECT codigo_parcela_pk,
           codigo_pagamento_pk,
           MAX(NVL(valor_pago, 0)) AS valor_pago
      FROM dbamv.HPC_V_CONTAS_A_PAGAR
     WHERE codigo_pagamento_pk IS NOT NULL
       AND data_de_estorno IS NULL
     GROUP BY codigo_parcela_pk, codigo_pagamento_pk
),
pagamentos AS (
    SELECT codigo_parcela_pk, SUM(valor_pago) AS valor_pago
      FROM pagamentos_distintos
     GROUP BY codigo_parcela_pk
),
parcelas AS (
    SELECT v.codigo_do_fornecedor AS codigo_fornecedor,
           {REFERENCIA_SEM_CADASTRO_SQL} AS referencia_sem_cadastro,
           MAX(v.nome_fornecedor) AS nome_fornecedor,
           v.codigo_parcela_pk,
           MAX(NVL(v.valor_da_duplicata, 0)) AS valor_duplicata,
           MIN(TO_DATE(v.dt_vencimento, 'DD/MM/YYYY')) AS data_vencimento,
           MAX(v.tipo_de_quitacao) AS tipo_quitacao
      FROM dbamv.HPC_V_CONTAS_A_PAGAR v
     GROUP BY v.codigo_do_fornecedor, v.codigo_parcela_pk,
              {REFERENCIA_SEM_CADASTRO_SQL}
),
saldos AS (
    SELECT p.codigo_fornecedor,
           p.referencia_sem_cadastro,
           p.nome_fornecedor,
           p.data_vencimento,
           p.valor_duplicata,
           LEAST(p.valor_duplicata, NVL(pg.valor_pago, 0)) AS valor_honrado,
           GREATEST(p.valor_duplicata - NVL(pg.valor_pago, 0), 0) AS saldo
      FROM parcelas p
      LEFT JOIN pagamentos pg ON pg.codigo_parcela_pk = p.codigo_parcela_pk
     WHERE p.tipo_quitacao IN (
           'previsto', 'comprometido', 'parcialmente pago'
     )
       AND (:data_inicio IS NULL OR p.data_vencimento >= :data_inicio)
       AND (:data_fim IS NULL OR p.data_vencimento <= :data_fim)
)
SELECT codigo_fornecedor,
       referencia_sem_cadastro,
       MAX(nome_fornecedor) AS nome_fornecedor,
       SUM(valor_duplicata) AS valor_total,
       SUM(valor_honrado) AS valor_total_honrado,
       SUM(saldo) AS saldo_a_pagar,
       SUM(CASE WHEN data_vencimento <= :hoje THEN saldo ELSE 0 END) AS valor_a_pagar_ate_hoje,
       SUM(CASE WHEN data_vencimento < TRUNC(SYSDATE) THEN saldo ELSE 0 END) AS valor_total_vencido,
       SUM(CASE WHEN data_vencimento >= TRUNC(SYSDATE) THEN saldo ELSE 0 END) AS valor_corrente,
       MIN(CASE WHEN data_vencimento < TRUNC(SYSDATE) AND saldo > 0 THEN data_vencimento END) AS vencimento_mais_antigo,
       SUM(CASE WHEN data_vencimento BETWEEN TRUNC(SYSDATE) - 6 AND TRUNC(SYSDATE) - 1 THEN saldo ELSE 0 END) AS novos_vencidos_7d,
       COUNT(CASE WHEN data_vencimento < TRUNC(SYSDATE) AND saldo > 0 THEN 1 END) AS titulos_vencidos,
       COUNT(CASE WHEN data_vencimento >= TRUNC(SYSDATE) AND saldo > 0 THEN 1 END) AS titulos_correntes
  FROM saldos
 GROUP BY codigo_fornecedor, referencia_sem_cadastro
HAVING SUM(saldo) > 0
""")


ORACLE_TITULOS_QUERY = text(f"""
WITH pagamentos_distintos AS (
    SELECT codigo_parcela_pk,
           codigo_pagamento_pk,
           MAX(NVL(valor_pago, 0)) AS valor_pago
      FROM dbamv.HPC_V_CONTAS_A_PAGAR
     WHERE codigo_pagamento_pk IS NOT NULL
       AND data_de_estorno IS NULL
     GROUP BY codigo_parcela_pk, codigo_pagamento_pk
),
pagamentos AS (
    SELECT codigo_parcela_pk, SUM(valor_pago) AS valor_pago
      FROM pagamentos_distintos
     GROUP BY codigo_parcela_pk
),
parcelas AS (
    SELECT v.codigo_do_fornecedor AS codigo_fornecedor,
           {REFERENCIA_SEM_CADASTRO_SQL} AS referencia_sem_cadastro,
           MAX(v.nome_fornecedor) AS nome_fornecedor,
           v.codigo_parcela_pk,
           MAX(v.codigo_do_contas_a_pagar) AS codigo_contas_pagar,
           MAX(v.numero_do_documento) AS numero_documento,
           MAX(v.descricao_da_conta) AS descricao_conta,
           MAX(v.numero_da_parcela) AS numero_parcela,
           MAX(v.data_de_lancamento) AS data_lancamento,
           MAX(v.data_de_emissao) AS data_emissao,
           MAX(v.valor_bruto) AS valor_nota_fiscal,
           MAX(NVL(v.valor_da_duplicata, 0)) AS valor_duplicata,
           MIN(TO_DATE(v.dt_vencimento, 'DD/MM/YYYY')) AS data_vencimento,
           MAX(v.tipo_de_quitacao) AS tipo_quitacao
     FROM dbamv.HPC_V_CONTAS_A_PAGAR v
     WHERE v.codigo_do_fornecedor IN :codigos_fornecedor
        OR ({REFERENCIA_SEM_CADASTRO_SQL}) IN :referencias_sem_cadastro
     GROUP BY v.codigo_do_fornecedor, v.codigo_parcela_pk,
              {REFERENCIA_SEM_CADASTRO_SQL}
)
SELECT p.codigo_fornecedor,
       p.referencia_sem_cadastro,
       p.nome_fornecedor,
       p.codigo_parcela_pk,
       p.codigo_contas_pagar,
       p.numero_documento,
       p.descricao_conta,
       p.numero_parcela,
       p.data_lancamento,
       p.data_emissao,
       p.valor_duplicata,
       p.valor_nota_fiscal,
       p.data_vencimento,
       p.tipo_quitacao,
       NVL(pg.valor_pago, 0) AS valor_honrado_oracle
  FROM parcelas p
  LEFT JOIN pagamentos pg ON pg.codigo_parcela_pk = p.codigo_parcela_pk
 WHERE p.tipo_quitacao IN (
       'previsto', 'comprometido', 'parcialmente pago'
 )
   AND (:data_inicio IS NULL OR p.data_vencimento >= :data_inicio)
   AND (:data_fim IS NULL OR p.data_vencimento <= :data_fim)
 ORDER BY p.codigo_fornecedor, p.data_vencimento, p.codigo_parcela_pk
""").bindparams(
    bindparam('codigos_fornecedor', expanding=True),
    bindparam('referencias_sem_cadastro', expanding=True),
)


def _decimal(value) -> Decimal:
    try:
        return Decimal(str(value or 0)).quantize(CENTAVOS)
    except (InvalidOperation, ValueError):
        return Decimal('0.00')


def _serializar_decimal(value: Decimal) -> str:
    return f'{value.quantize(CENTAVOS):.2f}'


def _normalizar_data(value):
    return value.date() if isinstance(value, datetime) else value


def _consultar_oracle(
    session: Session,
    data_inicio: date | None = None,
    data_fim: date | None = None,
) -> list[dict]:
    try:
        rows = (
            session
            .execute(
                ORACLE_FORNECEDORES_QUERY,
                {
                    'data_inicio': data_inicio,
                    'data_fim': data_fim,
                    'hoje': _hoje_hospital(),
                },
            )
            .mappings()
            .all()
        )
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=HTTPStatus.SERVICE_UNAVAILABLE,
            detail='Não foi possível consultar a HPC_V_CONTAS_A_PAGAR.',
        ) from exc
    hoje = _hoje_hospital()
    resultado = []
    for row in rows:
        vencimento = _normalizar_data(row['vencimento_mais_antigo'])
        dias_atraso = max((hoje - vencimento).days, 0) if vencimento else 0
        resultado.append({
            'codigo_fornecedor': _identidade_fornecedor(row),
            'sem_cadastro': row['codigo_fornecedor'] is None,
            'nome_fornecedor': row['nome_fornecedor'] or 'Fornecedor sem nome',
            'valor_total': _decimal(row['valor_total']),
            'valor_total_vencido': _decimal(row['valor_total_vencido']),
            'valor_total_honrado': _decimal(row['valor_total_honrado']),
            'saldo_a_pagar': _decimal(row['saldo_a_pagar']),
            'valor_a_pagar_ate_hoje': _decimal(
                row.get('valor_a_pagar_ate_hoje')
            ),
            'valor_corrente': _decimal(row['valor_corrente']),
            'vencimento_mais_antigo': vencimento,
            'dias_atraso': dias_atraso,
            'total_dias_vencidos': dias_atraso,
            'novos_vencidos_7d': _decimal(row['novos_vencidos_7d']),
            'titulos_vencidos': int(row['titulos_vencidos'] or 0),
            'titulos_correntes': int(row['titulos_correntes'] or 0),
            'titulos': [],
        })
    return resultado


def _consultar_titulos_oracle(
    session: Session,
    codigos_fornecedor: list[int | str],
    data_inicio: date | None = None,
    data_fim: date | None = None,
) -> list[dict]:
    if not codigos_fornecedor:
        return []
    try:
        rows = []
        # Oracle supports at most 1000 expressions per IN list.
        for inicio in range(0, len(codigos_fornecedor), 1000):
            lote = codigos_fornecedor[inicio : inicio + 1000]
            codigos = [codigo for codigo in lote if isinstance(codigo, int)]
            referencias = [
                urlsafe_b64decode(
                    codigo.removeprefix(SEM_CADASTRO_PREFIX) + '==='
                ).decode('utf-8')
                for codigo in lote
                if isinstance(codigo, str)
                and codigo.startswith(SEM_CADASTRO_PREFIX)
            ]
            rows.extend(
                session
                .execute(
                    ORACLE_TITULOS_QUERY,
                    {
                        'codigos_fornecedor': codigos,
                        'referencias_sem_cadastro': referencias,
                        'data_inicio': data_inicio,
                        'data_fim': data_fim,
                    },
                )
                .mappings()
                .all()
            )
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=HTTPStatus.SERVICE_UNAVAILABLE,
            detail='Não foi possível consultar a HPC_V_CONTAS_A_PAGAR.',
        ) from exc
    resultado = []
    for row in rows:
        if row['codigo_parcela_pk'] is None:
            continue
        resultado.append({
            'codigo_fornecedor': _identidade_fornecedor(row),
            'sem_cadastro': row['codigo_fornecedor'] is None,
            'nome_fornecedor': row['nome_fornecedor'] or 'Fornecedor sem nome',
            'codigo_parcela': int(row['codigo_parcela_pk']),
            'codigo_contas_pagar': int(row['codigo_contas_pagar'])
            if row['codigo_contas_pagar'] is not None
            else None,
            'numero_documento': row['numero_documento'],
            'descricao_conta': row['descricao_conta'],
            'numero_parcela': row['numero_parcela'],
            'data_lancamento': _normalizar_data(row['data_lancamento']),
            'data_emissao': _normalizar_data(row['data_emissao']),
            'valor_total': _decimal(row['valor_duplicata']),
            'valor_nota_fiscal': _decimal(row['valor_nota_fiscal'])
            if row.get('valor_nota_fiscal') is not None
            else None,
            'data_vencimento': _normalizar_data(row['data_vencimento']),
            'tipo_quitacao': row['tipo_quitacao'],
            'valor_honrado_oracle': _decimal(row['valor_honrado_oracle']),
        })
    return resultado


def _pagamentos_manuais(session: Session) -> dict[int, list[dict]]:
    rows = session.execute(
        text("""
        SELECT p.*, u.nome AS usuario_nome
          FROM api_prontocardio.contas_pagar_pagamentos p
          JOIN api_prontocardio.usuarios_api u ON u.id = p.usuario_id
         ORDER BY p.data_pagamento DESC, p.id DESC
    """)
    ).mappings()
    pagamentos = {}
    for row in rows:
        item = dict(row)
        item['valor_pago'] = _decimal(item['valor_pago'])
        pagamentos.setdefault(int(item['codigo_parcela']), []).append(item)
    return pagamentos


def _agrupar_fornecedores(
    titulos: list[dict], pagamentos_por_titulo: dict[int, list[dict]]
) -> list[dict]:
    hoje = _hoje_hospital()
    fornecedores = {}
    for titulo_oracle in titulos:
        titulo = dict(titulo_oracle)
        pagamentos = [
            item
            for item in pagamentos_por_titulo.get(titulo['codigo_parcela'], [])
            if item.get('codigo_fornecedor') == titulo['codigo_fornecedor']
        ]
        valor_manual = sum(
            (item['valor_pago'] for item in pagamentos), Decimal('0')
        )
        valor_honrado = min(
            titulo['valor_total'],
            titulo['valor_honrado_oracle'] + valor_manual,
        )
        saldo = max(titulo['valor_total'] - valor_honrado, Decimal('0'))
        vencimento = titulo['data_vencimento']
        dias_vencidos = max((hoje - vencimento).days, 0) if vencimento else 0
        titulo.update({
            'valor_honrado_manual': valor_manual,
            'valor_total_honrado': valor_honrado,
            'saldo_a_pagar': saldo,
            'dias_vencidos': dias_vencidos,
            'pagamentos': pagamentos,
        })
        codigo = titulo['codigo_fornecedor']
        fornecedor = fornecedores.setdefault(
            codigo,
            {
                'codigo_fornecedor': codigo,
                'sem_cadastro': titulo.get('sem_cadastro', False),
                'nome_fornecedor': titulo['nome_fornecedor'],
                'valor_total': Decimal('0'),
                'valor_total_vencido': Decimal('0'),
                'valor_total_honrado': Decimal('0'),
                'valor_corrente': Decimal('0'),
                'novos_vencidos_7d': Decimal('0'),
                'total_dias_vencidos': 0,
                'dias_atraso': 0,
                'vencimento_mais_antigo': None,
                'titulos_vencidos': 0,
                'titulos_correntes': 0,
                'saldo_a_pagar': Decimal('0'),
                'valor_a_pagar_ate_hoje': Decimal('0'),
                'titulos': [],
            },
        )
        fornecedor['titulos'].append(titulo)
        fornecedor['valor_total'] += titulo['valor_total']
        fornecedor['valor_total_honrado'] += valor_honrado
        fornecedor['saldo_a_pagar'] += saldo
        if vencimento and vencimento <= hoje:
            fornecedor['valor_a_pagar_ate_hoje'] += saldo
        if saldo > 0:
            fornecedor['total_dias_vencidos'] += dias_vencidos
            fornecedor['dias_atraso'] = max(
                fornecedor['dias_atraso'], dias_vencidos
            )
        if vencimento and vencimento < hoje and saldo > 0:
            fornecedor['valor_total_vencido'] += saldo
            fornecedor['titulos_vencidos'] += 1
            antigo = fornecedor['vencimento_mais_antigo']
            if antigo is None or vencimento < antigo:
                fornecedor['vencimento_mais_antigo'] = vencimento
            if vencimento >= hoje - timedelta(days=6):
                fornecedor['novos_vencidos_7d'] += saldo
        elif saldo > 0:
            fornecedor['valor_corrente'] += saldo
            fornecedor['titulos_correntes'] += 1
    resultado = list(fornecedores.values())
    for fornecedor in resultado:
        fornecedor['titulos'].sort(
            key=lambda titulo: (
                -titulo['dias_vencidos'],
                titulo['data_vencimento'] or date.max,
                titulo['codigo_parcela'],
            )
        )
    return resultado


def _chave_prioridade_fornecedor(item: dict):
    return (
        -(item.get('nivel_criticidade') or (5 if item['critico'] else 1)),
        -item['dias_atraso'],
        -item['valor_total_vencido'],
    )


def _tratamentos(session: Session) -> dict[int | str, dict]:
    rows = session.execute(
        text("""
        SELECT t.*, u.nome AS usuario_nome, uc.nome AS classificado_por
          FROM api_prontocardio.contas_pagar_tratamentos t
          JOIN api_prontocardio.usuarios_api u ON u.id = t.usuario_id
          LEFT JOIN api_prontocardio.usuarios_api uc ON uc.id = t.usuario_criticidade_id
    """)
    ).mappings()
    tratamentos = {int(row['codigo_fornecedor']): dict(row) for row in rows}
    classificacoes = session.execute(
        text("""
        SELECT DISTINCT ON (c.identificador)
               c.identificador, c.nivel_criticidade,
               c.nivel_criticidade > 1 AS critico,
               c.justificativa AS justificativa_criticidade,
               c.data_classificacao, u.nome AS classificado_por,
               u.nome AS usuario_nome
          FROM api_prontocardio.contas_pagar_classificacoes_sem_cadastro c
          JOIN api_prontocardio.usuarios_api u ON u.id = c.usuario_id
         ORDER BY c.identificador, c.id DESC
    """)
    ).mappings()
    tratamentos.update({
        row['identificador']: dict(row) for row in classificacoes
    })
    return tratamentos


def _registrar_snapshot(session: Session, fornecedores: list[dict]) -> None:
    hoje = _hoje_hospital()
    total_vencido = sum(
        (item['valor_vencido'] for item in fornecedores), Decimal('0')
    )
    novos = sum(
        (item['novos_vencidos_7d'] for item in fornecedores), Decimal('0')
    )
    corrente = sum(
        (item['valor_corrente'] for item in fornecedores), Decimal('0')
    )
    quantidade = sum(1 for item in fornecedores if item['valor_vencido'] > 0)
    session.execute(
        text("""
        INSERT INTO api_prontocardio.contas_pagar_snapshots
            (data_referencia, valor_vencido, novos_vencidos, valor_corrente, fornecedores_vencidos)
        VALUES (:data, :vencido, :novos, :corrente, :quantidade)
        ON CONFLICT (data_referencia) DO UPDATE SET
            valor_vencido = EXCLUDED.valor_vencido,
            novos_vencidos = EXCLUDED.novos_vencidos,
            valor_corrente = EXCLUDED.valor_corrente,
            fornecedores_vencidos = EXCLUDED.fornecedores_vencidos,
            data_registro = timezone('America/Sao_Paulo', now())
    """),
        {
            'data': hoje,
            'vencido': total_vencido,
            'novos': novos,
            'corrente': corrente,
            'quantidade': quantidade,
        },
    )
    session.commit()


def _historico(session: Session) -> list[dict]:
    rows = (
        session
        .execute(
            text("""
        SELECT data_referencia, valor_vencido, novos_vencidos, valor_corrente, fornecedores_vencidos
          FROM api_prontocardio.contas_pagar_snapshots
         ORDER BY data_referencia DESC
         LIMIT 13
    """)
        )
        .mappings()
        .all()
    )
    return [
        {
            'data_referencia': row['data_referencia'].isoformat(),
            'valor_vencido': _serializar_decimal(
                _decimal(row['valor_vencido'])
            ),
            'novos_vencidos': _serializar_decimal(
                _decimal(row['novos_vencidos'])
            ),
            'valor_corrente': _serializar_decimal(
                _decimal(row['valor_corrente'])
            ),
            'fornecedores_vencidos': int(row['fornecedores_vencidos']),
        }
        for row in reversed(rows)
    ]


def _valor_vencido_inicio_acompanhamento(
    session: Session,
) -> Decimal | None:
    valor = session.execute(
        text("""
        SELECT valor_vencido
          FROM api_prontocardio.contas_pagar_snapshots
         ORDER BY data_referencia ASC
         LIMIT 1
    """)
    ).scalar_one_or_none()
    return _decimal(valor) if valor is not None else None


def _filtrar_prioridade(
    filtrados,
    somente_vencidos,
    nivel_criticidade,
    faixa_atraso,
    oracle,
    pagamentos_por_titulo,
    data_inicio,
    data_fim,
):
    if somente_vencidos:
        filtrados = [
            item for item in filtrados if item['valor_total_vencido'] > 0
        ]
    if nivel_criticidade == 'pendente':
        filtrados = [
            item for item in filtrados if item['criticidade_pendente']
        ]
    elif nivel_criticidade != 'todos':
        filtrados = [
            item
            for item in filtrados
            if item['nivel_criticidade'] == int(nivel_criticidade)
        ]
    detalhes_filtrados = None
    if faixa_atraso != 'todos':
        titulos_faixa = _consultar_titulos_oracle(
            oracle,
            [item['codigo_fornecedor'] for item in filtrados],
            data_inicio=data_inicio,
            data_fim=data_fim,
        )
        detalhes_filtrados = {
            item['codigo_fornecedor']: [
                titulo
                for titulo in item['titulos']
                if titulo['saldo_a_pagar'] > 0
                and age_level(titulo['dias_vencidos']) == int(faixa_atraso)
            ]
            for item in _agrupar_fornecedores(
                titulos_faixa, pagamentos_por_titulo
            )
        }
        filtrados = [
            item
            for item in filtrados
            if detalhes_filtrados.get(item['codigo_fornecedor'])
        ]
    filtrados.sort(key=_chave_prioridade_fornecedor)
    return filtrados, detalhes_filtrados


def _enriquecer_fornecedores(itens, tratamentos):
    for item in itens:
        tratamento = tratamentos.get(item['codigo_fornecedor'], {})
        item.update({
            'critico': bool(tratamento.get('critico', False)),
            'pagamento_imediato': _decimal(
                tratamento.get('pagamento_imediato')
            ),
            'status': tratamento.get('status') or 'PENDENTE',
            'responsavel': tratamento.get('responsavel'),
            'proxima_acao': tratamento.get('proxima_acao'),
            'data_proxima_acao': tratamento.get('data_proxima_acao'),
            'condicao_negociada': tratamento.get('condicao_negociada'),
            'observacao': tratamento.get('observacao'),
            'usuario_atualizacao': tratamento.get('usuario_nome'),
            'data_atualizacao': tratamento.get('data_atualizacao'),
        })
        item.update(
            classification({
                **tratamento,
                'sem_cadastro': item.get('sem_cadastro', False),
            })
        )
        item['faixa_atraso'] = age_level(item['dias_atraso'])
        item['saldo_negociar'] = max(
            item['valor_total_vencido'] - item['pagamento_imediato'],
            Decimal('0'),
        )
        item['valor_vencido'] = item['valor_total_vencido']


def _selecionar_codigos(valor):
    codigos = {
        parte.strip() for parte in (valor or '').split(',') if parte.strip()
    }
    if any(
        not re.fullmatch(r'(?:[0-9]+|sem-cadastro-[A-Za-z0-9_-]+)', codigo)
        for codigo in codigos
    ):
        raise HTTPException(422, 'Selecao de fornecedores invalida.')
    return codigos


def _resolver_quadrante(nivel, faixa, quadrante):
    if quadrante is None:
        return nivel, faixa, True
    if not 1 <= quadrante <= TOTAL_QUADRANTES:
        raise HTTPException(422, 'Quadrante deve estar entre 1 e 25.')
    nivel_quadrante = str(5 - (quadrante - 1) // 5)
    faixa_quadrante = str(5 - (quadrante - 1) % 5)
    compativel = nivel in ('todos', nivel_quadrante) and faixa in (
        'todos',
        faixa_quadrante,
    )
    return nivel_quadrante, faixa_quadrante, compativel


def _filtrar_fornecedores(
    vencidos,
    q,
    criticidade,
    status,
    codigos_selecionados,
    quadrante_compativel,
):
    termo = (q or '').strip().casefold()
    filtrados = vencidos if quadrante_compativel else []
    if codigos_selecionados:
        filtrados = [
            item
            for item in filtrados
            if str(item['codigo_fornecedor']) in codigos_selecionados
        ]
    if termo:
        filtrados = [
            item
            for item in filtrados
            if termo in item['nome_fornecedor'].casefold()
            or termo in str(item['codigo_fornecedor'])
        ]
    if criticidade == 'criticos':
        filtrados = [item for item in filtrados if item['critico']]
    elif criticidade == 'nao_criticos':
        filtrados = [
            item
            for item in filtrados
            if not item['critico'] and not item['criticidade_pendente']
        ]
    if status and status.upper() in STATUS_VALIDOS:
        filtrados = [
            item for item in filtrados if item['status'] == status.upper()
        ]
    return filtrados


@router.get('')
def listar_contas_pagar(
    _: UsuarioAtual,
    session: SessionPostgres,
    oracle: SessionOracle,
    q: str | None = None,
    criticidade: Literal['todos', 'criticos', 'nao_criticos'] = 'todos',
    status: str | None = None,
    data_inicio: date | None = None,
    data_fim: date | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    exportar: bool = False,
    nivel_criticidade: Literal[
        'todos', 'pendente', '1', '2', '3', '4', '5'
    ] = 'todos',
    faixa_atraso: Literal['todos', '1', '2', '3', '4', '5'] = 'todos',
    somente_vencidos: bool = False,
    fornecedores_ids: Annotated[str | None, Query(max_length=50000)] = None,
    quadrante: Annotated[int | None, Query(ge=1, le=25)] = None,
    ordenar: Literal['prioridade', 'maior_valor_vencido'] = 'prioridade',
    incluir_opcoes: bool = False,
):
    if data_inicio and data_fim and data_inicio > data_fim:
        raise HTTPException(
            status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
            detail='A data inicial não pode ser posterior à data final.',
        )
    codigos_selecionados = _selecionar_codigos(fornecedores_ids)
    nivel_criticidade, faixa_atraso, quadrante_compativel = (
        _resolver_quadrante(nivel_criticidade, faixa_atraso, quadrante)
    )
    pagamentos_por_titulo = _pagamentos_manuais(session)
    fornecedores = _consultar_oracle(
        oracle, data_inicio=data_inicio, data_fim=data_fim
    )
    opcoes = []
    if incluir_opcoes:
        base_opcoes = (
            _consultar_oracle(oracle)
            if data_inicio or data_fim
            else fornecedores
        )
        opcoes = sorted(
            [
                {
                    'codigo': str(item['codigo_fornecedor']),
                    'nome': item['nome_fornecedor'],
                    'sem_cadastro': item.get('sem_cadastro', False),
                }
                for item in base_opcoes
            ],
            key=lambda item: (item['nome'].casefold(), item['codigo']),
        )
    codigos_com_pagamento_manual = sorted({
        int(pagamento['codigo_fornecedor'])
        for pagamentos in pagamentos_por_titulo.values()
        for pagamento in pagamentos
    })
    if codigos_com_pagamento_manual:
        titulos_manuais = _consultar_titulos_oracle(
            oracle,
            codigos_com_pagamento_manual,
            data_inicio=data_inicio,
            data_fim=data_fim,
        )
        fornecedores_manuais = {
            item['codigo_fornecedor']: item
            for item in _agrupar_fornecedores(
                titulos_manuais, pagamentos_por_titulo
            )
        }
        campos_calculados = {
            'valor_total',
            'valor_total_vencido',
            'valor_total_honrado',
            'valor_corrente',
            'novos_vencidos_7d',
            'total_dias_vencidos',
            'dias_atraso',
            'vencimento_mais_antigo',
            'titulos_vencidos',
            'titulos_correntes',
            'saldo_a_pagar',
            'valor_a_pagar_ate_hoje',
        }
        for fornecedor in fornecedores:
            calculado = fornecedores_manuais.get(
                fornecedor['codigo_fornecedor']
            )
            if calculado:
                fornecedor.update({
                    campo: calculado[campo] for campo in campos_calculados
                })
    tratamentos = _tratamentos(session)

    _enriquecer_fornecedores(fornecedores, tratamentos)

    vencidos = [item for item in fornecedores if item['saldo_a_pagar'] > 0]
    if not exportar and not data_inicio and not data_fim:
        _registrar_snapshot(session, fornecedores)
    resumo = {
        'valor_a_pagar_ate_hoje': sum(
            (
                item.get('valor_a_pagar_ate_hoje', Decimal('0'))
                for item in fornecedores
            ),
            Decimal('0'),
        ),
        'valor_vencido_atual': sum(
            (item['valor_vencido'] for item in vencidos), Decimal('0')
        ),
        'valor_corrente': sum(
            (item['valor_corrente'] for item in fornecedores), Decimal('0')
        ),
        'novos_vencidos_7d': sum(
            (item['novos_vencidos_7d'] for item in vencidos), Decimal('0')
        ),
        'pagamento_imediato': sum(
            (item['pagamento_imediato'] for item in vencidos), Decimal('0')
        ),
        'saldo_negociar': sum(
            (item['saldo_negociar'] for item in vencidos), Decimal('0')
        ),
        'fornecedores_vencidos': len(vencidos),
        'fornecedores_criticos': sum(
            1 for item in vencidos if item['critico']
        ),
    }
    historico = _historico(session)
    valor_vencido_inicial = _valor_vencido_inicio_acompanhamento(session)
    resumo['valor_vencido_inicial'] = (
        valor_vencido_inicial
        if valor_vencido_inicial is not None
        else resumo['valor_vencido_atual']
    )
    resumo['variacao_desde_inicio'] = (
        resumo['valor_vencido_atual'] - resumo['valor_vencido_inicial']
    )

    filtrados = _filtrar_fornecedores(
        vencidos,
        q,
        criticidade,
        status,
        codigos_selecionados,
        quadrante_compativel,
    )
    filtrados, detalhes_filtrados = _filtrar_prioridade(
        filtrados,
        somente_vencidos,
        nivel_criticidade,
        faixa_atraso,
        oracle,
        pagamentos_por_titulo,
        data_inicio,
        data_fim,
    )
    if ordenar == 'maior_valor_vencido':
        filtrados.sort(
            key=lambda item: (
                -item['valor_total_vencido'],
                _chave_prioridade_fornecedor(item),
                str(item['codigo_fornecedor']),
            )
        )
    total = len(filtrados)
    inicio = (page - 1) * page_size
    pagina = filtrados if exportar else filtrados[inicio : inicio + page_size]
    codigos_pagina = [item['codigo_fornecedor'] for item in pagina]
    if detalhes_filtrados is not None:
        detalhes_pagina = detalhes_filtrados
    else:
        titulos_pagina = _consultar_titulos_oracle(
            oracle,
            codigos_pagina,
            data_inicio=data_inicio,
            data_fim=data_fim,
        )
        detalhes_pagina = {
            item['codigo_fornecedor']: item['titulos']
            for item in _agrupar_fornecedores(
                titulos_pagina, pagamentos_por_titulo
            )
        }
    for item in pagina:
        item['titulos'] = detalhes_pagina.get(item['codigo_fornecedor'], [])

    def serializar(item):
        titulos_serializados = []
        for titulo in item['titulos']:
            pagamentos = [
                {
                    **pagamento,
                    'data_pagamento': pagamento['data_pagamento'].isoformat(),
                    'valor_pago': _serializar_decimal(pagamento['valor_pago']),
                    'data_criacao': pagamento['data_criacao'].isoformat(),
                    'data_atualizacao': pagamento[
                        'data_atualizacao'
                    ].isoformat(),
                }
                for pagamento in titulo['pagamentos']
            ]
            titulos_serializados.append({
                **titulo,
                'data_vencimento': titulo['data_vencimento'].isoformat()
                if titulo['data_vencimento']
                else None,
                'data_lancamento': titulo['data_lancamento'].isoformat()
                if titulo['data_lancamento']
                else None,
                'data_emissao': titulo['data_emissao'].isoformat()
                if titulo['data_emissao']
                else None,
                'valor_total': _serializar_decimal(titulo['valor_total']),
                'valor_nota_fiscal': _serializar_decimal(
                    titulo['valor_nota_fiscal']
                )
                if titulo.get('valor_nota_fiscal') is not None
                else None,
                'valor_honrado_oracle': _serializar_decimal(
                    titulo['valor_honrado_oracle']
                ),
                'valor_honrado_manual': _serializar_decimal(
                    titulo['valor_honrado_manual']
                ),
                'valor_total_honrado': _serializar_decimal(
                    titulo['valor_total_honrado']
                ),
                'saldo_a_pagar': _serializar_decimal(titulo['saldo_a_pagar']),
                'pagamentos': pagamentos,
            })
        return {
            **item,
            'valor_total': _serializar_decimal(item['valor_total']),
            'valor_total_vencido': _serializar_decimal(
                item['valor_total_vencido']
            ),
            'valor_total_honrado': _serializar_decimal(
                item['valor_total_honrado']
            ),
            'saldo_a_pagar': _serializar_decimal(item['saldo_a_pagar']),
            'valor_vencido': _serializar_decimal(item['valor_vencido']),
            'valor_corrente': _serializar_decimal(item['valor_corrente']),
            'novos_vencidos_7d': _serializar_decimal(
                item['novos_vencidos_7d']
            ),
            'pagamento_imediato': _serializar_decimal(
                item['pagamento_imediato']
            ),
            'saldo_negociar': _serializar_decimal(item['saldo_negociar']),
            'titulos': titulos_serializados,
        }

    return {
        'fornecedores': [serializar(item) for item in pagina],
        **({'opcoes_fornecedores': opcoes} if incluir_opcoes else {}),
        'total': total,
        'page': 1 if exportar else page,
        'total_pages': 1 if exportar else max(ceil(total / page_size), 1),
        'resumo': {
            key: _serializar_decimal(value)
            if isinstance(value, Decimal)
            else value
            for key, value in resumo.items()
        },
        'historico': historico,
        'gerado_em': _hoje_hospital().isoformat(),
    }


@router.put('/fornecedores/{codigo_fornecedor}')
def salvar_tratamento(
    codigo_fornecedor: int,
    payload: TratamentoInput,
    usuario: UsuarioAtual,
    session: SessionPostgres,
):
    session.execute(
        text("""
        INSERT INTO api_prontocardio.contas_pagar_tratamentos
            (codigo_fornecedor, critico, pagamento_imediato, status, responsavel,
             proxima_acao, data_proxima_acao, condicao_negociada, observacao, usuario_id)
        VALUES (:codigo, :critico, :pagamento, :status, :responsavel,
                :proxima_acao, :data_proxima_acao, :condicao, :observacao, :usuario)
        ON CONFLICT (codigo_fornecedor) DO UPDATE SET
            critico = CASE WHEN contas_pagar_tratamentos.nivel_criticidade IS NOT NULL
                THEN contas_pagar_tratamentos.nivel_criticidade > 1 ELSE EXCLUDED.critico END,
            pagamento_imediato = EXCLUDED.pagamento_imediato,
            status = EXCLUDED.status,
            responsavel = EXCLUDED.responsavel,
            proxima_acao = EXCLUDED.proxima_acao,
            data_proxima_acao = EXCLUDED.data_proxima_acao,
            condicao_negociada = EXCLUDED.condicao_negociada,
            observacao = EXCLUDED.observacao,
            usuario_id = EXCLUDED.usuario_id,
            data_atualizacao = timezone('America/Sao_Paulo', now())
    """),
        {
            'codigo': codigo_fornecedor,
            'critico': payload.critico,
            'pagamento': payload.pagamento_imediato,
            'status': payload.status,
            'responsavel': payload.responsavel,
            'proxima_acao': payload.proxima_acao,
            'data_proxima_acao': payload.data_proxima_acao,
            'condicao': payload.condicao_negociada,
            'observacao': payload.observacao,
            'usuario': usuario.id,
        },
    )
    session.commit()
    return {'detail': 'Tratamento salvo com sucesso.'}


@router.delete(
    '/fornecedores/{codigo_fornecedor}', status_code=HTTPStatus.NO_CONTENT
)
def excluir_tratamento(
    codigo_fornecedor: int,
    _: UsuarioAtual,
    session: SessionPostgres,
):
    session.execute(
        text("""
        DELETE FROM api_prontocardio.contas_pagar_tratamentos
         WHERE codigo_fornecedor = :codigo
    """),
        {'codigo': codigo_fornecedor},
    )
    session.commit()
    return Response(status_code=HTTPStatus.NO_CONTENT)


def _validar_identificador_sem_cadastro(identificador):
    if len(identificador) > MAX_IDENTIFICADOR_SEM_CADASTRO or not re.fullmatch(
        r'sem-cadastro-[A-Za-z0-9_-]+', identificador
    ):
        raise HTTPException(422, 'Identificador sem cadastro inválido.')
    try:
        referencia = urlsafe_b64decode(
            identificador.removeprefix(SEM_CADASTRO_PREFIX) + '==='
        ).decode('utf-8')
    except (Base64Error, UnicodeDecodeError, ValueError) as exc:
        raise HTTPException(
            422, 'Identificador sem cadastro inválido.'
        ) from exc
    if not referencia.startswith(('nome:', 'conta:', 'parcela:')) or (
        _identidade_fornecedor({
            'codigo_fornecedor': None,
            'referencia_sem_cadastro': referencia,
        })
        != identificador
    ):
        raise HTTPException(422, 'Identificador sem cadastro inválido.')


@router.patch('/sem-cadastro/{identificador}/criticidade')
def classificar_sem_cadastro(
    identificador: str,
    payload: CriticidadeFornecedorInput,
    usuario: UsuarioAtual,
    session: SessionPostgres,
    oracle: SessionOracle,
):
    _validar_identificador_sem_cadastro(identificador)
    if payload.nivel_criticidade is None:
        raise HTTPException(
            422, 'Informe o nível de criticidade e a justificativa.'
        )
    fornecedor = next(
        (
            item
            for item in _consultar_oracle(oracle)
            if item['codigo_fornecedor'] == identificador
            and item.get('sem_cadastro')
        ),
        None,
    )
    if fornecedor is None:
        raise HTTPException(
            404,
            'Registro sem cadastro não encontrado na consulta atual do MV.',
        )
    # Each change appends an audit event; the latest id is the active classification.
    session.execute(
        text("""
        INSERT INTO api_prontocardio.contas_pagar_classificacoes_sem_cadastro
            (identificador, nome_fornecedor, nivel_criticidade, justificativa, usuario_id)
        VALUES (:identificador, :nome, :nivel, :justificativa, :usuario)
    """),
        {
            'identificador': identificador,
            'nome': fornecedor['nome_fornecedor'],
            'nivel': payload.nivel_criticidade,
            'justificativa': payload.justificativa,
            'usuario': usuario.id,
        },
    )
    session.commit()
    return {'detail': 'Classificação atualizada com sucesso.'}


@router.patch('/fornecedores/{codigo_fornecedor}/criticidade')
def atualizar_criticidade_fornecedor(
    codigo_fornecedor: int,
    payload: CriticidadeFornecedorInput,
    usuario: UsuarioAtual,
    session: SessionPostgres,
):
    if payload.nivel_criticidade is None:
        session.execute(
            text("""
            INSERT INTO api_prontocardio.contas_pagar_tratamentos
                (codigo_fornecedor, critico, usuario_id)
            VALUES (:codigo, :critico, :usuario_id)
            ON CONFLICT (codigo_fornecedor) DO UPDATE SET
                critico = EXCLUDED.critico,
                nivel_criticidade = CASE WHEN NOT EXCLUDED.critico THEN 1
                    WHEN contas_pagar_tratamentos.nivel_criticidade > 1
                    THEN contas_pagar_tratamentos.nivel_criticidade ELSE NULL END,
                justificativa_criticidade = CASE
                    WHEN contas_pagar_tratamentos.critico = EXCLUDED.critico
                    THEN contas_pagar_tratamentos.justificativa_criticidade ELSE NULL END,
                usuario_criticidade_id = :usuario_id,
                data_classificacao = timezone('America/Sao_Paulo', now()),
                usuario_id = EXCLUDED.usuario_id,
                data_atualizacao = timezone('America/Sao_Paulo', now())
        """),
            {
                'codigo': codigo_fornecedor,
                'critico': payload.critico,
                'usuario_id': usuario.id,
            },
        )
        session.commit()
        return {
            'detail': 'Fornecedor marcado como crítico.'
            if payload.critico
            else 'Criticidade removida do fornecedor.'
        }
    session.execute(
        text("""
        INSERT INTO api_prontocardio.contas_pagar_tratamentos
            (codigo_fornecedor, critico, usuario_id, nivel_criticidade,
             justificativa_criticidade, usuario_criticidade_id, data_classificacao)
        VALUES (:codigo, :critico, :usuario_id, :nivel, :justificativa, :usuario_id,
                timezone('America/Sao_Paulo', now()))
        ON CONFLICT (codigo_fornecedor) DO UPDATE SET
            critico = EXCLUDED.critico,
            nivel_criticidade = EXCLUDED.nivel_criticidade,
            justificativa_criticidade = EXCLUDED.justificativa_criticidade,
            usuario_criticidade_id = EXCLUDED.usuario_criticidade_id,
            data_classificacao = EXCLUDED.data_classificacao,
            usuario_id = EXCLUDED.usuario_id,
            data_atualizacao = timezone('America/Sao_Paulo', now())
    """),
        {
            'codigo': codigo_fornecedor,
            'critico': payload.critico,
            'usuario_id': usuario.id,
            'nivel': payload.nivel_criticidade,
            'justificativa': payload.justificativa,
        },
    )
    session.commit()
    return {'detail': 'Classificação atualizada com sucesso.'}


def _validar_fornecedor_titulo(
    oracle_session: Session, fornecedor: int, parcela: int
):
    # Historical payment edits must remain valid, regardless of settlement.
    try:
        existe = oracle_session.execute(
            text("""
            SELECT 1 FROM dbamv.HPC_V_CONTAS_A_PAGAR
             WHERE codigo_do_fornecedor = :fornecedor
               AND codigo_parcela_pk = :parcela AND ROWNUM = 1
        """),
            {'fornecedor': fornecedor, 'parcela': parcela},
        ).scalar_one_or_none()
    except SQLAlchemyError as exc:
        raise HTTPException(
            503, 'Não foi possível validar o título no MV. Tente novamente.'
        ) from exc
    if existe is None:
        raise HTTPException(404, 'Título não encontrado para este fornecedor.')


@router.post(
    '/fornecedores/{codigo_fornecedor}/titulos/{codigo_parcela}/pagamentos',
    status_code=HTTPStatus.CREATED,
)
def registrar_pagamento_titulo(
    codigo_fornecedor: int,
    codigo_parcela: int,
    payload: PagamentoTituloInput,
    usuario: UsuarioAtual,
    session: SessionPostgres,
    oracle_session: SessionOracle,
):
    _validar_fornecedor_titulo(
        oracle_session, codigo_fornecedor, codigo_parcela
    )
    pagamento_id = session.execute(
        text("""
        INSERT INTO api_prontocardio.contas_pagar_pagamentos
            (codigo_fornecedor, codigo_parcela, data_pagamento, valor_pago,
             banco, agencia, numero_conta, observacao, usuario_id)
        VALUES (:fornecedor, :parcela, :data_pagamento, :valor_pago,
                :banco, :agencia, :numero_conta, :observacao, :usuario_id)
        RETURNING id
    """),
        {
            'fornecedor': codigo_fornecedor,
            'parcela': codigo_parcela,
            'data_pagamento': payload.data_pagamento,
            'valor_pago': payload.valor_pago,
            'banco': payload.banco,
            'agencia': payload.agencia,
            'numero_conta': payload.numero_conta,
            'observacao': payload.observacao,
            'usuario_id': usuario.id,
        },
    ).scalar_one()
    session.commit()
    return {
        'id': pagamento_id,
        'detail': 'Pagamento informado com sucesso.',
    }


@router.put(
    '/fornecedores/{codigo_fornecedor}/titulos/{codigo_parcela}'
    '/pagamentos/{pagamento_id}'
)
def atualizar_pagamento_titulo(
    codigo_fornecedor: int,
    codigo_parcela: int,
    pagamento_id: int,
    payload: PagamentoTituloInput,
    usuario: UsuarioAtual,
    session: SessionPostgres,
    oracle_session: SessionOracle,
):
    _validar_fornecedor_titulo(
        oracle_session, codigo_fornecedor, codigo_parcela
    )
    atualizado = session.execute(
        text("""
        UPDATE api_prontocardio.contas_pagar_pagamentos
           SET data_pagamento = :data_pagamento,
               valor_pago = :valor_pago,
               banco = :banco,
               agencia = :agencia,
               numero_conta = :numero_conta,
               observacao = :observacao,
               usuario_id = :usuario_id,
               data_atualizacao = timezone('America/Sao_Paulo', now())
         WHERE id = :pagamento_id
           AND codigo_fornecedor = :fornecedor
           AND codigo_parcela = :parcela
     RETURNING id
    """),
        {
            'pagamento_id': pagamento_id,
            'fornecedor': codigo_fornecedor,
            'parcela': codigo_parcela,
            'data_pagamento': payload.data_pagamento,
            'valor_pago': payload.valor_pago,
            'banco': payload.banco,
            'agencia': payload.agencia,
            'numero_conta': payload.numero_conta,
            'observacao': payload.observacao,
            'usuario_id': usuario.id,
        },
    ).scalar_one_or_none()
    if atualizado is None:
        raise HTTPException(
            status_code=HTTPStatus.NOT_FOUND,
            detail='Pagamento não encontrado para este título.',
        )
    session.commit()
    return {'detail': 'Pagamento atualizado com sucesso.'}


@router.delete(
    '/fornecedores/{codigo_fornecedor}/titulos/{codigo_parcela}'
    '/pagamentos/{pagamento_id}',
    status_code=HTTPStatus.NO_CONTENT,
)
def excluir_pagamento_titulo(
    codigo_fornecedor: int,
    codigo_parcela: int,
    pagamento_id: int,
    _: UsuarioAtual,
    session: SessionPostgres,
):
    excluido = session.execute(
        text("""
        DELETE FROM api_prontocardio.contas_pagar_pagamentos
         WHERE id = :pagamento_id
           AND codigo_fornecedor = :fornecedor
           AND codigo_parcela = :parcela
     RETURNING id
    """),
        {
            'pagamento_id': pagamento_id,
            'fornecedor': codigo_fornecedor,
            'parcela': codigo_parcela,
        },
    ).scalar_one_or_none()
    if excluido is None:
        raise HTTPException(
            status_code=HTTPStatus.NOT_FOUND,
            detail='Pagamento não encontrado para este título.',
        )
    session.commit()
    return Response(status_code=HTTPStatus.NO_CONTENT)


class OrcamentoInput(BaseModel):
    valor: Decimal = Field(ge=0, max_digits=15, decimal_places=2)


@router.put('/orcamento/{competencia}')
def salvar_orcamento(
    competencia: date,
    payload: OrcamentoInput,
    usuario: UsuarioAtual,
    session: SessionPostgres,
):
    if competencia.day != 1:
        raise HTTPException(422, 'Informe o primeiro dia da competência.')
    session.execute(
        text("""
        INSERT INTO api_prontocardio.contas_pagar_orcamentos (competencia, valor, usuario_id)
        VALUES (:competencia, :valor, :usuario)
        ON CONFLICT (competencia) DO UPDATE SET valor = EXCLUDED.valor,
            usuario_id = EXCLUDED.usuario_id,
            data_atualizacao = timezone('America/Sao_Paulo', now())
    """),
        {
            'competencia': competencia,
            'valor': payload.valor,
            'usuario': usuario.id,
        },
    )
    session.commit()
    return {
        'detail': 'Orçamento mensal salvo. Nenhum pagamento foi executado.'
    }


@router.get('/matriz')
def consultar_matriz(
    _: UsuarioAtual,
    session: SessionPostgres,
    oracle: SessionOracle,
    competencia: date | None = None,
    q: str | None = None,
    status: str | None = None,
    data_inicio: date | None = None,
    data_fim: date | None = None,
    criticidade: Literal['todos', 'criticos', 'nao_criticos'] = 'todos',
    nivel_criticidade: Literal[
        'todos', 'pendente', '1', '2', '3', '4', '5'
    ] = 'todos',
    faixa_atraso: Literal['todos', '1', '2', '3', '4', '5'] = 'todos',
    fornecedores_ids: Annotated[str | None, Query(max_length=50000)] = None,
    quadrante: Annotated[int | None, Query(ge=1, le=25)] = None,
):
    competencia = (competencia or _hoje_hospital()).replace(day=1)
    if data_inicio and data_fim and data_inicio > data_fim:
        raise HTTPException(
            422, 'A data inicial não pode ser posterior à data final.'
        )
    codigos_selecionados = _selecionar_codigos(fornecedores_ids)
    nivel_criticidade, faixa_atraso, quadrante_compativel = (
        _resolver_quadrante(nivel_criticidade, faixa_atraso, quadrante)
    )
    tratamentos = _tratamentos(session)
    fornecedores = _consultar_oracle(
        oracle, data_inicio=data_inicio, data_fim=data_fim
    )
    termo = (q or '').strip().casefold()
    selecionados = []
    for item in fornecedores:
        if not quadrante_compativel:
            continue
        if (
            codigos_selecionados
            and str(item['codigo_fornecedor']) not in codigos_selecionados
        ):
            continue
        tratamento = tratamentos.get(item['codigo_fornecedor'], {})
        if (
            termo
            and termo not in item['nome_fornecedor'].casefold()
            and termo not in str(item['codigo_fornecedor'])
        ):
            continue
        if status and tratamento.get('status', 'PENDENTE') != status.upper():
            continue
        if criticidade == 'criticos' and not tratamento.get('critico', False):
            continue
        if criticidade == 'nao_criticos' and (
            tratamento.get('critico', False)
            or (
                item.get('sem_cadastro')
                and tratamento.get('nivel_criticidade') is None
            )
        ):
            continue
        selecionados.append(item['codigo_fornecedor'])
    titulos = _consultar_titulos_oracle(
        oracle, selecionados, data_inicio=data_inicio, data_fim=data_fim
    )
    grupos = _agrupar_fornecedores(titulos, _pagamentos_manuais(session))
    for item in grupos:
        item.update(
            classification({
                **tratamentos.get(item['codigo_fornecedor'], {}),
                'sem_cadastro': item.get('sem_cadastro', False),
            })
        )
    if nivel_criticidade != 'todos':
        grupos = [
            item
            for item in grupos
            if (
                item['nivel_criticidade'] is None
                if nivel_criticidade == 'pendente'
                else item['nivel_criticidade'] == int(nivel_criticidade)
            )
        ]
    if faixa_atraso != 'todos':
        for item in grupos:
            item['titulos'] = [
                titulo
                for titulo in item['titulos']
                if age_level(titulo['dias_vencidos']) == int(faixa_atraso)
            ]
    orcamento = (
        session
        .execute(
            text("""
        SELECT o.valor, o.data_atualizacao, u.nome AS usuario_nome
          FROM api_prontocardio.contas_pagar_orcamentos o
          LEFT JOIN api_prontocardio.usuarios_api u ON u.id = o.usuario_id
         WHERE o.competencia = :competencia
    """),
            {'competencia': competencia},
        )
        .mappings()
        .all()
    )
    registro = dict(orcamento[0]) if orcamento else {}
    matriz = build_matrix(grupos, registro.get('valor', Decimal('0')))
    return jsonable_encoder(
        {
            **matriz,
            'competencia': competencia,
            'orcamento_cadastrado': bool(orcamento),
            'atualizado_por': registro.get('usuario_nome'),
            'data_atualizacao': registro.get('data_atualizacao'),
            'gerado_em': _hoje_hospital(),
        },
        custom_encoder={Decimal: _serializar_decimal},
    )
