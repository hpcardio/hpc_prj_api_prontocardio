# ruff: noqa: PLR2004
"""Reconciliação da rede (recuperada para o Git em 24/11/2026).

Cobre só as proteções; as consultas ao MV não têm banco de teste.
"""

import pytest

from app_prontocardio.app import app
from app_prontocardio.reconciliacao_origem_mv import (
    ProtecaoFinanceiraOrigemMv,
    validar_protecao_financeira,
)
from app_prontocardio.routers import reconciliacao_rede as rota


def test_rotas_registradas():
    caminhos = {r.path for r in app.routes}
    assert '/reconciliacao-rede/atendimentos' in caminhos
    assert '/reconciliacao-rede/convenios' in caminhos
    assert (
        '/reconciliacao-rede/atendimentos/{atendimento}/origem/aplicar'
        in caminhos
    )


def test_gravacao_no_mv_desligada_por_padrao(monkeypatch):
    monkeypatch.delenv('RECONCILIACAO_ORIGEM_MV_WRITE_ENABLED', raising=False)
    assert rota._write_origem_habilitada() is False
    monkeypatch.setenv('RECONCILIACAO_ORIGEM_MV_WRITE_ENABLED', 'true')
    assert rota._write_origem_habilitada() is True


@pytest.mark.parametrize(
    ('fechada', 'remessa'),
    [(None, None), (True, None), (False, 123)],
)
def test_protecao_financeira_bloqueia_conta_fechada_ou_em_remessa(
    fechada, remessa
):
    with pytest.raises(ProtecaoFinanceiraOrigemMv):
        validar_protecao_financeira(
            conta_fechada=fechada, codigo_remessa=remessa
        )


def test_conta_aberta_sem_remessa_pode_ser_alterada():
    validar_protecao_financeira(conta_fechada=False, codigo_remessa=None)
