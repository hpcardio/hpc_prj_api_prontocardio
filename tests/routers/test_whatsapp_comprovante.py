from fastapi import status

from app_prontocardio.routers import whatsapp

PNG = b'\x89PNG\r\n\x1a\n' + b'x' * 32


def request_args(token):
    return {
        'files': {'arquivo': ('comprovante.png', PNG, 'image/png')},
        'data': {
            'telefone': '5585994367185',
            'nome_template': 'confirmacao_agendamento_rede',
            'idioma': 'pt_BR',
            'chave_idempotencia': 'rede:sol-1:item-1:v1',
        },
        'headers': {'Authorization': f'Bearer {token}'},
    }


def test_endpoint_exige_flag_homologada(cliente, token_teste):
    response = cliente.post(
        '/whatsapp/enviar-comprovante', **request_args(token_teste)
    )
    assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE, (
        response.json()
    )
    assert 'não configurado' in response.json()['detail']


def test_endpoint_nao_repete_chave_ja_enviada(
    cliente, token_teste, monkeypatch
):
    calls = []
    monkeypatch.setattr(
        whatsapp.settings, 'WHATSAPP_COMPROVANTE_ENABLED', True
    )
    monkeypatch.setattr(
        whatsapp,
        'enviar_comprovante_whatsapp_service',
        lambda **kwargs: (
            calls.append(kwargs)
            or {
                'status': 'enviado',
                'id_externo': 'msg-1',
                'telefone_final': '7185',
            }
        ),
    )
    first = cliente.post(
        '/whatsapp/enviar-comprovante', **request_args(token_teste)
    )
    second = cliente.post(
        '/whatsapp/enviar-comprovante', **request_args(token_teste)
    )
    assert first.json()['status'] == 'enviado'
    assert second.json()['status'] == 'ja_enviado'
    assert len(calls) == 1
