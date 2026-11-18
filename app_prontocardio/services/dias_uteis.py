# ruff: noqa: E501
"""Dias úteis do financeiro (Fortaleza) e prazo mínimo de vencimento dos títulos.

Regra do financeiro: o título só é aceito se vencer pelo menos 72 horas
depois do envio, contando apenas dias úteis (3 dias úteis). Não são úteis os
sábados, domingos e feriados nacionais, do Ceará e de Fortaleza.
"""

from datetime import date, datetime, time, timedelta
from functools import cache

HORAS_DE_ANTECEDENCIA = 72

# (mês, dia): nome
FERIADOS_FIXOS = {
    (1, 1): 'Confraternização Universal',
    (3, 19): 'São José (CE)',
    (3, 25): 'Data Magna do Ceará',
    (4, 21): 'Tiradentes',
    (5, 1): 'Dia do Trabalho',
    (8, 15): 'Nossa Senhora da Assunção (Fortaleza)',
    (9, 7): 'Independência',
    (10, 12): 'Nossa Senhora Aparecida',
    (11, 2): 'Finados',
    (11, 15): 'Proclamação da República',
    (11, 20): 'Consciência Negra',
    (12, 25): 'Natal',
}


def pascoa(ano: int) -> date:
    """Domingo de Páscoa (algoritmo de Meeus/Jones/Butcher)."""
    a, b, c = ano % 19, ano // 100, ano % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7  # noqa: E741
    m = (a + 11 * h + 22 * l) // 451
    mes = (h + l - 7 * m + 114) // 31
    dia = ((h + l - 7 * m + 114) % 31) + 1
    return date(ano, mes, dia)


@cache
def feriados(ano: int) -> dict[date, str]:
    dias = {date(ano, m, d): nome for (m, d), nome in FERIADOS_FIXOS.items()}
    p = pascoa(ano)
    dias[p - timedelta(days=48)] = 'Carnaval'
    dias[p - timedelta(days=47)] = 'Carnaval'
    dias[p - timedelta(days=2)] = 'Sexta-feira Santa'
    dias[p + timedelta(days=60)] = 'Corpus Christi'
    return dias


def dia_util(dia: date) -> bool:
    return dia.weekday() < 5 and dia not in feriados(dia.year)  # noqa: PLR2004


def prazo_minimo(envio: datetime, horas: int = HORAS_DE_ANTECEDENCIA) -> datetime:
    """Momento em que se completam `horas` contadas só em dias úteis desde o envio."""
    restante = timedelta(hours=horas)
    atual = envio.replace(tzinfo=None)
    while True:
        meia_noite = datetime.combine(atual.date() + timedelta(days=1), time())
        if dia_util(atual.date()):
            disponivel = meia_noite - atual
            if disponivel >= restante:
                return atual + restante
            restante -= disponivel
        atual = meia_noite


def vencimento_no_prazo(vencimento: date, envio: datetime) -> bool:
    """O dia do vencimento conta inteiro: basta vencer no dia do prazo mínimo ou depois."""
    return vencimento >= prazo_minimo(envio).date()
