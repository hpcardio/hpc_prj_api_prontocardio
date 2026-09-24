def sincronizar_contatos_agendamento(
    cursor,
    *,
    cd_paciente: int,
    cd_it_agenda_inicio: int,
    cd_it_agenda_fim: int,
) -> None:
    """Copia os contatos cadastrais para a fotografia da reserva no MV."""
    cursor.execute(
        """
        MERGE INTO DBAMV.IT_AGENDA_CENTRAL i
        USING (
            SELECT CD_PACIENTE,
                   NR_DDI_FONE,
                   NR_DDD_FONE,
                   NR_FONE,
                   NR_DDI_CELULAR,
                   NR_DDD_CELULAR,
                   NR_CELULAR
              FROM DBAMV.PACIENTE
             WHERE CD_PACIENTE = :cd_paciente
        ) p
           ON (i.CD_PACIENTE = p.CD_PACIENTE
               AND i.CD_IT_AGENDA_CENTRAL BETWEEN :slot_inicio AND :slot_fim)
        WHEN MATCHED THEN UPDATE SET
             i.NR_DDI_TELEFONE = p.NR_DDI_FONE,
             i.NR_DDD_FONE = p.NR_DDD_FONE,
             i.NR_FONE = p.NR_FONE,
             i.NR_DDI_CELULAR = p.NR_DDI_CELULAR,
             i.NR_DDD_CELULAR = p.NR_DDD_CELULAR,
             i.NR_CELULAR = p.NR_CELULAR
        """,
        {
            'cd_paciente': cd_paciente,
            'slot_inicio': cd_it_agenda_inicio,
            'slot_fim': cd_it_agenda_fim,
        },
    )
