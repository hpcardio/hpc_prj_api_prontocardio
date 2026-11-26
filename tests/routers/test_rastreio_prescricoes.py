from datetime import date, datetime
from types import SimpleNamespace

from app_prontocardio.routers.agendamentos import _identificacao_agendamento
from app_prontocardio.routers.rastreio_prescricoes import data_elegivel


def test_prescricao_so_libera_depois_de_doze_dias():
    assert data_elegivel(datetime(2026, 11, 26, 15, 30)) == date(2026, 12, 8)


def test_agendamento_rastreado_recebe_marcador_explicito_no_mv():
    payload = SimpleNamespace(
        observacao=None,
        usuario_mv='YAGO',
        nome_operador='Yago Silva',
        rastreio_prescricao_id=42,
    )
    usuario = SimpleNamespace(nome='Yago', origem_agendamento='CALL_CENTER')

    _, _, observacao = _identificacao_agendamento(payload, usuario)

    assert 'ORIGEM: PRESCRICAO AMBULATORIAL' in observacao
    assert 'RASTREIO_ID: 42' in observacao
