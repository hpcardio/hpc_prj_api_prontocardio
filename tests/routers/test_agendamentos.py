from datetime import datetime

from app_prontocardio.routers.agendamentos import (
    _deve_liberar_bloqueio_residual,
)


def _agendamento_anterior(
    *,
    bloqueado='S',
    data_agendamento=None,
    data_bloqueio=None,
):
    return (
        10,
        20,
        30,
        40,
        bloqueado,
        data_agendamento,
        data_bloqueio,
    )


def test_libera_apenas_bloqueio_anterior_ao_agendamento():
    assert _deve_liberar_bloqueio_residual(
        _agendamento_anterior(
            data_agendamento=datetime(2026, 10, 8, 9),
            data_bloqueio=datetime(2026, 10, 8, 8),
        )
    )


def test_preserva_bloqueio_manual_posterior_ao_agendamento():
    assert not _deve_liberar_bloqueio_residual(
        _agendamento_anterior(
            data_agendamento=datetime(2026, 10, 8, 8),
            data_bloqueio=datetime(2026, 10, 8, 9),
        )
    )


def test_nao_libera_slot_sem_bloqueio_ativo():
    assert not _deve_liberar_bloqueio_residual(
        _agendamento_anterior(
            bloqueado='N',
            data_agendamento=datetime(2026, 10, 8, 9),
            data_bloqueio=datetime(2026, 10, 8, 8),
        )
    )
