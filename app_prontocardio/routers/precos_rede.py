from http import HTTPStatus
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app_prontocardio.database import get_session_oracle
from app_prontocardio.models import Usuario
from app_prontocardio.schemas.precos_rede import PrecoRedeResultado
from app_prontocardio.security import valida_token_usuario_atual
from app_prontocardio.services.precos_rede_mv import listar_precos_rede_mv

router = APIRouter(prefix='/prontorede', tags=['prontorede'])

SessionOracle = Annotated[Session, Depends(get_session_oracle)]
UsuarioAtual = Annotated[Usuario, Depends(valida_token_usuario_atual)]


@router.get('/precos', response_model=PrecoRedeResultado)
def listar_precos_rede(
    session: SessionOracle,
    _usuario: UsuarioAtual,
    codigo: Annotated[list[str] | None, Query()] = None,
    codigo_pacote: Annotated[list[str] | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
    cursor: Annotated[str | None, Query(max_length=100)] = None,
):
    try:
        return listar_precos_rede_mv(
            session=session,
            codigos=codigo or [],
            codigos_pacote=codigo_pacote or [],
            limit=limit,
            cursor=cursor,
        )
    except (SQLAlchemyError, ValueError) as exc:
        raise HTTPException(
            status_code=HTTPStatus.BAD_GATEWAY,
            detail='Falha ao consultar a tabela de preços no MV.',
        ) from exc
