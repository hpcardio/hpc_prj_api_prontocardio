import os
import unittest
from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import patch
import json

os.environ.setdefault('ORACLE_DATABASE_URL','oracle+oracledb://test:test@127.0.0.1:1521/?service_name=test')
os.environ.setdefault('ORACLE_THICK_MODE','false')
os.environ.setdefault('POSTGRES_SCHEMA','api_prontocardio')
os.environ.setdefault('SECRET_KEY','isolated-payment-tests-only')
os.environ.setdefault('ALGORITHM','HS256')
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, update
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool
from app_prontocardio.routers import supplier_payment_notices as api
from app_prontocardio.services.supplier_payment_notices import payment_items, select_payments, notice_pdf
from app_prontocardio.services.supplier_payment_notices import notice_message
from app_prontocardio.services.supplier_payment_store import metadata, payment_links

BASE='/app_glosas/financeiro/contas-a-pagar/fornecedores-atualizacao/pagamentos'
PUBLIC='/app_glosas/financeiro/contas-a-pagar/fornecedores-atualizacao/publico/pagamentos'


class PaymentTests(unittest.TestCase):
    def setUp(self):
        self.engine=create_engine('sqlite://',connect_args={'check_same_thread':False},poolclass=StaticPool,
            execution_options={'schema_translate_map':{'api_prontocardio':None}})
        metadata.create_all(self.engine)
        self.session=Session(self.engine)
        self.addCleanup(self.engine.dispose)
        self.addCleanup(self.session.close)
        self.position=dict(company=1,company_name='Hospital',supplier='12',name='Fornecedor',document='12345678000195',
            titles=[dict(parcel='9',reference='NF1/1',invoice='NF1',due='2026-11-01',balance='0.00',
                payments=[dict(id='42',date='2026-11-20',amount='100.00',source='MV')])])
        self.reader=patch.object(api,'read_position',return_value=[self.position])
        self.reader.start()
        self.addCleanup(self.reader.stop)
        self.user=SimpleNamespace(id=7,perfil='ti',telas_permitidas=[])
        app=FastAPI();app.include_router(api.router)
        app.dependency_overrides[api.valida_token_usuario_atual]=lambda:self.user
        app.dependency_overrides[api.get_session_postgres]=lambda:self.session
        app.dependency_overrides[api.get_session_oracle]=lambda:None
        self.client=TestClient(app)

    def selection(self):
        result=self.client.post(BASE+'/preparar',json={'company':1,'supplier':'12','keys':['mv:1:9:42']})
        self.assertEqual(result.status_code,200,result.text)
        return result.json()

    def confirm(self):
        preview=self.selection()
        payload=dict(company=1,supplier='12',keys=['mv:1:9:42'],fingerprint=preview['fingerprint'],confirmed=True,note='Conferido no banco.')
        result=self.client.post(BASE+'/confirmar',data={'payload':json.dumps(payload)},files={
            'files':('comprovante.pdf',b'%PDF-1.4\nfixture','application/pdf')})
        self.assertEqual(result.status_code,200,result.text)
        return result.json()

    def test_unique_payment_keys_and_company_scope(self):
        items=payment_items([self.position],date(2026,1,1),date(2026,12,31))
        self.assertEqual(items[0]['key'],'mv:1:9:42')
        self.assertEqual(items[0]['amount'],'100.00')

    def test_notice_uses_readable_payment_dates(self):
        selected=select_payments([self.position],['mv:1:9:42'])
        confirmation={'name':'Fornecedor','company':1,'items':selected['items'],'total':selected['total']}
        self.assertIn('20/11/2026',notice_message(confirmation,'https://example.com/#link'))

    def test_whatsapp_preparation_and_send_confirmation_are_distinct(self):
        c=self.confirm()
        payload={'token':c['token'],'channel':'whatsapp','result':'preparado','destination':'85999999999'}
        self.assertEqual(self.client.post(BASE+'/confirmacoes/'+c['id']+'/comunicacoes',json=payload).status_code,200)
        self.assertEqual(self.client.get(BASE+'/confirmacoes/'+c['id']).json()['stage'],'confirmado')
        payload['result']='enviado_confirmado'
        self.assertEqual(self.client.post(BASE+'/confirmacoes/'+c['id']+'/comunicacoes',json=payload).status_code,200)
        self.assertEqual(self.client.get(BASE+'/confirmacoes/'+c['id']).json()['stage'],'aviso_registrado')

    def test_mixed_supplier_selection_is_rejected(self):
        other={**self.position,'company':2,'supplier':'13'}
        with self.assertRaisesRegex(ValueError,'mesma empresa'):
            select_payments([self.position,other],['mv:1:9:42','mv:2:9:42'])

    def test_confirmation_requires_bank_receipt(self):
        preview=self.selection()
        result=self.client.post(BASE+'/confirmar',data={'payload':json.dumps(dict(company=1,supplier='12',keys=['mv:1:9:42'],
            fingerprint=preview['fingerprint'],confirmed=True))})
        self.assertEqual(result.status_code,422,result.text)

    def test_confirmation_is_immutable_and_does_not_send_message(self):
        with patch.object(api,'send_payment_email') as send:
            result=self.confirm()
        send.assert_not_called()
        detail=self.client.get(BASE+'/confirmacoes/'+result['id']).json()
        self.assertEqual(detail['confirmation']['total'],'100.00')
        self.assertEqual(len(detail['files']),1)
        duplicate=self.client.post(BASE+'/preparar',json={'company':1,'supplier':'12','keys':['mv:1:9:42']})
        self.assertEqual(duplicate.status_code,409)

    def test_only_financial_manager_can_confirm(self):
        self.user=SimpleNamespace(id=8,perfil='usuario',telas_permitidas=['contas_pagar_gestao'])
        self.assertEqual(self.client.post(BASE+'/confirmar',data={}).status_code,403)

    def test_reversal_after_preview_blocks_confirmation(self):
        preview=self.selection()
        self.position['titles'][0]['payments']=[]
        payload=dict(company=1,supplier='12',keys=['mv:1:9:42'],fingerprint=preview['fingerprint'],confirmed=True)
        result=self.client.post(BASE+'/confirmar',data={'payload':json.dumps(payload)},files={'files':('p.pdf',b'%PDF-1.4\nx','application/pdf')})
        self.assertEqual(result.status_code,409)

    def test_email_attaches_notice_and_bank_receipt_only_after_confirmation(self):
        confirmation=self.confirm()
        with patch.object(api,'send_payment_email') as send:
            result=self.client.post(BASE+'/confirmacoes/'+confirmation['id']+'/email',json={'token':confirmation['token'],'destination':'ana@example.com'})
        self.assertEqual(result.status_code,200,result.text)
        kwargs=send.call_args.kwargs
        self.assertEqual(len(kwargs['files']),2)
        self.assertTrue(kwargs['files'][0]['content'].startswith(b'%PDF-'))
        self.assertEqual(kwargs['files'][1]['name'],'comprovante.pdf')

    def test_public_link_exposes_only_selected_confirmation_and_documents(self):
        confirmation=self.confirm()
        result=self.client.post(PUBLIC+'/consultar',json={'token':confirmation['token']})
        self.assertEqual(result.status_code,200,result.text)
        self.assertEqual(result.json()['total'],'100.00')
        result=self.client.post(PUBLIC+'/arquivo',json={'token':confirmation['token'],'file':'aviso'})
        self.assertEqual(result.status_code,200,result.text)
        self.assertTrue(result.content.startswith(b'%PDF-'))
        other=self.client.post(PUBLIC+'/arquivo',json={'token':confirmation['token'],'file':'someone-else'})
        self.assertEqual(other.status_code,404)

    def test_reversed_payment_blocks_sending_and_public_access(self):
        confirmation=self.confirm()
        self.position['titles'][0]['payments']=[]
        with patch.object(api,'send_payment_email') as send:
            result=self.client.post(BASE+'/confirmacoes/'+confirmation['id']+'/email',json={'token':confirmation['token'],'destination':'ana@example.com'})
        self.assertEqual(result.status_code,409)
        send.assert_not_called()
        self.assertEqual(self.client.post(PUBLIC+'/consultar',json={'token':confirmation['token']}).status_code,410)

    def test_expired_token_cannot_download_receipt(self):
        confirmation=self.confirm()
        self.session.execute(update(payment_links).values(expires_at=api.now()-timedelta(days=1)))
        self.session.commit()
        self.assertEqual(self.client.post(PUBLIC+'/arquivo',json={'token':confirmation['token'],'file':'aviso'}).status_code,410)

    def test_revocation_immediately_blocks_public_download(self):
        c=self.confirm()
        result=self.client.post(BASE+'/confirmacoes/'+c['id']+'/revogar',json={})
        self.assertEqual(result.status_code,200,result.text)
        self.assertEqual(self.client.post(PUBLIC+'/arquivo',json={'token':c['token'],'file':'aviso'}).status_code,410)

    def test_expired_link_cannot_be_marked_as_sent_on_whatsapp(self):
        c=self.confirm()
        self.session.execute(update(payment_links).values(expires_at=api.now()-timedelta(days=1)));self.session.commit()
        result=self.client.post(BASE+'/confirmacoes/'+c['id']+'/comunicacoes',json={
            'token':c['token'],'channel':'whatsapp','result':'enviado_confirmado','destination':'85999999999'})
        self.assertEqual(result.status_code,409,result.text)
        self.assertEqual(self.client.get(BASE+'/confirmacoes/'+c['id']).json()['stage'],'confirmado')


if __name__=='__main__':unittest.main()
