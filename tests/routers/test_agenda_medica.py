from datetime import date, timedelta
from http import HTTPStatus

import pytest

from app_prontocardio.agenda_medica_models import (
    AgendaMedicaEvento,
    AgendaMedicaOferta,
)
from app_prontocardio.app import app
from app_prontocardio.models import Usuario
from app_prontocardio.permissions import TELAS_PADRAO
from app_prontocardio.routers.agenda_medica import get_soulmv_agenda_medica
from app_prontocardio.security import gera_hash_senha
from app_prontocardio.services.agenda_medica_soulmv import (
    dias_geracao,
    tp_dia_semana_de,
)

DATA_FUTURA = date.today() + timedelta(days=7)
CD_PRESTADOR = 245
CD_ESCALA = 757
CD_AGENDA = 55211
CD_SETOR_CLINICA = 56


class SoulMVFalso:
    """SoulMV em memória: nenhuma conexão com o banco real."""

    def __init__(self):
        self.conflitos_retorno = []
        self.marcados = 0

    def escala_modelo(self, cd_escala):
        if cd_escala != CD_ESCALA:
            return None
        return {
            'cd_escala_central': CD_ESCALA,
            'tp_dia_semana': tp_dia_semana_de(DATA_FUTURA),
            'hr_inicio': '08:00',
            'hr_fim': '12:00',
            'qt_atendimento': 8,
            'cd_prestador': 358,
            'nm_prestador': 'MODELO',
            'cd_unidade_atendimento': 1,
            'ds_unidade_atendimento': 'CLINICA PRONTOCARDIO SAUDE',
            'cd_recurso_central': None,
            'cd_setor': CD_SETOR_CLINICA,
            'nm_setor': 'RECEPCAO CLINICA',
            'cd_multi_empresa': 1,
        }

    def escalas_modelo(self, cd_unidade, tp_dia_semana):
        return [self.escala_modelo(CD_ESCALA)]

    @staticmethod
    def prestador(cd_prestador):
        return {'cd_prestador': cd_prestador, 'nm_prestador': 'DR TESTE'}

    @staticmethod
    def especialidades_prestador(cd_prestador):
        return ['CARDIOLOGIA']

    def conflitos(self, cd_prestador, data_agenda, inicio, fim):
        return self.conflitos_retorno

    def agenda(self, cd_agenda_central):
        if cd_agenda_central != CD_AGENDA:
            return None
        return {
            'cd_agenda_central': CD_AGENDA,
            'cd_prestador': CD_PRESTADOR,
            'dt_agenda': DATA_FUTURA,
            'hr_inicio': '13:00',
            'hr_fim': '17:00',
            'qt_atendimento': 18,
            'qt_marcados': self.marcados,
            'ds_unidade_atendimento': 'UNIDADE DIAGNOSTICO 2',
        }


@pytest.fixture
def soulmv():
    falso = SoulMVFalso()
    app.dependency_overrides[get_soulmv_agenda_medica] = lambda: falso
    yield falso
    app.dependency_overrides.pop(get_soulmv_agenda_medica, None)


