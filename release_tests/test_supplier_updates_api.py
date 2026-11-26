import json
import os
import unittest
from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault('ORACLE_DATABASE_URL', 'oracle+oracledb://test:test@127.0.0.1:1521/?service_name=test')
os.environ.setdefault('ORACLE_THICK_MODE', 'false')
os.environ.setdefault('POSTGRES_SCHEMA', 'api_prontocardio')
os.environ.setdefault('SECRET_KEY', 'isolated-tests-only-not-a-production-key')
os.environ.setdefault('ALGORITHM', 'HS256')
os.environ.setdefault('RUN_MIGRATIONS_ON_STARTUP', 'false')

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, update, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app_prontocardio.routers import supplier_updates as api
from app_prontocardio.services.supplier_updates_store import metadata, invites, events

BASE = '/app_glosas/financeiro/contas-a-pagar/fornecedores-atualizacao'
PUBLIC = BASE + '/publico'


class APITests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool,
                                    execution_options={'schema_translate_map': {'api_prontocardio': None}})
        metadata.create_all(self.engine)
        self.session = Session(self.engine)
        self.addCleanup(self.session.close)
        app = FastAPI()
        app.include_router(api.router)
        self.user = SimpleNamespace(id=7, perfil='ti', telas_permitidas=[])
        app.dependency_overrides[api.valida_token_usuario_atual] = lambda: self.user
        app.dependency_overrides[api.get_session_postgres] = lambda: self.session
        app.dependency_overrides[api.get_session_oracle] = lambda: None
        self.client = TestClient(app)
        self.position = dict(company=1, company_name='Hospital', supplier='12', name='Fornecedor', document='12345678000195',
            system_balance='100.00', paid_period='0.00', last_payment=None, titles=[], fingerprint='current')
        self.reader = patch.object(api, 'read_position', return_value=[self.position])
        self.reader.start()
        self.addCleanup(self.reader.stop)

    def case_and_invite(self):
        result = self.client.post(BASE, json={'company': 1, 'supplier': '12'})
        self.assertEqual(result.status_code, 200, result.text)
        case_id = result.json()['id']
        invitation = self.client.post(f'{BASE}/{case_id}/convites', json={}).json()
        return case_id, invitation['token']

    def response_payload(self):
        return dict(legal_name='Fornecedor', document='12345678000195', contact='Ana', email='ana@example.com',
                    phone='85999999999', as_of=str(date.today()), no_debt=True, confirmed=True)

    def test_denies_unauthorized_staff(self):
        self.user = SimpleNamespace(id=8, perfil='usuario', telas_permitidas=[])
        self.assertEqual(self.client.get(BASE).status_code, 403)

    def test_communication_list_reads_saved_sends_without_querying_mv(self):
        case_id, _ = self.case_and_invite()
        self.client.post(f'{BASE}/{case_id}/comunicacoes', json={'channel':'whatsapp','result':'enviado_confirmado','destination':'85999999999'})
        self.reader.stop()
        with patch.object(api, 'read_position', side_effect=AssertionError('MV must not be queried for message history')):
            result = self.client.get(BASE+'/comunicacoes')
        self.reader.start()
        self.assertEqual(result.status_code,200,result.text)
        self.assertEqual(result.json()['items'][0]['sent_count'],1)
        self.assertNotIn('system_balance',result.json()['items'][0])

    def test_prepared_messages_do_not_appear_as_sent(self):
        case_id, _ = self.case_and_invite()
        self.client.post(f'{BASE}/{case_id}/comunicacoes', json={'channel':'whatsapp','result':'preparado','destination':'85999999999'})
        result=self.client.get(BASE+'/comunicacoes')
        self.assertEqual(result.status_code,200,result.text)
        self.assertEqual(result.json()['total'],0)

    def test_failure_filter_shows_latest_failed_attempt_after_previous_success(self):
        id,_=self.case_and_invite()
        for result,destination in [('enviado_confirmado','primeiro@example.com'),('falhou','segundo@example.com')]:
            self.client.post(f'{BASE}/{id}/comunicacoes',json={'channel':'email','result':result,'destination':destination})
        row=self.client.get(BASE+'/comunicacoes?scope=falhas').json()['items'][0]
        self.assertEqual(row['last_result'],'email_falhou')
        self.assertEqual(row['destination'],'segundo@example.com')

    def test_public_invite_does_not_expose_internal_balance(self):
        case_id, token = self.case_and_invite()
        response = self.client.post(PUBLIC + '/consultar', json={'token': token})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn('system_balance', response.json())
        self.assertNotIn('titles', response.json())
        self.assertEqual(response.json()['name'], 'Fornecedor')

    def test_expired_invite_returns_gone(self):
        _, token = self.case_and_invite()
        self.session.execute(update(invites).values(expires_at=api.now() - timedelta(days=1)))
        self.session.commit()
        self.assertEqual(self.client.post(PUBLIC + '/consultar', json={'token': token}).status_code, 410)

    def test_submission_once_and_requires_financial_review(self):
        case_id, token = self.case_and_invite()
        data = {'token': token, 'payload': json.dumps(self.response_payload())}
        result = self.client.post(PUBLIC + '/respostas', data=data, headers={'X-Supplier-Invite': token})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()['status'], 'aguardando_validacao')
        self.assertEqual(self.client.post(PUBLIC + '/respostas', data=data, headers={'X-Supplier-Invite': token}).status_code, 410)
        detail = self.client.get(f'{BASE}/{case_id}').json()
        self.assertEqual(detail['case']['stage'], 'respondido')
        self.assertIsNone(detail['case']['validated_balance'])
        self.assertEqual(detail.get('active_invite_ids'), [])
        self.assertEqual(detail['comparison'], 'divergencia')
        review = dict(decision='aprovar', balance='0', reason='Conferidos todos os documentos e pagamentos.',
                      version=detail['case']['version'], fingerprint='old', confirm=True)
        self.assertEqual(self.client.post(f'{BASE}/{case_id}/revisoes', json=review).status_code, 409)
        review['fingerprint'] = 'current'
        self.assertEqual(self.client.post(f'{BASE}/{case_id}/revisoes', json=review).status_code, 200)

    def test_invalid_attachment_is_not_accepted(self):
        _, token = self.case_and_invite()
        result = self.client.post(PUBLIC + '/respostas', data={'token': token, 'payload': json.dumps(self.response_payload())},
                                  files={'files': ('documento.pdf', b'executable', 'application/pdf')}, headers={'X-Supplier-Invite': token})
        self.assertEqual(result.status_code, 422)

    def test_invalid_invitation_rejected_before_consuming_stream(self):
        consumed = []
        def body():
            consumed.append(True)
            yield b'not-a-valid-upload'
        result = self.client.post(PUBLIC + '/respostas', content=body(),
            headers={'Content-Type': 'multipart/form-data; boundary=test', 'X-Supplier-Invite': 'invalid'})
        self.assertEqual(result.status_code, 410)
        self.assertEqual(consumed, [])

    def test_chunked_upload_is_capped_before_full_parse(self):
        _, token = self.case_and_invite()
        boundary = 'stream-test'
        def body():
            yield (f'--{boundary}\r\nContent-Disposition: form-data; name="token"\r\n\r\n{token}\r\n'
                   f'--{boundary}\r\nContent-Disposition: form-data; name="payload"\r\n\r\n{json.dumps(self.response_payload())}\r\n'
                   f'--{boundary}\r\nContent-Disposition: form-data; name="files"; filename="large.pdf"\r\nContent-Type: application/pdf\r\n\r\n').encode()
            for _ in range(28):
                yield b'x' * 1024 * 1024
            yield f'\r\n--{boundary}--\r\n'.encode()
        result = self.client.post(PUBLIC + '/respostas', content=body(), headers={
            'Content-Type': f'multipart/form-data; boundary={boundary}', 'X-Supplier-Invite': token})
        self.assertEqual(result.status_code, 413)

    def test_preparing_whatsapp_does_not_mark_communicated(self):
        case_id, token = self.case_and_invite()
        response = self.client.post(f'{BASE}/{case_id}/comunicacoes', json={'channel': 'whatsapp', 'result': 'preparado', 'destination': '85999999999'})
        self.assertEqual(response.status_code, 200, response.text)
        detail = self.client.get(f'{BASE}/{case_id}').json()
        self.assertEqual(detail['case']['stage'], 'a_comunicar')

    def test_operator_cannot_approve_without_financial_permission(self):
        case_id, _ = self.case_and_invite()
        self.user = SimpleNamespace(id=8, perfil='usuario', telas_permitidas=['contas_pagar_gestao'])
        response = self.client.post(f'{BASE}/{case_id}/revisoes', json=dict(decision='aprovar', balance='1',
            reason='Conferido com documentos suficientes.', version=1, fingerprint='current', confirm=True))
        self.assertEqual(response.status_code, 403)

    def test_email_error_is_audited_and_not_sent(self):
        case_id, token = self.case_and_invite()
        with patch.object(api, 'send_invitation_email', side_effect=RuntimeError('SMTP indisponível')):
            response = self.client.post(f'{BASE}/{case_id}/email', json={'token': token, 'destination': 'ana@example.com'})
        self.assertEqual(response.status_code, 503)
        detail = self.client.get(f'{BASE}/{case_id}').json()
        self.assertEqual(detail['case']['stage'], 'a_comunicar')
        self.assertTrue(any(e['kind'] == 'email_falhou' for e in detail['events']))

    def test_approval_requires_explicit_balance(self):
        case_id, _ = self.case_and_invite()
        result = self.client.post(f'{BASE}/{case_id}/revisoes', json=dict(decision='aprovar',
            reason='Conferência de todos os documentos.', version=1, fingerprint='current', confirm=True))
        self.assertEqual(result.status_code, 422)


if __name__ == '__main__':
    unittest.main()
