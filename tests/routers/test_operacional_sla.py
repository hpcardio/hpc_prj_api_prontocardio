from datetime import datetime, timedelta
from types import SimpleNamespace

from app_prontocardio.routers.operacional import _sla_tratativa


def _item(minutes: int, status: str = 'PENDENTE') -> SimpleNamespace:
    now = datetime(2026, 11, 25, 12, 0)
    return SimpleNamespace(
        criado_em=now - timedelta(minutes=minutes),
        concluido_em=now if status == 'CONCLUIDA' else None,
        status=status,
    )


def test_sla_tratativa_classifica_faixas_operacionais():
    now = datetime(2026, 11, 25, 12, 0)

    assert _sla_tratativa(_item(30), now)['sla_status'] == 'NO_PRAZO'
    assert _sla_tratativa(_item(120), now)['sla_status'] == 'ATENCAO'
    assert _sla_tratativa(_item(240), now)['sla_status'] == 'ATRASADA'
    assert (
        _sla_tratativa(_item(300, 'CONCLUIDA'), now)['sla_status']
        == 'CONCLUIDA'
    )
