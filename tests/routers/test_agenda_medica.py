from datetime import date, timedelta
from http import HTTPStatus

import pytest
from sqlalchemy import select

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
    BloqueioAgendaErro,
    GeracaoAgendaErro,
    dias_geracao,
    texto_evolucao,
    tp_dia_semana_de,
)

DATA_FUTURA = date.today() + timedelta(days=7)
CD_PRESTADOR = 245
CD_ESCALA = 757
CD_AGENDA = 55211
CD_SETOR_CLINICA = 56
DIAS_AGENDAS = 30


class SoulMVFalso:
    """SoulMV em memória: nenhuma conexão com o banco real."""

    def __init__(self):
        self.conflitos_retorno = []
        self.marcados = 0
        self.bloqueios = []

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
    def agendas_atuais(dias):
        assert dias == DIAS_AGENDAS
        return [
            {
                'cd_agenda_central': CD_AGENDA,
                'tp_agenda': 'A',
                'dt_agenda': DATA_FUTURA.isoformat(),
                'hr_inicio': f'{DATA_FUTURA.isoformat()}T08:00:00',
                'hr_fim': f'{DATA_FUTURA.isoformat()}T12:00:00',
                'qt_atendimento': 8,
                'qt_marcados': 3,
                'cd_prestador': CD_PRESTADOR,
                'nm_prestador': 'DR TESTE',
                'cd_unidade_atendimento': 1,
                'ds_unidade_atendimento': 'CLINICA PRONTOCARDIO SAUDE',
            }
        ]

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

    def bloquear_agenda_aprovada(self, cd_agenda, motivo, usuario_mv):
        self.bloqueios.append((cd_agenda, motivo, usuario_mv))
        return {
            'modo': 'gravado',
            'gravado_no_soulmv': True,
            'cd_agenda_central': cd_agenda,
            'horarios_bloqueados': 8,
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
    assert resposta.json()['cancelamento_soulmv'] is False


def test_agendas_sao_consultadas_no_momento_da_requisicao(cliente, perfis):
    resposta = cliente.get('/agenda-medica/agendas', headers=perfis['medico'])
    assert resposta.status_code == HTTPStatus.OK
    dados = resposta.json()
    assert dados['dias'] == DIAS_AGENDAS
    assert dados['summary'] == {
        'agendas': 1,
        'vagas_configuradas': 8,
        'horarios_marcados': 3,
        'vagas_livres': 5,
    }
    assert dados['agendas'][0]['cd_agenda_central'] == CD_AGENDA


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


def _pedir_e_aprovar(cliente, perfis):
    oferta_id = _oferta(cliente, perfis)
    pedido = cliente.post(
        f'/agenda-medica/ofertas/{oferta_id}/interesse',
        headers=perfis['medico'],
    ).json()
    decisao = cliente.post(
        f'/agenda-medica/solicitacoes/{pedido["id"]}/decisao',
        json={'acao': 'aprovar'},
        headers=perfis['admin'],
    )
    return oferta_id, decisao


def test_gravacao_ligada_gera_agenda_no_soulmv(
    cliente, session, perfis, soulmv, monkeypatch
):
    monkeypatch.setenv('AGENDA_MEDICA_MV_GRAVACAO_HABILITADA', 'true')
    chamadas = []

    def gerar(escala, cd_prestador, data_agenda, usuario_mv):
        chamadas.append((escala['cd_escala_central'], cd_prestador))
        return {'modo': 'gravado', 'gravado_no_soulmv': True}

    soulmv.gerar_agenda_aprovada = gerar
    status = cliente.get('/agenda-medica/status', headers=perfis['admin'])
    assert status.json()['gravacao_soulmv'] is True

    oferta_id, decisao = _pedir_e_aprovar(cliente, perfis)
    assert decisao.status_code == HTTPStatus.OK
    assert decisao.json()['resultado_mv']['gravado_no_soulmv'] is True
    assert chamadas == [(CD_ESCALA, CD_PRESTADOR)]
    assert session.get(AgendaMedicaOferta, oferta_id).status == 'PREENCHIDA'


def test_falha_na_gravacao_mantem_pedido_pendente(
    cliente, session, perfis, soulmv, monkeypatch
):
    monkeypatch.setenv('AGENDA_MEDICA_MV_GRAVACAO_HABILITADA', 'true')

    def falhar(*_):
        raise GeracaoAgendaErro('O SoulMV não gerou a agenda (retorno 3).')

    soulmv.gerar_agenda_aprovada = falhar
    oferta_id, decisao = _pedir_e_aprovar(cliente, perfis)
    assert decisao.status_code == HTTPStatus.CONFLICT
    assert 'retorno 3' in decisao.json()['detail']['pendencias'][0]
    assert session.get(AgendaMedicaOferta, oferta_id).status == 'ABERTA'
    lista = cliente.get(
        '/agenda-medica/solicitacoes', headers=perfis['medico']
    ).json()
    assert lista['solicitacoes'][0]['status'] == 'PENDENTE'


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


def test_cancelamento_habilitado_bloqueia_agenda_no_soulmv(
    cliente, perfis, soulmv, monkeypatch
):
    monkeypatch.setenv('AGENDA_MEDICA_MV_CANCELAMENTO_HABILITADO', 'true')
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
    assert decisao.status_code == HTTPStatus.OK
    assert decisao.json()['resultado_mv']['gravado_no_soulmv'] is True
    assert soulmv.bloqueios == [
        (CD_AGENDA, 'Congresso médico', 'DBAMV')
    ]


def test_falha_no_bloqueio_mantem_cancelamento_pendente(
    cliente, perfis, soulmv, monkeypatch
):
    monkeypatch.setenv('AGENDA_MEDICA_MV_CANCELAMENTO_HABILITADO', 'true')

    def falhar(*_):
        raise BloqueioAgendaErro('Bloqueio incompleto: 7 de 8 horários.')

    soulmv.bloquear_agenda_aprovada = falhar
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
    lista = cliente.get(
        '/agenda-medica/solicitacoes', headers=perfis['medico']
    ).json()
    assert lista['solicitacoes'][0]['status'] == 'PENDENTE'


def test_medico_nao_cancela_agenda_de_outro_prestador(cliente, perfis):
    resposta = cliente.post(
        '/agenda-medica/cancelamentos',
        json={'cd_agenda_central': 1, 'motivo': 'Congresso médico'},
        headers=perfis['medico'],
    )
    assert resposta.status_code == HTTPStatus.NOT_FOUND


# ---------------------------------------------------------------- triagem

CD_IT_TRIAGEM = 900001
CD_PACIENTE_TRIAGEM = 777
SENIOR_A = 255
SENIOR_B = 358


def _paciente_falso():
    return {
        'cd_it_agenda_central': CD_IT_TRIAGEM,
        'cd_agenda_central': CD_AGENDA,
        'hr_agenda': f'{DATA_FUTURA.isoformat()}T08:00:00',
        'cd_paciente': CD_PACIENTE_TRIAGEM,
        'nm_paciente': 'PACIENTE FICTICIO',
        'confirmado': 'N',
        'nm_convenio': 'CONVENIO TESTE',
        'ds_item_agendamento': 'CONSULTA',
        'ds_tip_mar': 'CONSULTA SUBSEQUENTE',
        'cd_prestador_agenda': CD_PRESTADOR,
        'nm_prestador_agenda': 'DR TESTE',
        'cd_setor': CD_SETOR_CLINICA,
        'nm_setor': 'RECEPCAO CLINICA',
        'turno': 'MANHA',
        'dh_ult_atend_amb': None,
        'cd_prestador_ult_amb': None,
        'nm_prestador_ult_amb': None,
        'dias_desde_ult_atend_amb': None,
    }


def _triagem_falsa(falso):
    falso.triagem_pacientes = lambda dias: [_paciente_falso()]
    falso.item_triagem = lambda cd_it, dias: (
        _paciente_falso() if cd_it == CD_IT_TRIAGEM else None
    )
    falso.paciente_na_triagem = lambda cd_paciente, dias: (
        cd_paciente == CD_PACIENTE_TRIAGEM
    )
    falso.evolucoes_paciente = lambda cd_paciente, quantidade=2: [
        {
            'data': '2026-09-30T10:15',
            'medico': 'DR TESTE',
            'atendimento': 'Ambulatorial',
            'setor': 'RECEPCAO CLINICA',
            'texto': 'Texto fictício.',
        }
    ]


@pytest.fixture
def seniores(cliente, session, soulmv, perfis):
    _triagem_falsa(soulmv)
    tokens = {}
    for nome, cd in (('senior_a', SENIOR_A), ('senior_b', SENIOR_B)):
        usuario = _usuario(session, nome, 'usuario', ['agenda_medica_medico'])
        resposta = cliente.post(
            '/agenda-medica/vinculos',
            json={
                'usuario_id': usuario.id,
                'cd_prestador': cd,
                'nivel': 'senior',
            },
            headers=perfis['admin'],
        )
        assert resposta.json()['nivel'] == 'senior'
        tokens[nome] = _token(cliente, usuario)
    return {**perfis, **tokens}


def _decidir(cliente, token, decisao, cd_it=CD_IT_TRIAGEM):
    return cliente.post(
        '/agenda-medica/triagem/decisoes',
        json={'cd_it_agenda_central': cd_it, 'decisao': decisao},
        headers=token,
    )


def test_texto_evolucao_converte_rtf_e_html():
    rtf = (
        r'{\rtf1\ansi{\fonttbl{\f0 Arial;}}{\colortbl;\red0\green0\blue0;}'
        r'\f0 Paciente est\'e1vel.\par Retorno em 30 dias.}'
    )
    assert texto_evolucao(rtf) == 'Paciente estável.\nRetorno em 30 dias.'
    assert (
        texto_evolucao('<p>Dor &amp; cansaço</p><p><b>ECG</b><br>retorno</p>')
        == 'Dor & cansaço\nECG\nretorno'
    )
    assert texto_evolucao(None) == ''


def test_me_informa_nivel_do_medico(cliente, seniores):
    me = cliente.get('/agenda-medica/me', headers=seniores['senior_a'])
    assert me.json()['nivel'] == 'senior'
    pleno = cliente.get('/agenda-medica/me', headers=seniores['medico'])
    assert pleno.json()['nivel'] == 'pleno'


def test_triagem_bloqueada_para_medico_pleno(cliente, seniores):
    medico = seniores['medico']
    assert (
        cliente.get('/agenda-medica/triagem', headers=medico).status_code
        == HTTPStatus.FORBIDDEN
    )
    assert _decidir(cliente, medico, 'senior').status_code == (
        HTTPStatus.FORBIDDEN
    )
    evolucoes = cliente.post(
        '/agenda-medica/triagem/evolucoes',
        json={'cd_paciente': CD_PACIENTE_TRIAGEM},
        headers=medico,
    )
    assert evolucoes.status_code == HTTPStatus.FORBIDDEN


def test_senior_decide_e_outro_senior_nao_sobrescreve(
    cliente, session, seniores
):
    lista = cliente.get('/agenda-medica/triagem', headers=seniores['senior_a'])
    assert lista.status_code == HTTPStatus.OK
    assert lista.json()['decisoes'] == []

    decisao = _decidir(cliente, seniores['senior_a'], 'senior')
    assert decisao.status_code == HTTPStatus.OK
    assert decisao.json()['cd_prestador_senior'] == SENIOR_A

    conflito = _decidir(cliente, seniores['senior_b'], 'demais')
    assert conflito.status_code == HTTPStatus.CONFLICT

    lista = cliente.get('/agenda-medica/triagem', headers=seniores['admin'])
    assert lista.json()['decisoes'][0]['decisao'] == 'senior'

    desfeita = _decidir(cliente, seniores['senior_a'], None)
    assert desfeita.json()['decisao'] is None
    assert _decidir(cliente, seniores['senior_b'], 'demais').status_code == (
        HTTPStatus.OK
    )

    acoes = [e.acao for e in session.query(AgendaMedicaEvento).all()]
    assert acoes.count('TRIAGEM_SENIOR') == 1
    assert acoes.count('TRIAGEM_DESFEITA') == 1
    assert acoes.count('TRIAGEM_DEMAIS') == 1


def test_triagem_recusa_agendamento_fora_da_lista(cliente, seniores):
    resposta = _decidir(cliente, seniores['senior_a'], 'senior', cd_it=1)
    assert resposta.status_code == HTTPStatus.NOT_FOUND


def test_admin_acompanha_mas_nao_decide(cliente, seniores):
    admin = seniores['admin']
    assert (
        cliente.get('/agenda-medica/triagem', headers=admin).status_code
        == HTTPStatus.OK
    )
    assert _decidir(cliente, admin, 'senior').status_code == (
        HTTPStatus.FORBIDDEN
    )


def test_evolucoes_exigem_senior_ou_permissao_explicita(
    cliente, session, seniores
):
    admin = seniores['admin']
    negada = cliente.post(
        '/agenda-medica/triagem/evolucoes',
        json={'cd_paciente': CD_PACIENTE_TRIAGEM},
        headers=admin,
    )
    assert negada.status_code == HTTPStatus.FORBIDDEN

    usuario = session.scalar(
        select(Usuario).where(Usuario.email == 'admin_agenda@teste.com')
    )
    usuario.telas_permitidas = [
        *usuario.telas_permitidas,
        'agenda_medica_evolucoes',
    ]
    session.commit()

    autorizada = cliente.post(
        '/agenda-medica/triagem/evolucoes',
        json={'cd_paciente': CD_PACIENTE_TRIAGEM},
        headers=admin,
    )
    assert autorizada.status_code == HTTPStatus.OK
    assert len(autorizada.json()['evolucoes']) == 1


def test_evolucoes_so_de_paciente_da_triagem_e_auditadas(
    cliente, session, seniores
):
    fora = cliente.post(
        '/agenda-medica/triagem/evolucoes',
        json={'cd_paciente': 1},
        headers=seniores['senior_a'],
    )
    assert fora.status_code == HTTPStatus.FORBIDDEN

    resposta = cliente.post(
        '/agenda-medica/triagem/evolucoes',
        json={'cd_paciente': CD_PACIENTE_TRIAGEM},
        headers=seniores['senior_a'],
    )
    assert resposta.status_code == HTTPStatus.OK
    assert len(resposta.json()['evolucoes']) == 1
    evento = (
        session
        .query(AgendaMedicaEvento)
        .filter_by(acao='EVOLUCAO_CONSULTADA')
        .one()
    )
    assert str(CD_PACIENTE_TRIAGEM) in evento.detalhe
