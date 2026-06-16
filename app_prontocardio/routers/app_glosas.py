from http import HTTPStatus
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import String, cast, func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app_prontocardio.database import get_session_oracle, get_session_postgres
from app_prontocardio.models import (
    ModelContaAtendimento,
    RegistroGlosa,
    Tiss,
    TipoAtendimento,
    Usuario,
)
from app_prontocardio.schema import (
    Atendimento,
    Atendimentos,
    FilterSearch,
    Message,
    RegistroGlosaCreate,
    RegistroGlosaPublic,
    RegistroGlosas,
    TissList,
)
from app_prontocardio.security import valida_token_usuario_atual

router = APIRouter(prefix='/app_glosas', tags=['app_glosas'])

ValidaUsuarioAtual = Annotated[Usuario, Depends(valida_token_usuario_atual)]
SessionPostgres = Annotated[Session, Depends(get_session_postgres)]
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


def _get_registro_glosa_or_404(
    glosa_id: int,
    session: Session,
) -> RegistroGlosa:
    registro_glosa = session.get(RegistroGlosa, glosa_id)
    if registro_glosa is None:
        raise HTTPException(
            status_code=HTTPStatus.NOT_FOUND,
            detail='Registro de glosa nao encontrado.',
        )

    return registro_glosa


def _aplicar_filtros_conta_atendimento(query, filtros: dict):
    for chave, valor in filtros.items():
        if hasattr(ModelContaAtendimento, chave):
            coluna = getattr(ModelContaAtendimento, chave)
            if chave == 'tp_atendimento':
                if isinstance(valor, TipoAtendimento):
                    query = query.where(coluna == valor.value)
                continue

            if chave in TEXT_FILTER_FIELDS and isinstance(valor, str):
                query = query.where(coluna.ilike(f'%{valor}%'))
            else:
                query = query.where(coluna == valor)

    return query


@router.get(
    '/',
    status_code=HTTPStatus.OK,
    response_model=Atendimentos,
)
def conta_atendimento(
    usuario_atual: ValidaUsuarioAtual,
    campos_pesquisados: Annotated[FilterSearch, Depends()],
    tp_atendimento: TipoAtendimento = Query(
        default=None,
    ),
    session: Session = Depends(get_session_oracle),
):

    try:
        filtros = campos_pesquisados.model_dump(
            exclude_unset=True,
            exclude_none=True,
        )

        offset = filtros.pop('offset', campos_pesquisados.offset)
        limit = filtros.pop('limit', campos_pesquisados.limit)
        if tp_atendimento is not None:
            filtros['tp_atendimento'] = tp_atendimento

        pacientes_filtrados = _aplicar_filtros_conta_atendimento(
            select(ModelContaAtendimento.cd_paciente).group_by(
                ModelContaAtendimento.cd_paciente
            ),
            filtros,
        ).subquery()
        total = (
            session.scalar(
                select(func.count()).select_from(pacientes_filtrados)
            )
            or 0
        )

        pacientes_query = _aplicar_filtros_conta_atendimento(
            select(ModelContaAtendimento.cd_paciente)
            .group_by(ModelContaAtendimento.cd_paciente)
            .order_by(
                func.min(ModelContaAtendimento.nm_paciente),
                ModelContaAtendimento.cd_paciente,
            ),
            filtros,
        ).offset(offset)
        if limit is not None:
            pacientes_query = pacientes_query.limit(limit)

        pacientes_paginados = pacientes_query.subquery()

        query = _aplicar_filtros_conta_atendimento(
            select(ModelContaAtendimento),
            filtros,
        ).where(
            ModelContaAtendimento.cd_paciente.in_(
                select(pacientes_paginados.c.cd_paciente)
            )
        )
        query = query.order_by(
            ModelContaAtendimento.nm_paciente,
            ModelContaAtendimento.cd_remessa,
            ModelContaAtendimento.cd_atendimento,
            ModelContaAtendimento.cd_reg,
            ModelContaAtendimento.cd_lancamento,
        )

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

    return {
        'atendimentos': atendimentos_list,
        'total': total,
        'limit': limit,
        'offset': offset,
    }


