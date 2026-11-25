# ruff: noqa: PLR2004, PLR0913, E501
from scripts import criar_usuarios_nfse_mv as script


def _linha(login, nome, oficina, cpf='529.982.247-25', email=None):
    return {'cd_usuario': login, 'nm_usuario': nome, 'cpf': cpf, 'ds_email': email,
            'cd_oficina': 1, 'ds_oficina': oficina}


def _existente(uid, nome, nome_usuario, cd_usuario_mv=None, telas=None, cpf=None, email=None):
    return {'id': uid, 'nome': nome, 'email': email or f'{nome_usuario}@x.com',
            'nome_usuario': nome_usuario, 'cpf': cpf, 'telas_permitidas': telas or [],
            'cd_usuario_mv': cd_usuario_mv}


def test_agrupa_oficinas_por_login_e_valida_cpf_e_email():
    usuarios = script.usuarios_do_mv([
        _linha('rh000938', 'ANA  SILVA', 'TI', email='ANA@HOSP.COM.BR'),
        _linha('RH000938', 'ANA SILVA', 'MANUTENCAO'),
        _linha('RH000111', 'BIA', 'TI', cpf='111.111.111-11', email='sem-email'),
    ])

    assert [(u.login, u.nome, u.oficinas) for u in usuarios] == [
        ('RH000938', 'ANA SILVA', ['MANUTENCAO', 'TI']),
        ('RH000111', 'BIA', ['TI']),
    ]
    assert usuarios[0].cpf == '52998224725'
    assert usuarios[0].email == 'ana@hosp.com.br'
    assert usuarios[1].cpf is None  # dígitos repetidos não é CPF
    assert usuarios[1].email is None


def test_cria_usuario_novo_com_login_mv_cpf_tela_e_vinculo():
    usuarios = script.usuarios_do_mv([_linha('RH000938', 'ANA SILVA', 'TI')])
    acao, = script.planejar(usuarios, [])

    assert acao.tipo == 'criar'
    assert acao.email == 'rh000938@sem-email.hospitalprontocardio.com.br'
    assert acao.cpf == '52998224725'
    assert acao.telas == ['contas_pagar_nfse_setor']
    assert acao.vincular is True


def test_nome_repetido_ganha_o_login_e_cpf_em_uso_nao_e_reusado():
    usuarios = script.usuarios_do_mv([_linha('RH000938', 'ANA SILVA', 'TI')])
    acao, = script.planejar(usuarios, [_existente(1, 'Ana Silva', 'ana', cpf='52998224725')])

    assert acao.tipo == 'criar'
    assert acao.nome == 'ANA SILVA (RH000938)'
    assert acao.cpf is None


def test_usuario_existente_ganha_tela_sem_mudar_senha():
    usuarios = script.usuarios_do_mv([_linha('RH000938', 'ANA SILVA', 'TI')])
    acao, = script.planejar(usuarios, [
        _existente(5, 'Ana', 'ana', cd_usuario_mv='RH000938', telas=['triagem']),
    ])

    assert acao.tipo == 'atualizar'
    assert acao.usuario_id == 5
    assert acao.telas == ['triagem', 'contas_pagar_nfse_setor']
    assert acao.vincular is False


def test_existente_ja_configurado_e_ignorado():
    usuarios = script.usuarios_do_mv([_linha('RH000938', 'ANA SILVA', 'TI')])
    acao, = script.planejar(usuarios, [
        _existente(5, 'Ana', 'rh000938', cd_usuario_mv='RH000938',
                   telas=['contas_pagar_nfse_setor'], cpf='52998224725'),
    ])

    assert acao.tipo == 'ignorar'


def test_nome_de_usuario_igual_mas_vinculado_a_outro_login_e_ignorado():
    usuarios = script.usuarios_do_mv([_linha('RH000938', 'ANA SILVA', 'TI')])
    acao, = script.planejar(usuarios, [
        _existente(5, 'Ana', 'rh000938', cd_usuario_mv='RH000999'),
    ])

    assert acao.tipo == 'ignorar'
    assert 'RH000999' in acao.motivo


def test_conta_de_sistema_do_mv_nao_vira_usuario():
    usuarios = script.usuarios_do_mv([_linha('DBAMV', 'DBAMV', 'TI'), _linha('RH000938', 'ANA', 'TI')])

    assert [u.login for u in usuarios] == ['RH000938']


def test_email_gerado_passa_na_validacao_da_listagem_de_usuarios():
    # A Gestão de acessos valida o e-mail de cada usuário (UserPublic/EmailStr).
    from app_prontocardio.schema import UserPublic  # noqa: PLC0415

    usuarios = script.usuarios_do_mv([_linha('RH000938', 'ANA SILVA', 'TI')])
    acao, = script.planejar(usuarios, [])
    UserPublic(id=1, nome=acao.nome, email=acao.email, nome_usuario='RH000938',
               perfil='usuario', ativo=True, telas_permitidas=acao.telas)
