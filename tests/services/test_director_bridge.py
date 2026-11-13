import asyncio
import hashlib
import hmac
import json
from http import HTTPStatus

import pytest
from fastapi import HTTPException

from app_prontocardio import director_bridge as bridge

SENDERS = ('5585900000001', '5585900000002')
PHONE_ID = '111'
WABA_ID = '222'
SECRET = 'segredo-de-teste'


class RequestFalso:
    def __init__(self, corpo: bytes, assinatura: str = ''):
        self._corpo = corpo
        self.headers = {'x-hub-signature-256': assinatura}

    async def json(self):
        return json.loads(self._corpo)

    async def stream(self):
        yield self._corpo


def _assinar(corpo: bytes) -> str:
    digest = hmac.new(SECRET.encode(), corpo, hashlib.sha256).hexdigest()
    return f'sha256={digest}'


def _envelope(remetente, *, waba=WABA_ID, phone=PHONE_ID):
    return json.dumps({
        'object': 'whatsapp_business_account',
        'entry': [
            {
                'id': waba,
                'changes': [
                    {
                        'field': 'messages',
                        'value': {
                            'metadata': {'phone_number_id': phone},
                            'messages': [
                                {
                                    'from': remetente,
                                    'id': 'wamid.1',
                                    'type': 'text',
                                },
                            ],
                        },
                    }
                ],
            }
        ],
    }).encode()


@pytest.fixture
def ligado(monkeypatch):
    monkeypatch.setenv('DIRECTOR_BRIDGE_ENABLED', 'true')
    monkeypatch.setenv('DIRECTOR_BRIDGE_SENDER_IDS', ','.join(SENDERS))
    monkeypatch.setenv('DIRECTOR_BRIDGE_PHONE_NUMBER_ID', PHONE_ID)
    monkeypatch.setenv('DIRECTOR_BRIDGE_WABA_ID', WABA_ID)
    monkeypatch.setenv('DIRECTOR_BRIDGE_META_APP_ID', '333')
    monkeypatch.setenv('DIRECTOR_BRIDGE_META_APP_SECRET', SECRET)
    enviados = []

    async def _forward(raw, signature, receipts):
        enviados.append(receipts)

    monkeypatch.setattr(bridge, 'forward_original', _forward)
    return enviados


def _rotear(request):
    return asyncio.run(bridge.route_director_envelope(request))


def test_desligado_por_padrao_nao_seleciona_nada(monkeypatch):
    monkeypatch.delenv('DIRECTOR_BRIDGE_ENABLED', raising=False)
    payload, selecionadas = _rotear(RequestFalso(_envelope(SENDERS[0])))
    assert selecionadas == set()
    assert payload['object'] == 'whatsapp_business_account'


def test_remetente_comum_segue_para_o_fluxo_normal(ligado):
    _, selecionadas = _rotear(RequestFalso(_envelope('5585999999999')))
    assert selecionadas == set()
    assert ligado == []


def test_diretor_com_assinatura_valida_e_encaminhado(ligado):
    corpo = _envelope(SENDERS[0])
    _, selecionadas = _rotear(RequestFalso(corpo, _assinar(corpo)))
    assert selecionadas == {(0, 0, 0)}
    assert ligado == [{(PHONE_ID, 'wamid.1')}]


def test_diretor_com_assinatura_invalida_e_recusado(ligado):
    corpo = _envelope(SENDERS[0])
    with pytest.raises(HTTPException) as erro:
        _rotear(RequestFalso(corpo, 'sha256=' + '0' * 64))
    assert erro.value.status_code == HTTPStatus.FORBIDDEN
    assert ligado == []


def test_diretor_vindo_de_outro_numero_e_recusado(ligado):
    corpo = _envelope(SENDERS[0], phone='999')
    with pytest.raises(HTTPException) as erro:
        _rotear(RequestFalso(corpo, _assinar(corpo)))
    assert erro.value.status_code == HTTPStatus.FORBIDDEN


def test_configuracao_incompleta_fica_indisponivel(ligado, monkeypatch):
    monkeypatch.setenv('DIRECTOR_BRIDGE_SENDER_IDS', SENDERS[0])
    corpo = _envelope(SENDERS[0])
    with pytest.raises(HTTPException) as erro:
        _rotear(RequestFalso(corpo, _assinar(corpo)))
    assert erro.value.status_code == HTTPStatus.SERVICE_UNAVAILABLE


def test_ack_precisa_confirmar_exatamente_as_mensagens():
    esperado = {(PHONE_ID, 'wamid.1')}
    ack = {
        'received': True,
        'director': {
            'enabled': True,
            'liveReady': True,
            'receipts': [
                {'phoneNumberId': PHONE_ID, 'metaMessageId': 'wamid.1'}
            ],
        },
    }
    bridge.validate_director_ack(ack, esperado)
    ack['director']['receipts'] = []
    with pytest.raises(HTTPException):
        bridge.validate_director_ack(ack, esperado)
