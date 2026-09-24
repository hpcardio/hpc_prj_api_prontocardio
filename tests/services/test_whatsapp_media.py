import pytest
from fastapi import HTTPException, status

from app_prontocardio import whatsapp_service

PNG = b'\x89PNG\r\n\x1a\n' + b'x' * 32


def test_envia_template_com_cabecalho_de_imagem(monkeypatch):
    chamadas = []
    monkeypatch.setattr(
        whatsapp_service,
        'upload_whatsapp_media',
        lambda *, content, mime_type: 'media-123',
    )
    monkeypatch.setattr(
        whatsapp_service,
        'post_graph_messages',
        lambda payload: (
            chamadas.append(payload) or {'messages': [{'id': 'msg-1'}]}
        ),
    )

    result = whatsapp_service.enviar_comprovante_whatsapp(
        telefone='5585994367185',
        nome_template='confirmacao_agendamento_rede',
        idioma='pt_BR',
        png=PNG,
    )

    assert result['id_externo'] == 'msg-1'
    assert result['telefone_final'] == '7185'
    assert chamadas[0]['template']['components'][0]['parameters'][0] == {
        'type': 'image',
        'image': {'id': 'media-123'},
    }


def test_rejeita_arquivo_sem_assinatura_png():
    with pytest.raises(HTTPException) as error:
        whatsapp_service.enviar_comprovante_whatsapp(
            telefone='5585994367185',
            nome_template='confirmacao_agendamento_rede',
            idioma='pt_BR',
            png=b'not-png',
        )
    assert error.value.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
