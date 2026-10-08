from __future__ import annotations

import hmac
import json
from datetime import UTC, date, datetime
from decimal import Decimal
from hashlib import sha256
from http import HTTPStatus
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app_prontocardio.database import get_session_oracle, get_session_postgres
from app_prontocardio.models import RepasseMvOperacao, Usuario
from app_prontocardio.security import (
    settings,
    valida_acesso_financeiro_mv,
    valida_token_usuario_atual,
    valida_usuario_ti,
)
from app_prontocardio.services.repasse_mv_financeiro import (
    CancelamentoBloqueado,
    ConcorrenciaFinanceira,
    TituloNaoEncontrado,
    cancelar_titulo,
    carregar_titulo,
    classificar_titulo,
    listar_titulos,
    token_cancelamento,
    validar_cancelamento,
)

router = APIRouter(prefix='/repasse-medico', tags=['repasse-medico'])
TAMANHO_MINIMO_MOTIVO = 5
JANELA_ASSINATURA_SEGUNDOS = 300
SessionOracle = Annotated[Session, Depends(get_session_oracle)]
SessionPostgres = Annotated[Session, Depends(get_session_postgres)]
UsuarioAtual = Annotated[Usuario, Depends(valida_token_usuario_atual)]
UsuarioTi = Annotated[Usuario, Depends(valida_usuario_ti)]
UsuarioFinanceiro = Annotated[
    Usuario, Depends(valida_acesso_financeiro_mv)
]


class EnvioRepasseInput(BaseModel):
    competencia: date
    data_pagamento: date
    empresa: int = Field(gt=0)
    token_confirmacao: str = Field(min_length=64, max_length=64)


class CancelamentoRepasseInput(BaseModel):
    codigo_repasse_esperado: int = Field(gt=0)
    motivo: str = Field(min_length=5, max_length=1000)
    token_confirmacao: str = Field(min_length=64, max_length=64)

    @field_validator('motivo')
    @classmethod
    def validar_motivo(cls, valor: str) -> str:
        normalizado = valor.strip()
        if len(normalizado) < TAMANHO_MINIMO_MOTIVO:
            raise ValueError('Informe um motivo com pelo menos 5 caracteres.')
        return normalizado


def _operador_auditado(
    usuario: Usuario,
    nome: str | None,
    email: str | None,
    instante: str | None,
    assinatura: str | None,
) -> tuple[str, str]:
    segredo = settings.PRONTOESCALA_AUDIT_SECRET
    if not all((segredo, nome, email, instante, assinatura)):
        return usuario.nome, usuario.email
    try:
        emitido = datetime.fromtimestamp(int(instante), tz=UTC)
    except (TypeError, ValueError, OSError):
        return usuario.nome, usuario.email
    idade = abs((datetime.now(UTC) - emitido).total_seconds())
    if idade > JANELA_ASSINATURA_SEGUNDOS:
        return usuario.nome, usuario.email
    mensagem = f'{instante}\n{email}\n{nome}'.encode()
    esperado = hmac.new(
        segredo.get_secret_value().encode(), mensagem, sha256
    ).hexdigest()
    if not hmac.compare_digest(esperado, assinatura):
        return usuario.nome, usuario.email
    return nome, email


TITULOS_ENVIADOS_QUERY = text("""
SELECT rpc.cd_con_pag,
       MIN(rpc.cd_repasse) AS cd_repasse,
       COUNT(DISTINCT rpc.cd_repasse) AS qtd_repasses,
       MAX(cp.nr_documento) AS nr_documento,
       MAX(cp.cd_multi_empresa) AS cd_multi_empresa,
       MAX(cp.cd_usuario_ins) AS usuario_mv,
       MAX(cp.vl_bruto_conta) AS vl_bruto_conta,
       MIN(r.dt_competencia) AS competencia
  FROM dbamv.repasse_prestador_con_pag rpc
  JOIN dbamv.con_pag cp ON cp.cd_con_pag = rpc.cd_con_pag
  JOIN dbamv.repasse r ON r.cd_repasse = rpc.cd_repasse
 WHERE r.cd_multi_empresa = :empresa
   AND r.dt_competencia >= :competencia
   AND r.dt_competencia < ADD_MONTHS(:competencia, 1)
 GROUP BY rpc.cd_con_pag
 ORDER BY rpc.cd_con_pag
""")


def _json_seguro(valor):
    return json.loads(json.dumps(valor, ensure_ascii=False, default=str))


