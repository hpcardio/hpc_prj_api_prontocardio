from http import HTTPStatus
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app_prontocardio.database import get_session_oracle
from app_prontocardio.models import ModelContaAtendimento, Usuario
from app_prontocardio.schema import (
    Atendimento,
    Atendimentos,
    FilterSearch
)
from app_prontocardio.security import valida_token_usuario_atual

router = APIRouter(prefix='/app_glosas', tags=['app_glosas'])

ValidaUsuarioAtual = Annotated[Usuario, Depends(valida_token_usuario_atual)]
TEXT_FILTER_FIELDS = {'nm_paciente', 'nm_convenio', 'descricao'}


def _is_oracle_connect_timeout(exc: SQLAlchemyError) -> bool:
    error_texts = [str(exc)]

    orig_error = getattr(exc, 'orig', None)
    if orig_error is not None:
        error_texts.append(str(orig_error))

    cause = exc.__cause__
    if cause is not None:
        error_texts.append(str(cause))

    return any('ORA-12170' in text for text in error_texts)


@router.get(
    '/',
    status_code=HTTPStatus.OK,
    response_model=Atendimentos,
)
def conta_atendimento(
    usuario_atual: ValidaUsuarioAtual,
    campos_pesquisados: Annotated[FilterSearch, Depends()],
    session: Session = Depends(get_session_oracle)

):

    try:
        filtros = campos_pesquisados.model_dump(
            exclude_unset=True,
            exclude_none=True,
        )

        offset = filtros.pop('offset', campos_pesquisados.offset)
        limit = filtros.pop('limit', campos_pesquisados.limit)

        query = select(ModelContaAtendimento)

        for chave, valor in filtros.items():
            if hasattr(ModelContaAtendimento, chave):
                coluna = getattr(ModelContaAtendimento, chave)
                if chave in TEXT_FILTER_FIELDS and isinstance(valor, str):
                    query = query.where(coluna.ilike(f'%{valor}%'))
                else:
                    query = query.where(coluna == valor)

        query = query.offset(offset).limit(limit)

        rows = session.execute(query).scalars().all()

    except SQLAlchemyError as exc:
        if _is_oracle_connect_timeout(exc):
            raise HTTPException(
                status_code=HTTPStatus.SERVICE_UNAVAILABLE,
                detail='Banco Oracle indisponivel no momento.',
            ) from exc

        raise HTTPException(
            status_code=HTTPStatus.INTERNAL_SERVER_ERROR,
            detail='Erro na consultar.',
        ) from exc

    if not rows:
        raise HTTPException(
            status_code=HTTPStatus.NOT_FOUND,
            detail='Nenhum registro encontrado para os filtros informados.',
        )

    atendimentos_list = [
        Atendimento.model_validate(
            row,
            from_attributes=True,
        )
        for row in rows
    ]

    return {'atendimentos': atendimentos_list}
