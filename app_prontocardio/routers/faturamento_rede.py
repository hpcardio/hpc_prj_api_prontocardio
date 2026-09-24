from datetime import date, datetime
from decimal import Decimal
from http import HTTPStatus
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app_prontocardio.database import (
    get_session_oracle,
    get_session_postgres,
)
from app_prontocardio.models import Usuario
from app_prontocardio.security import (
    criar_token,
    valida_senha_cru_x_senha_hash_db,
    valida_token_usuario_atual,
)

router = APIRouter(prefix='/api', tags=['compatibilidade-prontocardio-rede'])

SessionOracle = Annotated[Session, Depends(get_session_oracle)]
SessionPostgres = Annotated[Session, Depends(get_session_postgres)]
UsuarioAtual = Annotated[Usuario, Depends(valida_token_usuario_atual)]

PAGE_SIZE_DEFAULT = 100
PAGE_SIZE_MAX = 500


class TokenRedeRequest(BaseModel):
    username: str
    password: str


class TokenRedeResponse(BaseModel):
    access: str
    refresh: str | None = None


class PacienteFaturamentoRede(BaseModel):
    cd_paciente: int | None = None
    nm_paciente: str | None = None
    email: str | None = None
    nr_cpf: str | None = None
    nr_celular: str | None = None
    tp_sexo: str | None = None
    dt_nascimento: date | None = None
    nm_tutor: str | None = None
    email_tutor: str | None = None
    nr_celular_tutor: str | None = None


class ItemFaturamentoRede(BaseModel):
    ID: str
    COD_REG: int | None = None
    NOME_PACIENTE: str | None = None
    COD_ATEND: int | None = None
    COD_CONVENIO: int | None = None
    NOME_CONVENIO: str | None = None
    COD_LAC: int | None = None
    DT_ATEND: date | None = None
    QNT: int | None = None
    CD_ORI_ATE: int | None = None
    DS_ORI_ATE: str | None = None
    COD_PROCEDIMENTO: str | None = None
    NOME_PROCEDIMENTO: str | None = None
    VL_CONTA: Decimal | None = None
    TIPO_MOV: str | None = None
    COD_PREST: int | None = None
    COD_SETOR_PROD: int | None = None
    DT_REMESSA: date | None = None
    SN_PACOTE: str | None = None
    CONTA_FECHADA: str | None = None
    COD_PAC: int | None = None
    paciente: PacienteFaturamentoRede | None = None


class PaginaFaturamentoRede(BaseModel):
    count: int
    next: str | None
    previous: str | None
    results: list[ItemFaturamentoRede]


class FiltrosFaturamentoRede:
    def __init__(  # noqa: PLR0913
        self,
        DT_ATEND_after: Annotated[date | None, Query()] = None,
        DT_ATEND_before: Annotated[date | None, Query()] = None,
        DT_REMESSA_after: Annotated[date | None, Query()] = None,
        DT_REMESSA_before: Annotated[date | None, Query()] = None,
        COD_PAC: Annotated[int | None, Query()] = None,
        COD_ATEND: Annotated[int | None, Query(gt=0)] = None,
        CD_ORI_ATE: Annotated[int | None, Query()] = None,
        page: Annotated[int, Query(ge=1)] = 1,
        page_size: Annotated[
            int,
            Query(ge=1, le=PAGE_SIZE_MAX),
        ] = PAGE_SIZE_DEFAULT,
    ) -> None:
        self.DT_ATEND_after = DT_ATEND_after
        self.DT_ATEND_before = DT_ATEND_before
        self.DT_REMESSA_after = DT_REMESSA_after
        self.DT_REMESSA_before = DT_REMESSA_before
        self.COD_PAC = COD_PAC
        self.COD_ATEND = COD_ATEND
        self.CD_ORI_ATE = CD_ORI_ATE
        self.page = page
        self.page_size = page_size


@router.post(
    '/auth/token/',
    status_code=HTTPStatus.OK,
    response_model=TokenRedeResponse,
)
def autenticar_usuario_rede(
    payload: TokenRedeRequest,
    session: SessionPostgres,
):
    usuario = session.scalar(
        select(Usuario).where(Usuario.email == payload.username)
    )
    excessao_autenticacao = HTTPException(
        status_code=HTTPStatus.UNAUTHORIZED,
        detail='Email ou Senha incorretos',
    )

    if not usuario or not usuario.ativo:
        raise excessao_autenticacao

    if not valida_senha_cru_x_senha_hash_db(payload.password, usuario.senha):
        raise excessao_autenticacao

    return {'access': criar_token({'sub': usuario.email}), 'refresh': None}


