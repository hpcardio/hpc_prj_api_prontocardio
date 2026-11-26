# ruff: noqa: PLR2004
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app_prontocardio.security import (
    criar_token_painel_medicos,
    valida_acesso_painel_leitos_medicos,
)


class FakeSession:
    def __init__(self, user):
        self.user = user

    def scalar(self, _statement):
        return self.user


def test_sessao_painel_persiste_e_e_revogada_ao_trocar_senha():
    user = SimpleNamespace(
        id=101,
        email='posto@hospitalprontocardio.com.br',
        senha='hash-original',
        ativo=True,
        perfil='leitura',
    )
    token = criar_token_painel_medicos(user)
    session = FakeSession(user)

    assert valida_acesso_painel_leitos_medicos(session, token) is user

    user.senha = 'hash-alterado'
    with pytest.raises(HTTPException) as error:
        valida_acesso_painel_leitos_medicos(session, token)
    assert error.value.status_code == 401