def _registrar_operacao_cancelamento(  # noqa: PLR0913, PLR0917
    session: Session,
    usuario: Usuario,
    cd_con_pag: int,
    payload: CancelamentoRepasseInput,
    operador_nome: str | None = None,
    operador_email: str | None = None,
) -> RepasseMvOperacao:
    operacao = RepasseMvOperacao(
        operacao_id=str(uuid4()),
        acao='cancelar',
        estado='em_processamento',
        usuario_id=usuario.id,
        usuario_nome=(operador_nome or usuario.nome)[:255],
        usuario_email=(operador_email or usuario.email)[:255],
        cd_con_pag=cd_con_pag,
        cd_repasse=payload.codigo_repasse_esperado,
        motivo=payload.motivo.strip(),
    )
    session.add(operacao)
    session.commit()
    return operacao


def _registrar_operacao_envio(
    session: Session,
    usuario: Usuario,
    payload: EnvioRepasseInput,
    operador_nome: str | None,
    operador_email: str | None,
) -> RepasseMvOperacao:
    operacao = RepasseMvOperacao(
        operacao_id=str(uuid4()),
        acao='enviar',
        estado='em_processamento',
        usuario_id=usuario.id,
        usuario_nome=(operador_nome or usuario.nome)[:255],
        usuario_email=(operador_email or usuario.email)[:255],
        competencia=payload.competencia.replace(day=1),
        cd_multi_empresa=payload.empresa,
    )
    try:
        session.add(operacao)
        session.commit()
    except IntegrityError as erro:
        session.rollback()
        raise HTTPException(
            status_code=HTTPStatus.CONFLICT,
            detail=(
                'Já existe um envio desta empresa e competência em '
                'processamento ou aguardando reconciliação. Não repita '
                'a operação.'
            ),
        ) from erro
    return operacao


def _capturar_titulos_enviados(
    session: Session, competencia: date, empresa: int
) -> list[dict]:
    return [
        dict(row)
        for row in session.execute(
            TITULOS_ENVIADOS_QUERY,
            {
                'competencia': competencia.replace(day=1),
                'empresa': empresa,
            },
        ).mappings().all()
    ]


def _concluir_operacao(  # noqa: PLR0913
    session: Session,
    operacao: RepasseMvOperacao,
    *,
    estado: str,
    mensagem: str,
    anterior=None,
    posterior=None,
) -> None:
    operacao.estado = estado
    operacao.mensagem = mensagem[:500]
    operacao.estado_anterior = _json_seguro(anterior) if anterior else None
    operacao.estado_posterior = _json_seguro(posterior) if posterior else None
    referencia = posterior or anterior or {}
    operacao.nr_documento = referencia.get('nr_documento')
    operacao.competencia = referencia.get('competencia')
    operacao.cd_multi_empresa = referencia.get('cd_multi_empresa')
    operacao.usuario_mv = referencia.get('usuario_mv')
    operacao.concluido_em = datetime.now()
    session.commit()


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