def _montar_where(
    filtros: FiltrosFaturamentoRede,
) -> tuple[str, dict[str, object]]:
    filtros_obrigatorios = [
        filtros.DT_ATEND_after,
        filtros.DT_ATEND_before,
        filtros.DT_REMESSA_after,
        filtros.DT_REMESSA_before,
        filtros.COD_PAC,
        filtros.COD_ATEND,
    ]
    if all(filtro is None for filtro in filtros_obrigatorios):
        raise HTTPException(
            status_code=HTTPStatus.BAD_REQUEST,
            detail=(
                'Use pelo menos um filtro: DT_ATEND_after, '
                'DT_ATEND_before, DT_REMESSA_after, DT_REMESSA_before '
                'COD_PAC ou COD_ATEND.'
            ),
        )

    clausulas = []
    parametros: dict[str, object] = {}

    if filtros.DT_ATEND_after is not None:
        clausulas.append('f.DT_ATEND >= :DT_ATEND_after')
        parametros['DT_ATEND_after'] = filtros.DT_ATEND_after
    if filtros.DT_ATEND_before is not None:
        clausulas.append('f.DT_ATEND <= :DT_ATEND_before')
        parametros['DT_ATEND_before'] = filtros.DT_ATEND_before
    if filtros.DT_REMESSA_after is not None:
        clausulas.append('f.DT_REMESSA >= :DT_REMESSA_after')
        parametros['DT_REMESSA_after'] = filtros.DT_REMESSA_after
    if filtros.DT_REMESSA_before is not None:
        clausulas.append('f.DT_REMESSA <= :DT_REMESSA_before')
        parametros['DT_REMESSA_before'] = filtros.DT_REMESSA_before
    if filtros.COD_PAC is not None:
        clausulas.append('f.COD_PAC = :COD_PAC')
        parametros['COD_PAC'] = filtros.COD_PAC
    if filtros.COD_ATEND is not None:
        clausulas.append('f.COD_ATEND = :COD_ATEND')
        parametros['COD_ATEND'] = filtros.COD_ATEND
    if filtros.CD_ORI_ATE is not None:
        clausulas.append('f.CD_ORI_ATE = :CD_ORI_ATE')
        parametros['CD_ORI_ATE'] = filtros.CD_ORI_ATE

    return f' WHERE {" AND ".join(clausulas)}', parametros


def _separar_contato_tutor(
    complemento_tutor: str | None,
) -> tuple[str | None, str | None]:
    if not complemento_tutor or '|' not in complemento_tutor:
        return None, None

    email, celular = complemento_tutor.split('|', maxsplit=1)
    return email or None, celular or None


def _montar_paciente(linha: dict[str, object]):
    email_tutor, nr_celular_tutor = _separar_contato_tutor(
        _campo(linha, 'paciente_ds_complemento_tutor')
    )
    paciente = {
        'cd_paciente': _campo(linha, 'paciente_cd_paciente'),
        'nm_paciente': _campo(linha, 'paciente_nm_paciente'),
        'email': _campo(linha, 'paciente_email'),
        'nr_cpf': _campo(linha, 'paciente_nr_cpf'),
        'nr_celular': _campo(linha, 'paciente_nr_celular'),
        'tp_sexo': _campo(linha, 'paciente_tp_sexo'),
        'dt_nascimento': _data(_campo(linha, 'paciente_dt_nascimento')),
        'nm_tutor': _campo(linha, 'paciente_nm_tutor'),
        'email_tutor': email_tutor,
        'nr_celular_tutor': nr_celular_tutor,
    }

    if all(valor is None for valor in paciente.values()):
        return None

    return paciente


def _campo(linha: dict[str, object], nome: str) -> object:
    return linha.get(nome) if nome in linha else linha.get(nome.lower())


def _data(valor: object) -> object:
    if isinstance(valor, datetime):
        return valor.date()
    return valor


def _montar_item(linha: dict[str, object]) -> dict[str, object]:
    return {
        'ID': _campo(linha, 'ID'),
        'COD_REG': _campo(linha, 'COD_REG'),
        'NOME_PACIENTE': _campo(linha, 'NOME_PACIENTE'),
        'COD_ATEND': _campo(linha, 'COD_ATEND'),
        'COD_CONVENIO': _campo(linha, 'COD_CONVENIO'),
        'NOME_CONVENIO': _campo(linha, 'NOME_CONVENIO'),
        'COD_LAC': _campo(linha, 'COD_LAC'),
        'DT_ATEND': _data(_campo(linha, 'DT_ATEND')),
        'QNT': _campo(linha, 'QNT'),
        'CD_ORI_ATE': _campo(linha, 'CD_ORI_ATE'),
        'DS_ORI_ATE': _campo(linha, 'DS_ORI_ATE'),
        'COD_PROCEDIMENTO': _campo(linha, 'COD_PROCEDIMENTO'),
        'NOME_PROCEDIMENTO': _campo(linha, 'NOME_PROCEDIMENTO'),
        'VL_CONTA': _campo(linha, 'VL_CONTA'),
        'TIPO_MOV': _campo(linha, 'TIPO_MOV'),
        'COD_PREST': _campo(linha, 'COD_PREST'),
        'COD_SETOR_PROD': _campo(linha, 'COD_SETOR_PROD'),
        'DT_REMESSA': _data(_campo(linha, 'DT_REMESSA')),
        'SN_PACOTE': _campo(linha, 'SN_PACOTE'),
        'CONTA_FECHADA': _campo(linha, 'CONTA_FECHADA'),
        'COD_PAC': _campo(linha, 'COD_PAC'),
        'paciente': _montar_paciente(linha),
    }


