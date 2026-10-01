from http import HTTPStatus

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session

from app_prontocardio.models import AuditoriaAgendamento, Usuario


PERMISSAO_ULTIMO_ATENDIMENTO = "agendamento_ultimo_atendimento"


CONSULTA_ULTIMO_ATENDIMENTO_PROCEDIMENTO = text(
    """
    SELECT cd_atendimento,
           data_atendimento,
           cd_especialidade,
           especialidade,
           cd_prestador,
           medico,
           cd_it_agenda_central,
           cd_agenda_central
      FROM (
        SELECT a.CD_ATENDIMENTO AS cd_atendimento,
               a.HR_ATENDIMENTO AS data_atendimento,
               NVL(a.CD_ESPECIALID, i.CD_ESPECIALID)
                   AS cd_especialidade,
               e.DS_ESPECIALID AS especialidade,
               a.CD_PRESTADOR AS cd_prestador,
               p.NM_PRESTADOR AS medico,
               i.CD_IT_AGENDA_CENTRAL AS cd_it_agenda_central,
               i.CD_AGENDA_CENTRAL AS cd_agenda_central
          FROM DBAMV.IT_AGENDA_CENTRAL i
          JOIN DBAMV.ATENDIME a
            ON a.CD_ATENDIMENTO = i.CD_ATENDIMENTO
          JOIN DBAMV.PRESTADOR p
            ON p.CD_PRESTADOR = a.CD_PRESTADOR
          LEFT JOIN DBAMV.ESPECIALID e
            ON e.CD_ESPECIALID = NVL(a.CD_ESPECIALID, i.CD_ESPECIALID)
         WHERE i.CD_PACIENTE = :cd_paciente
           AND a.CD_PACIENTE = :cd_paciente
           AND i.CD_ITEM_AGENDAMENTO = :cd_item_agendamento
           AND i.CD_ATENDIMENTO IS NOT NULL
           AND a.TP_ATENDIMENTO = 'A'
           AND (a.DT_ALTA_MEDICA IS NOT NULL
                OR a.HR_ALTA_MEDICA IS NOT NULL)
           AND a.CD_PRESTADOR IS NOT NULL
         ORDER BY a.HR_ATENDIMENTO DESC,
                  a.CD_ATENDIMENTO DESC
      )
     WHERE ROWNUM = 1
    """
)


def exigir_permissao_ultimo_atendimento(usuario: object) -> Usuario:
    permissoes = {
        str(permissao).casefold()
        for permissao in (getattr(usuario, "telas_permitidas", None) or [])
    }
    if (
        not isinstance(usuario, Usuario)
        or not usuario.ativo
        or PERMISSAO_ULTIMO_ATENDIMENTO.casefold() not in permissoes
    ):
        raise HTTPException(
            status_code=HTTPStatus.FORBIDDEN,
            detail="Credencial sem permissão para consultar último atendimento.",
        )
    return usuario


def _auditar_consulta(
    *,
    postgres: Session,
    usuario: Usuario,
    cd_paciente: int,
    cd_item_agendamento: int,
    row: dict | None,
) -> None:
    postgres.add(
        AuditoriaAgendamento(
            operador_id=usuario.id,
            operador_nome=usuario.nome,
            origem="ULTIMO_ATENDIMENTO",
            cd_paciente=cd_paciente,
            cd_item_agendamento=cd_item_agendamento,
            cd_it_agenda_central=int(
                row.get("cd_it_agenda_central") or 0
            ) if row else 0,
            cd_agenda_central=int(
                row.get("cd_agenda_central") or 0
            ) if row else 0,
            cd_tip_mar=None,
            protocolo_mv=(
                int(row.get("cd_atendimento"))
                if row and row.get("cd_atendimento") is not None
                else None
            ),
            status=(
                "ultimo_atendimento_encontrado"
                if row
                else "ultimo_atendimento_ausente"
            ),
            chave_efeito_lote=None,
        )
    )
    postgres.commit()


def consultar_ultimo_atendimento_procedimento(
    *,
    oracle: Session,
    postgres: Session,
    usuario: Usuario,
    cd_paciente: int,
    cd_item_agendamento: int,
) -> dict:
    row = oracle.execute(
        CONSULTA_ULTIMO_ATENDIMENTO_PROCEDIMENTO,
        {
            "cd_paciente": cd_paciente,
            "cd_item_agendamento": cd_item_agendamento,
        },
    ).mappings().first()
    item = dict(row) if row is not None else None

    _auditar_consulta(
        postgres=postgres,
        usuario=usuario,
        cd_paciente=cd_paciente,
        cd_item_agendamento=cd_item_agendamento,
        row=item,
    )

    if item is None:
        return {"encontrado": False}
    return {
        "encontrado": True,
        "cd_atendimento": item.get("cd_atendimento"),
        "data_atendimento": item.get("data_atendimento"),
        "cd_especialidade": item.get("cd_especialidade"),
        "especialidade": item.get("especialidade"),
        "cd_prestador": item.get("cd_prestador"),
        "medico": item.get("medico"),
    }
