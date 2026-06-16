from copy import deepcopy
from http import HTTPStatus


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