@router.get(
    '/glosas',
    status_code=HTTPStatus.OK,
    response_model=RegistroGlosas,
)
def consultar_glosas_registradas(
    usuario_atual: ValidaUsuarioAtual,
    campos_pesquisados: Annotated[FilterSearch, Depends()],
    session: SessionPostgres,
    tp_atendimento: TipoAtendimento = Query(
        default=None,
    ),
):
    filtros = campos_pesquisados.model_dump(
        exclude_unset=True,
        exclude_none=True,
    )

    offset = filtros.pop('offset', campos_pesquisados.offset)
    limit = filtros.pop('limit', campos_pesquisados.limit)
    if tp_atendimento is not None:
        filtros['tp_atendimento'] = tp_atendimento

    query = select(RegistroGlosa)

    field_mapping = {
        'cd_remessa': RegistroGlosa.cd_remessa,
        'cd_atendimento': RegistroGlosa.cd_atendimento,
        'cd_reg': RegistroGlosa.conta,
        'nm_convenio': RegistroGlosa.convenio,
        'descricao': RegistroGlosa.descricao_glosa,
        'tp_atendimento': RegistroGlosa.tp_atendimento,
    }
    text_fields = {'nm_convenio', 'descricao', 'tp_atendimento'}

    for chave, valor in filtros.items():
        coluna = field_mapping.get(chave)
        if coluna is not None:
            if chave == 'tp_atendimento':
                if isinstance(valor, TipoAtendimento):
                    query = query.where(coluna == valor.value)
                continue

            if chave in text_fields and isinstance(valor, str):
                query = query.where(coluna.ilike(f'%{valor}%'))
            else:
                query = query.where(coluna == valor)
            continue

        if chave == 'nm_paciente' and isinstance(valor, str):
            query = query.where(
                cast(RegistroGlosa.codigo_paciente, String).ilike(f'%{valor}%')
            )

    rows = (
        session
        .execute(
            query.order_by(RegistroGlosa.id.desc()).offset(offset).limit(limit)
        )
        .scalars()
        .all()
    )

    return {'glosas': rows}


@router.get(
    '/tiss',
    status_code=HTTPStatus.OK,
    response_model=TissList,
)
def consultar_tiss(
    usuario_atual: ValidaUsuarioAtual,
    session: SessionPostgres,
    q: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=600),
):
    query = select(Tiss)

    if q:
        termo = f'%{q.strip()}%'
        query = query.where(
            Tiss.codigo_termo.ilike(termo) | Tiss.termo.ilike(termo)
        )

    rows = (
        session
        .execute(query.order_by(Tiss.codigo_termo).limit(limit))
        .scalars()
        .all()
    )

    return {'itens': rows}


@router.post(
    '/glosas',
    status_code=HTTPStatus.CREATED,
    response_model=RegistroGlosaPublic,
)
def registrar_glosa(
    payload: RegistroGlosaCreate,
    usuario_atual: ValidaUsuarioAtual,
    session: SessionPostgres,
):
    registro_glosa = RegistroGlosa(**payload.model_dump(mode='json'))

    session.add(registro_glosa)
    session.commit()
    session.refresh(registro_glosa)

    return registro_glosa


@router.put(
    '/glosas/{glosa_id}',
    status_code=HTTPStatus.OK,
    response_model=RegistroGlosaPublic,
)
def editar_glosa(
    glosa_id: int,
    payload: RegistroGlosaCreate,
    usuario_atual: ValidaUsuarioAtual,
    session: SessionPostgres,
):
    registro_glosa = _get_registro_glosa_or_404(glosa_id, session)

    for field_name, value in payload.model_dump(mode='json').items():
        setattr(registro_glosa, field_name, value)

    session.commit()
    session.refresh(registro_glosa)

    return registro_glosa


@router.delete(
    '/glosas/{glosa_id}',
    status_code=HTTPStatus.OK,
    response_model=Message,
)
def deletar_glosa(
    glosa_id: int,
    usuario_atual: ValidaUsuarioAtual,
    session: SessionPostgres,
):
    registro_glosa = _get_registro_glosa_or_404(glosa_id, session)

    session.delete(registro_glosa)
    session.commit()

    return {'message': 'Registro de glosa excluido!'}
