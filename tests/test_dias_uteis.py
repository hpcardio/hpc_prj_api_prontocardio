# ruff: noqa: PLR2004, E501
from datetime import date, datetime

import pytest

from app_prontocardio.services.dias_uteis import (
    dia_util,
    feriados,
    pascoa,
    prazo_minimo,
    vencimento_no_prazo,
)


def test_pascoa_e_feriados_moveis_de_2026_e_2027():
    assert pascoa(2026) == date(2026, 4, 5)
    assert pascoa(2027) == date(2027, 3, 28)
    f = feriados(2026)
    assert f[date(2026, 2, 16)] == f[date(2026, 2, 17)] == 'Carnaval'
    assert f[date(2026, 4, 3)] == 'Sexta-feira Santa'
    assert f[date(2026, 6, 4)] == 'Corpus Christi'


@pytest.mark.parametrize('dia', [date(2026, 3, 19), date(2026, 3, 25), date(2026, 8, 15), date(2026, 11, 20),
                                 date(2026, 11, 21), date(2026, 11, 22)])
def test_dias_nao_uteis(dia):
    assert not dia_util(dia)


def test_dia_util_comum():
    assert dia_util(date(2026, 11, 18))


@pytest.mark.parametrize(('envio', 'prazo'), [
    (datetime(2026, 11, 25, 10, 0), datetime(2026, 11, 30, 10, 0)),  # qua 10h -> seg 10h
    (datetime(2026, 11, 23, 9, 30), datetime(2026, 11, 26, 9, 30)),  # seg -> qui
    (datetime(2026, 11, 17, 10, 0), datetime(2026, 11, 23, 10, 0)),  # ter, pulando o feriado de 20/11
    (datetime(2026, 11, 28, 15, 0), datetime(2026, 12, 3, 0, 0)),    # sábado: conta a partir de seg 00h
    (datetime(2026, 12, 23, 18, 0), datetime(2026, 12, 29, 18, 0)),  # pula o Natal e o fim de semana
])
def test_prazo_minimo(envio, prazo):
    assert prazo_minimo(envio) == prazo


def test_dia_do_vencimento_conta_inteiro():
    envio = datetime(2026, 11, 25, 10, 0)  # prazo: seg 30/11 10h
    assert vencimento_no_prazo(date(2026, 11, 30), envio)
    assert not vencimento_no_prazo(date(2026, 11, 27), envio)
