from app_prontocardio.services.agenda_medica_soulmv import (
    CONSULTA_HORARIOS_AGENDA,
)


def test_horarios_distingue_cancelamento_solicitado_de_nao_confirmado():
    consulta = ' '.join(CONSULTA_HORARIOS_AGENDA.upper().split())

    regra_cancelamento = "WHEN UPPER(RC.DS_ASSUNTO) LIKE '%CANCEL%'"
    regra_nao_confirmado = "WHEN UPPER(RC.DS_ASSUNTO) LIKE '%NÃO CONFIRM%'"

    assert "THEN 'CANCELAMENTO_SOLICITADO'" in consulta
    assert consulta.index(regra_cancelamento) < consulta.index(
        regra_nao_confirmado
    )
