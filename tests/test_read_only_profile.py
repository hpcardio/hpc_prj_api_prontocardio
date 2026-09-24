from http import HTTPStatus

from sqlalchemy import func, select

from app_prontocardio import security as security_module
from app_prontocardio.models import RegistroGlosa
from app_prontocardio.security import gera_hash_senha
from tests.conftest import UserFactory


def _token_leitura(cliente, session) -> str:
    senha = 'senha-leitura-teste'
    usuario = UserFactory(
        perfil='leitura',
        senha=gera_hash_senha(senha),
    )
    session.add(usuario)
    session.commit()

    response = cliente.post(
        '/autenticacao/token',
        data={'username': usuario.email, 'password': senha},
    )

    assert response.status_code == HTTPStatus.OK
    return response.json()['access_token']


def _payload_glosa() -> dict:
    return {
        'codigo_paciente': 1,
        'cd_remessa': 1234,
        'cd_atendimento': 271445,
        'conta': 333709,
        'cd_prestador': 10,
        'cd_convenio': 20,
        'tp_atendimento': 'Ambulatório',
        'procedimento': 'CONSULTA EM CONSULTORIO',
        'convenio': 'CASSI',
        'guia': '123456',
        'prestador': 'JOSE MARTINS CORDEIRO',
        'data_atendimento': '2025-11-22T00:00:00',
        'valor': '103.45',
        'processo_controle_fatura_gab': 'controle',
        'processo_recurso': 'recurso',
        'data_glosa': '2026-06-10',
        'motivo_glosa': '1008 - ASSINATURA DIVERGENTE',
        'descricao_glosa': 'descricao da glosa',
        'qtd_registro': '2',
        'qtd_glosada': '1',
        'valor_glosado': '12.31',
        'dt_recurso': '2026-06-16',
        'dt_pagamento': '2026-06-11',
        'sn_glosado': 'true',
    }


def test_perfil_leitura_bloqueia_escrita_e_preserva_consulta(
    cliente,
    session,
    monkeypatch,
):
    token = _token_leitura(cliente, session)
    monkeypatch.setattr(
        security_module,
        'postgres_engine',
        session.get_bind(),
    )
    headers = {'Authorization': f'Bearer {token}'}

    escrita = cliente.post(
        '/app_glosas/glosas',
        headers=headers,
        json=_payload_glosa(),
    )

    assert escrita.status_code == HTTPStatus.FORBIDDEN
    assert escrita.json() == {
        'detail': 'Perfil de leitura não permite alterações.'
    }
    assert session.scalar(
        select(func.count()).select_from(RegistroGlosa)
    ) == 0

    leitura = cliente.get('/usuarios/me', headers=headers)

    assert leitura.status_code == HTTPStatus.OK
    assert leitura.json()['perfil'] == 'leitura'
