from datetime import date

from sqlalchemy import text
from sqlalchemy.orm import Session

PERIODO_MAXIMO_CANDIDATOS_DIAS = 31


SQL_ATENDIMENTO_AGENDAMENTO = text(
    '''
    SELECT iac.CD_IT_AGENDA_CENTRAL AS cd_it_agenda_central,
           iac.CD_AGENDA_CENTRAL AS cd_agenda_central,
           iac.CD_PACIENTE AS cd_paciente,
           iac.CD_ITEM_AGENDAMENTO AS cd_item_agendamento,
           iac.CD_ATENDIMENTO AS cd_atendimento,
           a.DT_ATENDIMENTO AS dh_atendimento
      FROM DBAMV.IT_AGENDA_CENTRAL iac
      JOIN DBAMV.ATENDIME a
        ON a.CD_ATENDIMENTO = iac.CD_ATENDIMENTO
     WHERE iac.CD_IT_AGENDA_CENTRAL = :cd_it_agenda_central
       AND iac.CD_ATENDIMENTO IS NOT NULL
    '''
)

SQL_CANDIDATOS_PROCEDIMENTO = text(
    '''
    SELECT DISTINCT
           a.CD_ATENDIMENTO AS cd_atendimento,
           a.CD_PACIENTE AS cd_paciente,
           a.DT_ATENDIMENTO AS dh_atendimento,
           irf.CD_PRO_FAT AS codigo_procedimento,
           pf.DS_PRO_FAT AS descricao_procedimento
      FROM DBAMV.ATENDIME a
      JOIN DBAMV.REG_FAT rf
        ON rf.CD_ATENDIMENTO = a.CD_ATENDIMENTO
      JOIN DBAMV.ITREG_FAT irf
        ON irf.CD_REG_FAT = rf.CD_REG_FAT
      LEFT JOIN DBAMV.PRO_FAT pf
        ON pf.CD_PRO_FAT = irf.CD_PRO_FAT
     WHERE a.CD_PACIENTE = :cd_paciente
       AND irf.CD_PRO_FAT = :codigo_procedimento
       AND a.DT_ATENDIMENTO >= :data_inicio
       AND a.DT_ATENDIMENTO < :data_fim + 1
     ORDER BY dh_atendimento, cd_atendimento
    '''
)


def consultar_atendimento_agendamento(
    session: Session,
    cd_it_agenda_central: int,
) -> dict[str, object] | None:
    row = session.execute(
        SQL_ATENDIMENTO_AGENDAMENTO,
        {'cd_it_agenda_central': cd_it_agenda_central},
    ).mappings().one_or_none()
    return dict(row) if row else None


def listar_candidatos_procedimento(
    session: Session,
    cd_paciente: int,
    codigo_procedimento: str,
    data_inicio: date,
    data_fim: date,
) -> list[dict[str, object]]:
    if (
        data_fim < data_inicio
        or (data_fim - data_inicio).days > PERIODO_MAXIMO_CANDIDATOS_DIAS
    ):
        raise ValueError('O período máximo para candidatos é de 31 dias.')

    rows = session.execute(
        SQL_CANDIDATOS_PROCEDIMENTO,
        {
            'cd_paciente': cd_paciente,
            'codigo_procedimento': codigo_procedimento,
            'data_inicio': data_inicio,
            'data_fim': data_fim,
        },
    ).mappings().all()
    return [dict(row) for row in rows]