DETAIL_QUERY = text("""
SELECT rc.cd_repasse_consolidado,
       rp.cd_repasse,
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
       NVL(rc.qt_lancamento, 1) AS quantidade,
       NVL(rc.vl_total_conta, 0) AS valor_bruto,
       NVL(rc.vl_glosa, 0) AS valor_desconto,
       NVL(rc.vl_repasse, 0) - NVL(rc.vl_glosa, 0) AS valor_liquido,
       NVL(rc.vl_perc_repasse, 0) AS vl_perc_repasse
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
   AND (
       :cd_prestador IS NULL
       OR NVL(rp.cd_prestador_repasse, rp.cd_prestador) = :cd_prestador
   )
 ORDER BY nm_prestador_destino,
          rc.dt_lancamento,
          rc.cd_repasse_consolidado
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
                detail=(
                    'CPF do médico não localizado entre os prestadores '
                    'ativos do MV.'
                ),
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
    detail_rows = [
        dict(row)
        for row in session.execute(
            DETAIL_QUERY,
            {
                'competencia': competencia,
                'empresa': empresa,
                'cd_prestador': cd_prestador,
            },
        ).mappings().all()
    ]
    atendimentos = []
    atendimentos_teste_ergometrico = []
    for item in detail_rows:
        atendimento = {
            'codigo_repasse_consolidado': item.get(
                'cd_repasse_consolidado'
            ),
            'codigo_repasse': item.get('cd_repasse'),
            'codigo_prestador': item.get('cd_prestador_destino'),
            'prestador': item.get('nm_prestador_destino'),
            'codigo_atendimento': item.get('cd_atendimento'),
            'paciente': item.get('nm_paciente'),
            'data_atendimento': item.get('dt_lancamento'),
            'codigo_procedimento': item.get('cd_pro_fat'),
            'procedimento': (
                item.get('ds_procedimento') or 'Sem detalhamento'
            ),
            'codigo_convenio': item.get('cd_convenio'),
            'convenio': item.get('nm_convenio'),
            'codigo_remessa': item.get('cd_remessa'),
            'numero_remessa': item.get('nr_remessa_convenio'),
            'situacao_remessa': (
                'inside'
                if item.get('cd_remessa') is not None
                else 'outside'
            ),
            'quantidade': int(item.get('quantidade') or 0),
            'valor_bruto': _decimal(item.get('valor_bruto')),
            'valor_desconto': _decimal(item.get('valor_desconto')),
            'valor_liquido': _decimal(item.get('valor_liquido')),
            'percentual_repasse': _decimal(item.get('vl_perc_repasse')),
        }
        if _eh_teste_ergometrico(item.get('ds_procedimento')):
            atendimentos_teste_ergometrico.append(atendimento)
        else:
            atendimentos.append(atendimento)
    return {
        'competencia': competencia,
        'empresa': empresa,
        'itens': itens,
        'atendimentos': atendimentos,
        'atendimentos_teste_ergometrico': atendimentos_teste_ergometrico,
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
def enviar_repasse(  # noqa: PLR0913, PLR0917
    payload: EnvioRepasseInput,
    usuario: UsuarioTi,
    session: SessionOracle,
    session_postgres: SessionPostgres,
    operador_nome: Annotated[
        str | None, Header(alias='X-ProntoEscala-Operador-Nome')
    ] = None,
    operador_email: Annotated[
        str | None, Header(alias='X-ProntoEscala-Operador-Email')
    ] = None,
    operador_instante: Annotated[
        str | None, Header(alias='X-ProntoEscala-Operador-Timestamp')
    ] = None,
    operador_assinatura: Annotated[
        str | None, Header(alias='X-ProntoEscala-Operador-Assinatura')
    ] = None,
):
    operador_nome, operador_email = _operador_auditado(
        usuario,
        operador_nome,
        operador_email,
        operador_instante,
        operador_assinatura,
    )
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

    operacao = None
    titulos_antes: set[int] = set()
    if session_postgres is not None:
        operacao = _registrar_operacao_envio(
            session_postgres,
            usuario,
            payload,
            operador_nome,
            operador_email,
        )
        titulos_antes = {
            int(item['cd_con_pag'])
            for item in _capturar_titulos_enviados(
                session, payload.competencia, payload.empresa
            )
        }

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
        if operacao is not None:
            _concluir_operacao(
                session_postgres,
                operacao,
                estado='erro',
                mensagem='Falha ao executar o envio do repasse no MV.',
            )
        raise

    if operacao is not None:
        try:
            titulos_depois = _capturar_titulos_enviados(
                session, payload.competencia, payload.empresa
            )
            repasses_conferidos = {
                int(item['codigo_repasse']) for item in preview['itens']
            }
            novos = [
                item
                for item in titulos_depois
                if int(item['cd_con_pag']) not in titulos_antes
                and int(item['cd_repasse']) in repasses_conferidos
            ]
            if not novos:
                operacao.estado = 'reconciliacao_pendente'
                operacao.mensagem = (
                    'A procedure foi confirmada, mas nenhum título novo '
                    'foi identificado. Não repetir automaticamente.'
                )
                session_postgres.commit()
                raise HTTPException(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    detail=operacao.mensagem,
                )
            for indice, titulo in enumerate(novos):
                evento = operacao
                if indice:
                    evento = RepasseMvOperacao(
                        operacao_id=str(uuid4()),
                        acao='enviar',
                        estado='em_processamento',
                        usuario_id=operacao.usuario_id,
                        usuario_nome=operacao.usuario_nome,
                        usuario_email=operacao.usuario_email,
                        competencia=payload.competencia.replace(day=1),
                        cd_multi_empresa=payload.empresa,
                    )
                    session_postgres.add(evento)
                evento.cd_con_pag = titulo['cd_con_pag']
                evento.cd_repasse = titulo['cd_repasse']
                evento.nr_documento = titulo['nr_documento']
                evento.usuario_mv = titulo['usuario_mv']
                evento.estado_posterior = _json_seguro(titulo)
                evento.estado = 'sucesso'
                evento.mensagem = 'Título de repasse enviado ao MV.'
                evento.concluido_em = datetime.now()
            session_postgres.commit()
        except HTTPException:
            raise
        except Exception as erro:
            session_postgres.rollback()
            raise HTTPException(
                HTTPStatus.SERVICE_UNAVAILABLE,
                detail=(
                    'O MV confirmou o envio, mas a auditoria ficou '
                    'pendente de reconciliação. Não repita a operação.'
                ),
            ) from erro
    return {
        'status': 'enviado',
        'competencia': payload.competencia.replace(day=1),
        'data_pagamento': payload.data_pagamento,
        'empresa': payload.empresa,
        'total_liquido_conferido': preview['total_liquido'],
        'procedimento': 'DBAMV.PGTO_REP_GERAL',
    }


@router.get('/financeiro/titulos')
def listar_titulos_financeiros(  # noqa: PLR0913, PLR0917
    usuario: UsuarioFinanceiro,
    oracle: SessionOracle,
    postgres: SessionPostgres,
    competencia: date | None = Query(default=None),
    fornecedor: str | None = Query(default=None, max_length=120),
    cd_con_pag: int | None = Query(default=None, gt=0),
    nr_documento: str | None = Query(default=None, max_length=100),
    cd_repasse: int | None = Query(default=None, gt=0),
    situacao: str | None = Query(default=None),
    pagina: int = Query(default=1, ge=1),
    limite: int = Query(default=50, ge=1, le=200),
):
    del usuario
    resultado = listar_titulos(
        oracle,
        competencia=competencia.replace(day=1) if competencia else None,
        fornecedor=fornecedor,
        cd_con_pag=cd_con_pag,
        nr_documento=nr_documento,
        cd_repasse=cd_repasse,
        situacao=situacao,
        pagina=pagina,
        limite=limite,
    )
    codigos = [item['cd_con_pag'] for item in resultado['itens']]
    auditorias_por_titulo: dict[int, list[dict]] = {}
    if codigos:
        operacoes = postgres.scalars(
            select(RepasseMvOperacao)
            .where(RepasseMvOperacao.cd_con_pag.in_(codigos))
            .order_by(RepasseMvOperacao.criado_em.desc())
        ).all()
        for operacao in operacoes:
            auditorias_por_titulo.setdefault(operacao.cd_con_pag, []).append(
                {
                    'acao': operacao.acao,
                    'estado': operacao.estado,
                    'usuario': operacao.usuario_nome,
                    'usuario_email': operacao.usuario_email,
                    'motivo': operacao.motivo,
                    'criado_em': operacao.criado_em,
                    'concluido_em': operacao.concluido_em,
                }
            )
    for item in resultado['itens']:
        item['auditoria'] = auditorias_por_titulo.get(
            item['cd_con_pag'], []
        )
        item['origem_auditoria'] = (
            'prontoescala' if item['auditoria'] else 'mv_legado'
        )
    return resultado


@router.get('/financeiro/titulos/{cd_con_pag}')
def consultar_titulo_financeiro(
    cd_con_pag: int,
    usuario: UsuarioFinanceiro,
    oracle: SessionOracle,
):
    del usuario
    try:
        titulo = carregar_titulo(oracle, cd_con_pag)
    except TituloNaoEncontrado as erro:
        raise HTTPException(HTTPStatus.NOT_FOUND, detail=str(erro)) from erro
    titulo['situacao'] = classificar_titulo(titulo)
    titulo['token_confirmacao'] = token_cancelamento(titulo)
    return titulo


@router.get('/financeiro/titulos/{cd_con_pag}/cancelamento')
def prevalidar_cancelamento_financeiro(
    cd_con_pag: int,
    codigo_repasse_esperado: int,
    usuario: UsuarioFinanceiro,
    oracle: SessionOracle,
):
    del usuario
    if not settings.REPASSE_MV_CANCELAMENTO_ENABLED:
        raise HTTPException(
            HTTPStatus.SERVICE_UNAVAILABLE,
            detail=(
                'Cancelamento no MV ainda não foi habilitado após a '
                'validação transacional com o Contas a Pagar.'
            ),
        )
    try:
        titulo = carregar_titulo(oracle, cd_con_pag)
        elegivel = validar_cancelamento(
            titulo, codigo_repasse_esperado
        )
        return {
            'elegivel': elegivel,
            'idempotente': not elegivel,
            'motivo_bloqueio': None,
            'situacao': classificar_titulo(titulo),
            'token_confirmacao': token_cancelamento(titulo),
            'titulo': titulo,
        }
    except TituloNaoEncontrado as erro:
        raise HTTPException(HTTPStatus.NOT_FOUND, detail=str(erro)) from erro
    except CancelamentoBloqueado as erro:
        return {
            'elegivel': False,
            'idempotente': False,
            'motivo_bloqueio': str(erro),
            'situacao': classificar_titulo(titulo),
            'token_confirmacao': token_cancelamento(titulo),
            'titulo': titulo,
        }


@router.post('/financeiro/titulos/{cd_con_pag}/cancelar')
def cancelar_titulo_financeiro(  # noqa: PLR0913, PLR0917
    cd_con_pag: int,
    payload: CancelamentoRepasseInput,
    usuario: UsuarioFinanceiro,
    oracle: SessionOracle,
    postgres: SessionPostgres,
    operador_nome: Annotated[
        str | None, Header(alias='X-ProntoEscala-Operador-Nome')
    ] = None,
    operador_email: Annotated[
        str | None, Header(alias='X-ProntoEscala-Operador-Email')
    ] = None,
    operador_instante: Annotated[
        str | None, Header(alias='X-ProntoEscala-Operador-Timestamp')
    ] = None,
    operador_assinatura: Annotated[
        str | None, Header(alias='X-ProntoEscala-Operador-Assinatura')
    ] = None,
):
    if not settings.REPASSE_MV_CANCELAMENTO_ENABLED:
        raise HTTPException(
            HTTPStatus.SERVICE_UNAVAILABLE,
            detail=(
                'Cancelamento no MV ainda não foi habilitado após a '
                'validação transacional com o Contas a Pagar.'
            ),
        )
    operador_nome, operador_email = _operador_auditado(
        usuario,
        operador_nome,
        operador_email,
        operador_instante,
        operador_assinatura,
    )
    operacao = _registrar_operacao_cancelamento(
        postgres,
        usuario,
        cd_con_pag,
        payload,
        operador_nome,
        operador_email,
    )
    try:
        resultado = cancelar_titulo(
            oracle,
            cd_con_pag=cd_con_pag,
            codigo_repasse_esperado=payload.codigo_repasse_esperado,
            token_esperado=payload.token_confirmacao,
        )
    except TituloNaoEncontrado as erro:
        _concluir_operacao(
            postgres, operacao, estado='bloqueado', mensagem=str(erro)
        )
        raise HTTPException(HTTPStatus.NOT_FOUND, detail=str(erro)) from erro
    except CancelamentoBloqueado as erro:
        _concluir_operacao(
            postgres, operacao, estado='bloqueado', mensagem=str(erro)
        )
        raise HTTPException(HTTPStatus.CONFLICT, detail=str(erro)) from erro
    except ConcorrenciaFinanceira as erro:
        _concluir_operacao(
            postgres, operacao, estado='bloqueado', mensagem=str(erro)
        )
        raise HTTPException(HTTPStatus.CONFLICT, detail=str(erro)) from erro
    except Exception as erro:
        _concluir_operacao(
            postgres,
            operacao,
            estado='erro',
            mensagem='Falha ao cancelar o título no MV.',
        )
        raise HTTPException(
            HTTPStatus.INTERNAL_SERVER_ERROR,
            detail='Falha ao cancelar o título no MV.',
        ) from erro

    try:
        _concluir_operacao(
            postgres,
            operacao,
            estado='sucesso',
            mensagem=(
                'Título já estava cancelado.'
                if resultado['idempotente']
                else 'Título cancelado no MV.'
            ),
            anterior=resultado.get('estado_anterior'),
            posterior=resultado['titulo'],
        )
    except Exception as erro:
        postgres.rollback()
        raise HTTPException(
            HTTPStatus.SERVICE_UNAVAILABLE,
            detail=(
                'O MV confirmou o cancelamento, mas a auditoria ficou '
                'pendente de reconciliação. Não repita a operação.'
            ),
        ) from erro
    return {
        'status': 'cancelado',
        'idempotente': resultado['idempotente'],
        'operacao_id': operacao.operacao_id,
        'titulo': resultado['titulo'],
    }
