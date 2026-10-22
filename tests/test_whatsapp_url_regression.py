from unittest.mock import Mock

import pytest

from app_prontocardio import whatsapp_service
from app_prontocardio.routers import whatsapp


@pytest.mark.parametrize('body', [[], ['codigo-teste']])
@pytest.mark.parametrize('button', [None, 'codigo-teste'])
def test_template_preserva_corpo_e_botao_opcional(monkeypatch, body, button):
    transport = Mock(return_value={'messages': []})
    monkeypatch.setattr(whatsapp_service, 'post_graph_messages', transport)
    whatsapp_service.enviar_template_whatsapp(
        telefone='5585999999999',
        nome_template='teste',
        parametros=body,
        parametro_botao_url=button,
    )
    template = transport.call_args.args[0]['template']
    components = template.get('components', [])
    assert len(components) == bool(body) + bool(button)
    if body:
        assert components[0] == {
            'type': 'body',
            'parameters': [{'type': 'text', 'text': body[0]}],
        }
    if button:
        assert components[-1] == {
            'type': 'button',
            'sub_type': 'url',
            'index': '0',
            'parameters': [{'type': 'text', 'text': button}],
        }
    if not body and not button:
        assert 'components' not in template


def test_endpoint_encaminha_botao(monkeypatch):
    service = Mock(return_value={})
    monkeypatch.setattr(whatsapp, 'enviar_template_whatsapp_service', service)
    payload = whatsapp.WhatsAppTemplateInput(
        telefone='5585999999999',
        nome_template='teste',
        parametro_botao_url='codigo-teste',
    )
    whatsapp.enviar_template_whatsapp(payload, usuario_atual=object())
    assert service.call_args.kwargs['parametro_botao_url'] == 'codigo-teste'
