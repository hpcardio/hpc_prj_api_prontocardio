import asyncio
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import HTTPException, UploadFile, status

from app_prontocardio import whatsapp_service as service
from app_prontocardio.routers import agendamentos, whatsapp


@pytest.mark.parametrize(
    ('data', 'mime'),
    [
        (b'\x89PNG\r\n\x1a\nDATA', 'image/png'),
        (bytes.fromhex('ffd8ff') + b'DATA', 'image/jpeg'),
    ],
)
def test_media_preserva_cabecalho_corpo_e_destino(monkeypatch, data, mime):
    upload = Mock(return_value='media-test')
    send = Mock(return_value={'messages': [{'id': 'test-id'}]})
    monkeypatch.setattr(service, 'upload_whatsapp_media', upload)
    monkeypatch.setattr(service, 'post_graph_messages', send)
    result = service.enviar_midia_template_whatsapp(
        telefone='5585999999999',
        nome_template='preparo',
        idioma='pt_BR',
        imagem=data,
        parametros=['nome'],
    )
    assert result['id_externo'] == 'test-id'
    upload.assert_called_once_with(content=data, mime_type=mime)
    parts = send.call_args.args[0]['template']['components']
    assert parts[0]['parameters'][0]['image']['id'] == 'media-test'
    assert parts[1]['parameters'] == [{'type': 'text', 'text': 'nome'}]


def test_origem_pode_suprimir_confirmacao_sem_consulta():
    session = Mock()
    result = agendamentos._enviar_confirmacao_whatsapp_agendamento(
        session=session,
        payload=SimpleNamespace(enviar_confirmacao_whatsapp=False),
        protocolo=1,
    )
    assert result[0] == 'suprimido_origem'
    session.execute.assert_not_called()


@pytest.mark.parametrize(
    ('previous', 'expected'),
    [
        ('ENVIADO', 'ja_enviado'),
        ('PENDENTE', 409),
        ('INCERTO', 409),
        ('FALHOU', 'enviado'),
        (None, 'enviado'),
    ],
)
def test_idempotencia_midia(monkeypatch, previous, expected):
    monkeypatch.setattr(
        whatsapp.settings, 'WHATSAPP_MIDIA_TEMPLATE_ENABLED', True
    )
    payload = whatsapp.WhatsAppComprovanteInput(
        telefone='5585999999999',
        nome_template='preparo_angiotc_coronariana_v1',
        chave_idempotencia='test-key-123',
    )
    record = (
        SimpleNamespace(
            status=previous, id_externo='old-id', telefone_final='9999'
        )
        if previous
        else None
    )
    session = Mock()
    session.scalar.return_value = record
    send = Mock(
        return_value={
            'status': 'enviado',
            'id_externo': 'new-id',
            'telefone_final': '9999',
        }
    )
    monkeypatch.setattr(
        whatsapp, 'enviar_midia_template_whatsapp_service', send
    )
    file = UploadFile(
        filename='test.png', file=BytesIO(b'\x89PNG\r\n\x1a\nDATA')
    )
    call = whatsapp.enviar_midia_template_whatsapp(
        usuario_atual=object(), payload=payload, arquivo=file, session=session
    )
    if isinstance(expected, int):
        with pytest.raises(HTTPException) as exc:
            asyncio.run(call)
        assert exc.value.status_code == expected
    else:
        assert asyncio.run(call)['status'] == expected
    assert send.call_count == int(expected == 'enviado')


@pytest.mark.parametrize(
    ('status_code', 'stored'), [(422, 'FALHOU'), (502, 'INCERTO')]
)
def test_falha_midia_preserva_estado(monkeypatch, status_code, stored):
    monkeypatch.setattr(
        whatsapp.settings, 'WHATSAPP_MIDIA_TEMPLATE_ENABLED', True
    )
    record = SimpleNamespace(status='FALHOU')
    session = Mock()
    session.scalar.return_value = record
    monkeypatch.setattr(
        whatsapp,
        'enviar_midia_template_whatsapp_service',
        Mock(
            side_effect=HTTPException(status_code=status_code, detail='teste')
        ),
    )
    payload = whatsapp.WhatsAppComprovanteInput(
        telefone='5585999999999',
        nome_template='preparo_angiotc_coronariana_v1',
        chave_idempotencia='test-key-123',
    )
    with pytest.raises(HTTPException):
        asyncio.run(
            whatsapp.enviar_midia_template_whatsapp(
                usuario_atual=object(),
                payload=payload,
                arquivo=UploadFile(file=BytesIO(b'\x89PNG\r\n\x1a\nDATA')),
                session=session,
            )
        )
    assert record.status == stored


def test_formulario_parametros_invalidos():
    with pytest.raises(HTTPException) as exc:
        whatsapp.comprovante_form(
            telefone='5585999999999',
            nome_template='preparo',
            chave_idempotencia='test-key-123',
            parametros='{}',
        )
    assert exc.value.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


@pytest.mark.parametrize(
    ('value', 'expected'),
    [(1234.5, 'R$ 1.234,50'), (None, 'Consulte a equipe')],
)
def test_confirmacao_preserva_valor_exame(monkeypatch, value, expected):
    monkeypatch.setenv('WHATSAPP_CONFIRMAR_AGENDAMENTO_AUTO', 'true')
    send = Mock()
    monkeypatch.setattr(agendamentos, 'enviar_template_whatsapp', send)
    session = Mock()
    session.execute.return_value.mappings.return_value.first.return_value = {
        'nm_paciente': 'Teste'
    }
    payload = SimpleNamespace(
        enviar_confirmacao_whatsapp=True,
        valor_exame=value,
        cd_paciente=1,
        cd_item_agendamento=1,
        cd_it_agenda_central=1,
        nr_ddd_celular='85',
        nr_celular='999999999',
    )
    result = agendamentos._enviar_confirmacao_whatsapp_agendamento(
        session=session, payload=payload, protocolo=123
    )
    assert result[0] == 'enviado'
    assert send.call_args.kwargs['parametros'][-1] == expected


def test_sem_rotas_de_comprovante_duplicadas():
    matches = [
        r
        for r in whatsapp.router.routes
        if r.path == '/whatsapp/enviar-comprovante'
    ]
    assert len(matches) == 1
