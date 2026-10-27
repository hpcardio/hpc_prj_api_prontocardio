from datetime import datetime, timedelta
from http import HTTPStatus

import pytest

from app_prontocardio.agenda_medica_models import AgendaMedicaVinculo
from app_prontocardio.models import Usuario
from app_prontocardio.routers import pronto_escala
from app_prontocardio.security import gera_hash_senha

AGORA = datetime(2026, 10, 27, 8, 0)
CD_PRESTADOR = 245
ANTECEDENCIA_CHECKIN = 60
ATRASO_CHECKOUT = 120


def _usuario(session, nome, telas):
    usuario = Usuario(
        nome=nome,
        email=f'{nome}@teste.com',
        senha=gera_hash_senha('testes'),
        perfil='usuario',
        telas_permitidas=telas,
    )
    session.add(usuario)
    session.commit()
    session.refresh(usuario)
    return usuario


def _token(cliente, usuario):
    resposta = cliente.post(
        '/autenticacao/token',
        data={'username': usuario.email, 'password': 'testes'},
    )
    return {'Authorization': f'Bearer {resposta.json()["access_token"]}'}


@pytest.fixture
def acessos(cliente, session, monkeypatch):
    monkeypatch.setattr(pronto_escala, '_agora', lambda: AGORA)
    medico = _usuario(session, 'medico_escala', ['pronto_escala_medico'])
    validador = _usuario(
        session, 'validador_escala', ['pronto_escala_validador']
    )
    comum = _usuario(session, 'comum_escala', [])
    session.add(
        AgendaMedicaVinculo(
            usuario_id=medico.id,
            cd_prestador=CD_PRESTADOR,
            criado_por='teste',
        )
    )
    session.commit()
    return {
        'medico': _token(cliente, medico),
        'validador': _token(cliente, validador),
        'comum': _token(cliente, comum),
    }


def _plantao(cliente, acessos, inicio=AGORA, fim=None):
    fim = fim or inicio + timedelta(hours=6)
    resposta = cliente.post(
        '/pronto-escala/plantoes',
        headers=acessos['validador'],
        json={
            'cd_prestador': CD_PRESTADOR,
            'nm_prestador': 'DRA TESTE',
            'setor': 'UTI 1',
            'inicio': inicio.isoformat(),
            'fim': fim.isoformat(),
        },
    )
    assert resposta.status_code == HTTPStatus.CREATED
    return resposta.json()['id']


def test_acesso_e_parametros_sao_explicitos(cliente, acessos):
    negado = cliente.get('/pronto-escala/plantoes', headers=acessos['comum'])
    assert negado.status_code == HTTPStatus.FORBIDDEN
    parametros = cliente.get(
        '/pronto-escala/parametros', headers=acessos['validador']
    )
    assert parametros.status_code == HTTPStatus.OK
    assert (
        parametros.json()['antecedencia_checkin_min'] == ANTECEDENCIA_CHECKIN
    )
    assert parametros.json()['atraso_checkout_min'] == ATRASO_CHECKOUT


def test_checkin_respeita_janela_e_nao_duplica(cliente, acessos):
    plantao_id = _plantao(cliente, acessos)
    resposta = cliente.post(
        f'/pronto-escala/plantoes/{plantao_id}/check-in',
        headers=acessos['medico'],
    )
    assert resposta.status_code == HTTPStatus.OK
    repetido = cliente.post(
        f'/pronto-escala/plantoes/{plantao_id}/check-in',
        headers=acessos['medico'],
    )
    assert repetido.status_code == HTTPStatus.CONFLICT


def test_continuidade_no_mesmo_setor_dispensa_novo_checkin(cliente, acessos):
    primeiro = _plantao(
        cliente,
        acessos,
        inicio=AGORA,
        fim=AGORA + timedelta(hours=6),
    )
    cliente.post(
        f'/pronto-escala/plantoes/{primeiro}/check-in',
        headers=acessos['medico'],
    )
    segundo = _plantao(
        cliente,
        acessos,
        inicio=AGORA + timedelta(hours=6),
        fim=AGORA + timedelta(hours=12),
    )
    lista = cliente.get(
        '/pronto-escala/plantoes', headers=acessos['medico']
    ).json()['plantoes']
    assert next(item for item in lista if item['id'] == segundo)[
        'dispensa_checkin_continuidade'
    ]


def test_justificativa_mostra_contagem_e_so_pode_ser_decidida_uma_vez(
    cliente, acessos
):
    plantao_id = _plantao(cliente, acessos)
    envio = cliente.post(
        '/pronto-escala/justificativas',
        headers=acessos['medico'],
        json={
            'plantao_id': plantao_id,
            'tipo': 'CHECKIN',
            'texto': 'Falha no equipamento de registro.',
        },
    )
    assert envio.status_code == HTTPStatus.CREATED
    assert envio.json()['justificativas_no_mes'] == 1
    item_id = envio.json()['id']
    fila = cliente.get(
        '/pronto-escala/justificativas?status=PENDENTE',
        headers=acessos['validador'],
    )
    assert fila.status_code == HTTPStatus.OK
    assert fila.json()['justificativas'][0]['justificativas_no_mes'] == 1
    decisao = cliente.post(
        f'/pronto-escala/justificativas/{item_id}/decisao',
        headers=acessos['validador'],
        json={'decisao': 'DEFERIDA'},
    )
    assert decisao.status_code == HTTPStatus.OK
    repetida = cliente.post(
        f'/pronto-escala/justificativas/{item_id}/decisao',
        headers=acessos['validador'],
        json={'decisao': 'INDEFERIDA', 'motivo': 'Revisão tardia'},
    )
    assert repetida.status_code == HTTPStatus.CONFLICT
    eventos = cliente.get(
        '/pronto-escala/eventos', headers=acessos['validador']
    )
    assert eventos.status_code == HTTPStatus.OK
    assert any(
        evento['acao'] == 'JUSTIFICATIVA_DEFERIDA'
        for evento in eventos.json()['eventos']
    )


def test_justificativa_pode_ser_salva_e_enviada_depois(cliente, acessos):
    plantao_id = _plantao(cliente, acessos)
    rascunho = cliente.post(
        '/pronto-escala/justificativas',
        headers=acessos['medico'],
        json={
            'plantao_id': plantao_id,
            'tipo': 'CHECKOUT',
            'texto': 'Texto ainda em revisão pelo médico.',
            'modo': 'salvar',
        },
    )
    assert rascunho.status_code == HTTPStatus.CREATED
    assert rascunho.json()['status'] == 'RASCUNHO'
    assert rascunho.json()['justificativas_no_mes'] == 0
    envio = cliente.post(
        f'/pronto-escala/justificativas/{rascunho.json()["id"]}/enviar',
        headers=acessos['medico'],
    )
    assert envio.status_code == HTTPStatus.OK
    assert envio.json()['status'] == 'PENDENTE'
    assert envio.json()['justificativas_no_mes'] == 1