def _url_pagina(request: Request, page: int) -> str:
    url = request.url.include_query_params(page=page)
    forwarded_proto = request.headers.get('x-forwarded-proto', '')
    scheme = forwarded_proto.split(',', maxsplit=1)[0].strip().lower()

    if scheme not in {'http', 'https'}:
        scheme = url.scheme
    if url.hostname == 'apihpc.hospitalprontocardio.com.br':
        scheme = 'https'

    return str(url.replace(scheme=scheme))


@router.get('/faturamento', response_model=PaginaFaturamentoRede)
@router.get(
    '/faturamento/',
    response_model=PaginaFaturamentoRede,
    include_in_schema=False,
)
def listar_faturamento_rede(
    request: Request,
    session: SessionOracle,
    _usuario: UsuarioAtual,
    filtros: Annotated[FiltrosFaturamentoRede, Depends()],
):
    where_sql, parametros = _montar_where(filtros)
    inicio = ((filtros.page - 1) * filtros.page_size) + 1
    fim = filtros.page * filtros.page_size

    consulta_base = f'''
        FROM DBAMV.HPC_VFAT_TOTAL3 f
        LEFT JOIN DBAMV.PACIENTE p ON p.CD_PACIENTE = f.COD_PAC
        {where_sql}
    '''

    consulta_total = text(f'SELECT COUNT(*) {consulta_base}')
    consulta_itens = text(f'''
        SELECT *
        FROM (
            SELECT base.*, ROW_NUMBER() OVER (
                ORDER BY base.COD_REG DESC NULLS LAST,
                         base.COD_LAC DESC NULLS LAST
            ) AS rn
            FROM (
                SELECT
                    f.ID AS "ID",
                    f.COD_REG AS "COD_REG",
                    f.NOME_PACIENTE AS "NOME_PACIENTE",
                    f.COD_ATEND AS "COD_ATEND",
                    f.COD_CONVENIO AS "COD_CONVENIO",
                    f.NOME_CONVENIO AS "NOME_CONVENIO",
                    f.COD_LAC AS "COD_LAC",
                    f.DT_ATEND AS "DT_ATEND",
                    f.QNT AS "QNT",
                    f.CD_ORI_ATE AS "CD_ORI_ATE",
                    f.DS_ORI_ATE AS "DS_ORI_ATE",
                    f.COD_PROCEDIMENTO AS "COD_PROCEDIMENTO",
                    f.NOME_PROCEDIMENTO AS "NOME_PROCEDIMENTO",
                    f.VL_CONTA AS "VL_CONTA",
                    f.TIPO_MOV AS "TIPO_MOV",
                    f.COD_PREST AS "COD_PREST",
                    f.COD_SETOR_PROD AS "COD_SETOR_PROD",
                    f.DT_REMESSA AS "DT_REMESSA",
                    f.SN_PACOTE AS "SN_PACOTE",
                    f.CONTA_FECHADA AS "CONTA_FECHADA",
                    f.COD_PAC AS "COD_PAC",
                    p.CD_PACIENTE AS "paciente_cd_paciente",
                    p.NM_PACIENTE AS "paciente_nm_paciente",
                    p.EMAIL AS "paciente_email",
                    p.NR_CPF AS "paciente_nr_cpf",
                    p.NR_CELULAR AS "paciente_nr_celular",
                    p.TP_SEXO AS "paciente_tp_sexo",
                    p.DT_NASCIMENTO AS "paciente_dt_nascimento",
                    p.NM_TUTOR AS "paciente_nm_tutor",
                    p.DS_COMPLEMENTO_TUTOR
                        AS "paciente_ds_complemento_tutor"
                {consulta_base}
            ) base
        )
        WHERE rn BETWEEN :inicio AND :fim
    ''')

    try:
        total = session.scalar(consulta_total, parametros) or 0
        linhas = session.execute(
            consulta_itens,
            parametros | {'inicio': inicio, 'fim': fim},
        )
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=HTTPStatus.BAD_GATEWAY,
            detail='Falha ao consultar faturamento no MV.',
        ) from exc

    resultados = [
        _montar_item(dict(linha._mapping)) for linha in linhas.fetchall()
    ]
    proxima = (
        _url_pagina(request, filtros.page + 1)
        if fim < total
        else None
    )
    anterior = (
        _url_pagina(request, filtros.page - 1)
        if filtros.page > 1
        else None
    )

    return {
        'count': total,
        'next': proxima,
        'previous': anterior,
        'results': resultados,
    }
