from http import HTTPStatus
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app_prontocardio.database import get_session_oracle
from app_prontocardio.models import Usuario
from app_prontocardio.security import valida_token_usuario_atual

router = APIRouter(prefix='/api/mv', tags=['catalogos-mv'])

SessionOracle = Annotated[Session, Depends(get_session_oracle)]
UsuarioAtual = Annotated[Usuario, Depends(valida_token_usuario_atual)]


class OrigemMvPublic(BaseModel):
    cd_ori_ate: int
    ds_ori_ate: str
    tp_origem: str
    cd_setor: int | None = None
    sn_ativo: str
    cd_multi_empresa: int
    cd_ori_ate_integra: str | None = None
    sn_padrao: str
    tp_modo_atendimento: str


class ListaOrigensMvPublic(BaseModel):
    count: int
    results: list[OrigemMvPublic]


def _campo(linha: dict[str, object], nome: str):
    if nome in linha:
        return linha[nome]
    return linha.get(nome.lower())


@router.get('/origens', response_model=ListaOrigensMvPublic)
def listar_origens_mv(
    session: SessionOracle,
    _usuario: UsuarioAtual,
    somente_ativas: Annotated[bool, Query()] = True,
    cd_multi_empresa: Annotated[int | None, Query(ge=1)] = None,
    termo: Annotated[str | None, Query(min_length=1, max_length=100)] = None,
):
    clausulas: list[str] = []
    parametros: dict[str, object] = {}

    if somente_ativas:
        clausulas.append("o.SN_ATIVO = 'S'")
    if cd_multi_empresa is not None:
        clausulas.append('o.CD_MULTI_EMPRESA = :cd_multi_empresa')
        parametros['cd_multi_empresa'] = cd_multi_empresa
    if termo:
        clausulas.append('UPPER(o.DS_ORI_ATE) LIKE :termo')
        parametros['termo'] = f'%{termo.strip().upper()}%'

    where_sql = (
        f' WHERE {" AND ".join(clausulas)}'
        if clausulas
        else ''
    )
    consulta = text(f'''
        SELECT
            o.CD_ORI_ATE AS "cd_ori_ate",
            o.DS_ORI_ATE AS "ds_ori_ate",
            o.TP_ORIGEM AS "tp_origem",
            o.CD_SETOR AS "cd_setor",
            o.SN_ATIVO AS "sn_ativo",
            o.CD_MULTI_EMPRESA AS "cd_multi_empresa",
            o.CD_ORI_ATE_INTEGRA AS "cd_ori_ate_integra",
            o.SN_PADRAO AS "sn_padrao",
            o.TP_MODO_ATENDIMENTO AS "tp_modo_atendimento"
        FROM DBAMV.ORI_ATE o
        {where_sql}
        ORDER BY o.DS_ORI_ATE, o.CD_ORI_ATE
    ''')

    try:
        linhas = session.execute(consulta, parametros).mappings().all()
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=HTTPStatus.BAD_GATEWAY,
            detail='Falha ao consultar origens no MV.',
        ) from exc
    resultados = [
        {
            'cd_ori_ate': _campo(dict(linha), 'cd_ori_ate'),
            'ds_ori_ate': _campo(dict(linha), 'ds_ori_ate'),
            'tp_origem': _campo(dict(linha), 'tp_origem'),
            'cd_setor': _campo(dict(linha), 'cd_setor'),
            'sn_ativo': _campo(dict(linha), 'sn_ativo'),
            'cd_multi_empresa': _campo(dict(linha), 'cd_multi_empresa'),
            'cd_ori_ate_integra': _campo(
                dict(linha),
                'cd_ori_ate_integra',
            ),
            'sn_padrao': _campo(dict(linha), 'sn_padrao'),
            'tp_modo_atendimento': _campo(
                dict(linha),
                'tp_modo_atendimento',
            ),
        }
        for linha in linhas
    ]

    return {'count': len(resultados), 'results': resultados}
