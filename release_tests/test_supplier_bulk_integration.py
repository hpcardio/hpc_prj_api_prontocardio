import unittest
from types import SimpleNamespace
from unittest.mock import patch
from fastapi import FastAPI
from fastapi.testclient import TestClient
import test_supplier_payment_notices as fixtures
from app_prontocardio.services.supplier_bulk import BulkService,Blocked
from app_prontocardio.services.supplier_bulk_sources import SupplierBulkSource
from app_prontocardio.services.supplier_bulk_channels import SupplierBulkProvider,_templates
from app_prontocardio.routers import supplier_bulk as bulk_api
from app_prontocardio.services.supplier_updates_store import SupplierStore,cases
from fastapi import HTTPException
from app_prontocardio.services.supplier_bulk import Uncertain


class Provider:
    def __init__(self):self.calls=[]
    def configuration(self,kind,channel):return {'available':True,'enabled':True,'reason':'','body':'Modelo de teste','binding':'test'}
    def send(self,channel,snapshot,prepared):self.calls.append((snapshot,prepared));return 'accepted-test'


class IntegrationTests(unittest.TestCase):
    selection=fixtures.PaymentTests.selection
    confirm=fixtures.PaymentTests.confirm
    def setUp(self):fixtures.PaymentTests.setUp(self)

    def bulk_engine(self):
        self.provider=Provider()
        return BulkService(self.session,SupplierBulkSource(self.session,None),self.provider,lambda *_:None,'test-secret')

    def test_payment_batch_sends_only_its_notice_and_bank_file(self):
        c=self.confirm();engine=self.bulk_engine()
        job=engine.preview('payments',[c['id']],'email',7,'unique-request')
        self.assertEqual(job['counts']['blocked'],1)
        job=engine.set_destination(job['id'],job['recipients'][0]['id'],'financeiro@example.com',7)
        engine.approve(job['id'],job['version'],7,True);engine.dispatch_one()
        self.assertEqual(len(self.provider.calls),1)
        snapshot,prepared=self.provider.calls[0]
        self.assertEqual(snapshot['target'],c['id'])
        self.assertEqual([f['name'] for f in prepared['files']],['aviso-pagamento.pdf','comprovante.pdf'])
        self.assertTrue(prepared['files'][0]['content'].startswith(b'%PDF-'))

    def test_payment_reversal_after_preview_blocks_real_source(self):
        c=self.confirm();engine=self.bulk_engine();job=engine.preview('payments',[c['id']],'email',7,'unique-request')
        job=engine.set_destination(job['id'],job['recipients'][0]['id'],'financeiro@example.com',7)
        engine.approve(job['id'],job['version'],7,True)
        self.position['titles'][0]['payments']=[]
        engine.dispatch_one()
        self.assertEqual(self.provider.calls,[])
        self.assertEqual(engine.get(job['id'])['counts']['blocked'],1)

    def test_bulk_payment_preview_requires_financial_permission(self):
        app=FastAPI();app.include_router(bulk_api.router)
        app.dependency_overrides[fixtures.api.valida_token_usuario_atual]=lambda:SimpleNamespace(id=7,perfil='usuario',telas_permitidas=['contas_pagar_gestao'])
        app.dependency_overrides[fixtures.api.get_session_postgres]=lambda:self.session
        app.dependency_overrides[fixtures.api.get_session_oracle]=lambda:None
        client=TestClient(app)
        result=client.post(bulk_api.BASE+'/prever',json={'kind':'payments','channel':'email','ids':['not-confirmed'],'request_key':'1234567890'})
        self.assertEqual(result.status_code,403)

    def test_all_filtered_preview_is_scoped_and_approval_does_not_send(self):
        store=SupplierStore(self.session)
        for company in (1,2):
            case=store.create_case({'company':company,'supplier':str(company),'name':'Fornecedor '+str(company),'document':''},7)
            self.session.execute(cases.update().where(cases.c.id==case['id']).values(contact={'email':'financeiro@example.com'}))
        self.session.commit()
        app=FastAPI();app.include_router(bulk_api.router)
        app.dependency_overrides[fixtures.api.valida_token_usuario_atual]=lambda:self.user
        app.dependency_overrides[fixtures.api.get_session_postgres]=lambda:self.session
        app.dependency_overrides[fixtures.api.get_session_oracle]=lambda:None
        provider=Provider();client=TestClient(app)
        with patch.object(bulk_api,'SupplierBulkProvider',return_value=provider):
            result=client.post(bulk_api.BASE+'/prever',json={'kind':'updates','channel':'email','all_filtered':True,
                'filters':{'company':'1','scope':'todas'},'request_key':'scoped-selection'})
            self.assertEqual(result.status_code,200,result.text)
            job=result.json();self.assertEqual(job['total'],1)
            self.assertEqual(job['recipients'][0]['snapshot']['company'],1)
            self.assertNotIn('link_nonce',job['recipients'][0])
            approved=client.post(bulk_api.BASE+'/jobs/'+job['id']+'/aprovar',json={'version':job['version'],'confirmed':True})
            self.assertEqual(approved.status_code,200,approved.text)
            self.assertEqual(approved.json()['status'],'queued')
        self.assertEqual(provider.calls,[])


