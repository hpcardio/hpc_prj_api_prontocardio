from http import HTTPStatus
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app_prontocardio.database import get_session_oracle
from app_prontocardio.models import ModelContaAtendimento, Usuario
from app_prontocardio.schema import (
    Atendimento,
    Atendimentos,
)
from app_prontocardio.security import valida_token_usuario_atual

router = APIRouter(prefix='/app_glosas', tags=['app_glosas'])

ValidaUsuarioAtual = Annotated[Usuario, Depends(valida_token_usuario_atual)]


@router.get(
    '/conta_atendimento/{cd_atendimento}',
    status_code=HTTPStatus.OK,
    response_model=Atendimentos,
)
def conta_atendimento(
    usuario_atual: ValidaUsuarioAtual,
    cd_atendimento: int = Path(..., gt=0),
    session: Session = Depends(get_session_oracle),
):
    try:
        result = select(ModelContaAtendimento).where(
            ModelContaAtendimento.cd_atendimento == cd_atendimento
        )
        rows = session.execute(result).scalars().all()

    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=HTTPStatus.INTERNAL_SERVER_ERROR,
            detail='Erro ao consultar conta_atendimento.',
        ) from exc

    if not rows:
        raise HTTPException(
            status_code=HTTPStatus.NOT_FOUND,
            detail=(
                f'cd_atendimento: {cd_atendimento} não encontrado. '
                'Forneça um código de atendimento válido'
            ),
        )

    atendimentos_list = [
        Atendimento.model_validate(
            row,
            from_attributes=True,
        )
        for row in rows
    ]

    return {'atendimentos': atendimentos_list}
