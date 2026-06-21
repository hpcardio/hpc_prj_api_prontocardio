from copy import deepcopy
from http import HTTPStatus


def test_conta_atendimento_exige_criterio(cliente, token_teste):
    response = cliente.get(
        '/app_glosas/',
        headers={'Authorization': f'Bearer {token_teste}'},
    )

    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY


def registro_glosa_payload(**overrides):
    payload = {
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
        'processo_controle_fatura_gab': 'xptou xptou',
        'processo_recurso': 'ugkgkg',
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
    payload.update(overrides)
    return payload


def test_criar_glosa_ignora_sn_ativo_do_payload(cliente, token_teste):
    payload = registro_glosa_payload(sn_ativo='not')

    response = cliente.post(
        '/app_glosas/glosas',
        headers={'Authorization': f'Bearer {token_teste}'},
        json=payload,
    )

    assert response.status_code == HTTPStatus.CREATED
    assert response.json()['sn_ativo'] == 'true'

    response = cliente.get(
        '/app_glosas/glosas',
        headers={'Authorization': f'Bearer {token_teste}'},
        params={'cd_reg': payload['conta']},
    )

    assert response.status_code == HTTPStatus.OK
    assert len(response.json()['glosas']) == 1
    assert response.json()['glosas'][0]['sn_ativo'] == 'true'


def test_rejeita_glosa_sem_dados_obrigatorios(cliente, token_teste):
    payload = registro_glosa_payload(
        processo_controle_fatura_gab='',
        processo_recurso='',
        motivo_glosa='',
        dt_pagamento=None,
        dt_recurso=None,
        qtd_glosada=None,
        valor_glosado=None,
    )

    response = cliente.post(
        '/app_glosas/glosas',
        headers={'Authorization': f'Bearer {token_teste}'},
        json=payload,
    )

    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY


def test_rejeita_datas_quantidade_e_valor_invalidos(cliente, token_teste):
    casos = (
        {'data_glosa': '2026-06-12', 'dt_pagamento': '2026-06-11'},
        {'dt_recurso': '2026-06-09'},
        {'qtd_glosada': '3'},
        {'valor_glosado': '103.46'},
    )

    for override in casos:
        response = cliente.post(
            '/app_glosas/glosas',
            headers={'Authorization': f'Bearer {token_teste}'},
            json=registro_glosa_payload(**override),
        )
        assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY


def test_rejeita_recebimento_em_acato(cliente, token_teste):
    response = cliente.post(
        '/app_glosas/glosas',
        headers={'Authorization': f'Bearer {token_teste}'},
        json=registro_glosa_payload(sn_glosado='not'),
    )
    assert response.status_code == HTTPStatus.CREATED

    response = cliente.patch(
        f"/app_glosas/glosas/{response.json()['id']}/recebimento",
        headers={'Authorization': f'Bearer {token_teste}'},
        json={
            'dt_recebimento': '2026-06-20',
            'valor_recebido': '10.00',
        },
    )

    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY


def test_rejeita_valor_recebido_zero(cliente, token_teste):
    response = cliente.post(
        '/app_glosas/glosas',
        headers={'Authorization': f'Bearer {token_teste}'},
        json=registro_glosa_payload(),
    )
    assert response.status_code == HTTPStatus.CREATED

    response = cliente.patch(
        f"/app_glosas/glosas/{response.json()['id']}/recebimento",
        headers={'Authorization': f'Bearer {token_teste}'},
        json={
            'dt_recebimento': '2026-06-20',
            'valor_recebido': '0',
        },
    )

    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY


def test_rejeita_recebimento_maior_que_valor_recursado(cliente, token_teste):
    response = cliente.post(
        '/app_glosas/glosas',
        headers={'Authorization': f'Bearer {token_teste}'},
        json=registro_glosa_payload(valor_glosado='12.31'),
    )
    assert response.status_code == HTTPStatus.CREATED

    response = cliente.patch(
        f"/app_glosas/glosas/{response.json()['id']}/recebimento",
        headers={'Authorization': f'Bearer {token_teste}'},
        json={
            'dt_recebimento': '2026-06-20',
            'valor_recebido': '12.32',
        },
    )

    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert 'valor recursado' in response.json()['detail'].lower()


def test_rejeita_acato_criado_com_recebimento(cliente, token_teste):
    response = cliente.post(
        '/app_glosas/glosas',
        headers={'Authorization': f'Bearer {token_teste}'},
        json=registro_glosa_payload(
            sn_glosado='not',
            dt_recebimento='2026-06-20',
            valor_recebido='10.00',
        ),
    )

    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY

def test_atualizar_glosa_reativa_estado_sujo_do_payload(cliente, token_teste):
    payload = registro_glosa_payload()
    response = cliente.post(
        '/app_glosas/glosas',
        headers={'Authorization': f'Bearer {token_teste}'},
        json=payload,
    )
    glosa_id = response.json()['id']

    update_payload = deepcopy(payload)
    update_payload['descricao_glosa'] = 'descricao atualizada'
    update_payload['sn_ativo'] = 'not'
    response = cliente.put(
        f'/app_glosas/glosas/{glosa_id}',
        headers={'Authorization': f'Bearer {token_teste}'},
        json=update_payload,
    )

    assert response.status_code == HTTPStatus.OK
    assert response.json()['descricao_glosa'] == 'descricao atualizada'
    assert response.json()['sn_ativo'] == 'true'