def _usuario(session, nome, perfil, telas):
    usuario = Usuario(
        nome=nome,
        email=f'{nome}@teste.com',
        senha=gera_hash_senha('testes'),
        perfil=perfil,
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
def perfis(cliente, session, soulmv):
    admin = _usuario(
        session, 'admin_agenda', 'usuario', ['agenda_medica_admin']
    )
    resp = _usuario(
        session, 'resp_agenda', 'usuario', ['agenda_medica_responsavel']
    )
    medico = _usuario(
        session, 'medico_agenda', 'usuario', ['agenda_medica_medico']
    )
    comum = _usuario(session, 'comum_agenda', 'usuario', list(TELAS_PADRAO))
    tokens = {
        'admin': _token(cliente, admin),
        'resp': _token(cliente, resp),
        'medico': _token(cliente, medico),
        'comum': _token(cliente, comum),
    }
    cliente.post(
        '/agenda-medica/vinculos',
        json={'usuario_id': medico.id, 'cd_prestador': CD_PRESTADOR},
        headers=tokens['admin'],
    )
    return tokens


def _oferta(cliente, tokens):
    resposta = cliente.post(
        '/agenda-medica/ofertas',
        json={
            'cd_escala_modelo': CD_ESCALA,
            'data_agenda': DATA_FUTURA.isoformat(),
            'especialidade': 'cardiologia',
        },
        headers=tokens['admin'],
    )
    assert resposta.status_code == HTTPStatus.CREATED
    return resposta.json()['id']


def test_telas_da_agenda_medica_ficam_fora_do_padrao():
    assert not {
        'agenda_medica_medico',
        'agenda_medica_admin',
        'agenda_medica_responsavel',
    }.intersection(TELAS_PADRAO)


def test_me_informa_papeis_e_prestador(cliente, perfis):
    medico = cliente.get('/agenda-medica/me', headers=perfis['medico'])
    assert medico.status_code == HTTPStatus.OK
    assert medico.json()['papeis'] == ['medico']
    assert medico.json()['cd_prestador'] == CD_PRESTADOR
    admin = cliente.get('/agenda-medica/me', headers=perfis['admin']).json()
    assert admin['papeis'] == ['admin']
    assert admin['cd_prestador'] is None


def test_oferta_guarda_horario_e_unidade_da_escala(cliente, perfis):
    _oferta(cliente, perfis)
    ofertas = cliente.get(
        '/agenda-medica/ofertas', headers=perfis['medico']
    ).json()['ofertas']
    assert ofertas[0]['hr_inicio'] == '08:00'
    assert ofertas[0]['hr_fim'] == '12:00'
    assert ofertas[0]['ds_unidade_atendimento'] == 'CLINICA PRONTOCARDIO SAUDE'


def test_usuario_sem_tela_nao_acessa(cliente, perfis):
    resposta = cliente.get('/agenda-medica/status', headers=perfis['comum'])
    assert resposta.status_code == HTTPStatus.FORBIDDEN


def test_status_informa_que_soulmv_nao_e_gravado(cliente, perfis):
    resposta = cliente.get('/agenda-medica/status', headers=perfis['medico'])
    assert resposta.status_code == HTTPStatus.OK
    assert resposta.json()['gravacao_soulmv'] is False


def test_oferta_em_dia_diferente_da_escala_e_recusada(cliente, perfis):
    resposta = cliente.post(
        '/agenda-medica/ofertas',
        json={
            'cd_escala_modelo': CD_ESCALA,
            'data_agenda': (DATA_FUTURA + timedelta(days=1)).isoformat(),
            'especialidade': 'CARDIOLOGIA',
        },
        headers=perfis['admin'],
    )
    assert resposta.status_code == HTTPStatus.CONFLICT


def test_medico_nao_cria_oferta(cliente, perfis):
    resposta = cliente.post(
        '/agenda-medica/ofertas',
        json={
            'cd_escala_modelo': CD_ESCALA,
            'data_agenda': DATA_FUTURA.isoformat(),
            'especialidade': 'CARDIOLOGIA',
        },
        headers=perfis['medico'],
    )
    assert resposta.status_code == HTTPStatus.FORBIDDEN


def test_fluxo_interesse_aprovado_devolve_plano_sem_gravar(
    cliente, session, perfis
):
    oferta_id = _oferta(cliente, perfis)
    pedido = cliente.post(
        f'/agenda-medica/ofertas/{oferta_id}/interesse',
        headers=perfis['medico'],
    )
    assert pedido.status_code == HTTPStatus.CREATED
    repetido = cliente.post(
        f'/agenda-medica/ofertas/{oferta_id}/interesse',
        headers=perfis['medico'],
    )
    assert repetido.status_code == HTTPStatus.CONFLICT

    decisao = cliente.post(
        f'/agenda-medica/solicitacoes/{pedido.json()["id"]}/decisao',
        json={'acao': 'aprovar'},
        headers=perfis['admin'],
    )
    assert decisao.status_code == HTTPStatus.OK
    corpo = decisao.json()
    assert corpo['status'] == 'APROVADA'
    assert corpo['resultado_mv']['gravado_no_soulmv'] is False
    parametros = corpo['resultado_mv']['passos'][2]['parametros']
    assert parametros['P_CD_SETOR'] == CD_SETOR_CLINICA
    assert parametros['P_CD_PRESTADOR'] == CD_PRESTADOR
    assert parametros['P_CHK_DIAS'] == dias_geracao(
        tp_dia_semana_de(DATA_FUTURA)
    )
    assert session.get(AgendaMedicaOferta, oferta_id).status == 'PREENCHIDA'
    acoes = {e.acao for e in session.query(AgendaMedicaEvento).all()}
    assert acoes >= {
        'OFERTA_CRIADA',
        'INTERESSE_MANIFESTADO',
        'INTERESSE_APROVADA',
    }


def test_aprovacao_bloqueada_por_conflito_no_soulmv(cliente, perfis, soulmv):
    oferta_id = _oferta(cliente, perfis)
    pedido = cliente.post(
        f'/agenda-medica/ofertas/{oferta_id}/interesse',
        headers=perfis['medico'],
    ).json()
    soulmv.conflitos_retorno = [
        {'cd_agenda_central': 999, 'hr_inicio': '09:00', 'hr_fim': '10:00'}
    ]
    decisao = cliente.post(
        f'/agenda-medica/solicitacoes/{pedido["id"]}/decisao',
        json={'acao': 'aprovar'},
        headers=perfis['admin'],
    )
    assert decisao.status_code == HTTPStatus.CONFLICT
    assert 'Conflito' in decisao.json()['detail']['pendencias'][0]
    lista = cliente.get(
        '/agenda-medica/solicitacoes', headers=perfis['medico']
    ).json()
    assert lista['solicitacoes'][0]['status'] == 'PENDENTE'


def test_cancelamento_com_paciente_marcado_e_bloqueado(
    cliente, perfis, soulmv
):
    soulmv.marcados = 3
    pedido = cliente.post(
        '/agenda-medica/cancelamentos',
        json={'cd_agenda_central': CD_AGENDA, 'motivo': 'Congresso médico'},
        headers=perfis['medico'],
    ).json()
    decisao = cliente.post(
        f'/agenda-medica/solicitacoes/{pedido["id"]}/decisao',
        json={'acao': 'aprovar'},
        headers=perfis['resp'],
    )
    assert decisao.status_code == HTTPStatus.CONFLICT


def test_cancelamento_sem_paciente_exige_responsavel(cliente, perfis):
    pedido = cliente.post(
        '/agenda-medica/cancelamentos',
        json={'cd_agenda_central': CD_AGENDA, 'motivo': 'Congresso médico'},
        headers=perfis['medico'],
    ).json()
    pelo_admin = cliente.post(
        f'/agenda-medica/solicitacoes/{pedido["id"]}/decisao',
        json={'acao': 'aprovar'},
        headers=perfis['admin'],
    )
    assert pelo_admin.status_code == HTTPStatus.FORBIDDEN
    pelo_responsavel = cliente.post(
        f'/agenda-medica/solicitacoes/{pedido["id"]}/decisao',
        json={'acao': 'aprovar'},
        headers=perfis['resp'],
    )
    assert pelo_responsavel.status_code == HTTPStatus.OK
    resultado = pelo_responsavel.json()['resultado_mv']
    assert resultado['gravado_no_soulmv'] is False


def test_medico_nao_cancela_agenda_de_outro_prestador(cliente, perfis):
    resposta = cliente.post(
        '/agenda-medica/cancelamentos',
        json={'cd_agenda_central': 1, 'motivo': 'Congresso médico'},
        headers=perfis['medico'],
    )
    assert resposta.status_code == HTTPStatus.NOT_FOUND