class TemplateTests(unittest.TestCase):
    def setUp(self):
        _templates.clear()
        self.settings=SimpleNamespace(SUPPLIER_BULK_SEND_ENABLED=True,SUPPLIER_WHATSAPP_WABA_ID='123',
            WHATSAPP_PHONE_NUMBER_ID='456',WHATSAPP_ACCESS_TOKEN='test',SUPPLIER_UPDATE_WHATSAPP_TEMPLATE='fornecedor_atualizacao',
            SUPPLIER_PAYMENT_WHATSAPP_TEMPLATE='fornecedor_pagamento',SUPPLIER_WHATSAPP_LANGUAGE='pt_BR',WHATSAPP_GRAPH_API_VERSION='v25.0')
        self.provider=SupplierBulkProvider(self.settings)

    def test_whatsapp_requires_approved_purpose_specific_template(self):
        template={'id':'789','name':'fornecedor_atualizacao','status':'PENDING','language':'pt_BR',
                  'components':[{'type':'BODY','text':'Fornecedor {{1}}, atualização cadastral: {{2}}'}]}
        with patch.object(self.provider,'graph_get',side_effect=[{'data':[{'id':'456'}]},{'data':[template]}]):
            self.assertFalse(self.provider.configuration('updates','whatsapp')['available'])
        template['status']='APPROVED'
        with patch.object(self.provider,'graph_get',side_effect=[{'data':[{'id':'456'}]},{'data':[template]}]):
            config=self.provider.configuration('updates','whatsapp')
        self.assertTrue(config['available']);self.assertIn('atualização',config['body'])

    def test_clinical_template_cannot_be_reused_for_suppliers(self):
        template={'id':'789','name':'fornecedor_atualizacao','status':'APPROVED','language':'pt_BR',
                  'components':[{'type':'BODY','text':'Olá {{1}}, sua consulta é em {{2}}.'}]}
        with patch.object(self.provider,'graph_get',side_effect=[{'data':[{'id':'456'}]},{'data':[template]}]):
            self.assertFalse(self.provider.configuration('updates','whatsapp')['available'])

    def test_server_error_from_meta_is_uncertain_not_retryable(self):
        for code in (500,503,408):
            with self.subTest(code=code),patch.object(self.provider,'template',return_value={'name':'fornecedor','language':'pt_BR'}), \
                 patch('app_prontocardio.services.supplier_bulk_channels.enviar_template_whatsapp',side_effect=HTTPException(code,'error')):
                with self.assertRaises(Uncertain):
                    self.provider.send('whatsapp',{'kind':'updates','name':'Fornecedor','destination':'5585999999999'}, {'link':'https://example.com'})


if __name__=='__main__':unittest.main()
