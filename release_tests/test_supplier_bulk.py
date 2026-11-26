import unittest
from datetime import timedelta
from sqlalchemy import create_engine,select,update,event
from sqlalchemy.orm import Session
from app_prontocardio.services.supplier_bulk import BulkService,Blocked,Rejected,Uncertain
from app_prontocardio.services.supplier_bulk_models import metadata,jobs,recipients,attempts
from app_prontocardio.services.supplier_updates_store import now


class Source:
    def __init__(self):self.changed=False;self.prepared=[];self.accepted=[];self.revoked=False
    def load(self,kind,id,channel):
        return dict(target=id,company=1,supplier=id,name='Fornecedor '+id,
            destination='' if id=='missing' else id+'@example.com',fingerprint='changed' if self.changed else id,
            documents=['aviso-'+id+'.pdf','comprovante-'+id+'.pdf'] if kind=='payments' else [],total='100.00' if kind=='payments' else None)
    def verify(self,snapshot):
        if self.revoked:raise Blocked('Pagamento estornado.')
    def prepare(self,snapshot,token,link_ref,actor):
        self.prepared.append((snapshot['target'],token))
        return dict(link_ref=link_ref or 'link-'+snapshot['target'],message='Individual '+snapshot['target'],documents=snapshot['documents'])
    def record_accept(self,*args):self.accepted.append(args)


class Provider:
    def __init__(self):self.calls=[];self.mode=None;self.available=True;self.enabled=True
    def configuration(self,kind,channel):
        return dict(available=self.available,enabled=self.enabled,reason='' if self.available else 'Modelo não configurado.',binding='test-binding',body='Mensagem de teste')
    def send(self,channel,snapshot,prepared):
        self.calls.append((channel,snapshot,prepared))
        if self.mode=='reject':raise Rejected('Destinatário recusado.')
        if self.mode=='uncertain':raise Uncertain('Servidor não confirmou o resultado.')
        return 'provider-'+snapshot['target']


class BulkTests(unittest.TestCase):
    def setUp(self):
        self.engine=create_engine('sqlite://',execution_options={'schema_translate_map':{'api_prontocardio':None}})
        metadata.create_all(self.engine)
        self.session=Session(self.engine)
        self.addCleanup(self.engine.dispose);self.addCleanup(self.session.close)
        self.source=Source();self.provider=Provider();self.allowed=True
        def authorize(actor,kind):
            if not self.allowed:raise Blocked('Permissão revogada.')
        self.service=BulkService(self.session,self.source,self.provider,authorize,secret='isolated-bulk-secret')

    def preview(self,ids=None,kind='updates'):
        return self.service.preview(kind,ids or ['1','2'],'email',7,'request-1')

    def approve(self,job):return self.service.approve(job['id'],job['version'],7,True)

    def test_preview_has_no_links_or_sends_and_deduplicates_selection(self):
        job=self.preview(['1','1','missing'])
        self.assertEqual(len(job['recipients']),2)
        self.assertEqual(job['counts']['ready'],1)
        self.assertEqual(job['counts']['blocked'],1)
        self.assertEqual(self.source.prepared,[]);self.assertEqual(self.provider.calls,[])

    def test_preview_and_approval_are_idempotent(self):
        job=self.preview()
        again=self.preview()
        self.assertEqual(job['id'],again['id'])
        self.approve(job);self.approve(job)
        self.assertEqual(self.session.execute(select(recipients)).mappings().all().__len__(),2)
        self.service.dispatch_one();self.service.dispatch_one();self.service.dispatch_one()
        self.assertEqual(len(self.provider.calls),2)

    def test_request_key_cannot_be_reused_for_another_selection(self):
        self.preview(['1'])
        with self.assertRaises(ValueError):self.preview(['2'])

    def test_old_worker_losing_lease_cannot_send(self):
        job=self.preview(['1']);self.approve(job)
        original=self.source.load
        def takeover(*args):
            snapshot=original(*args)
            self.session.execute(update(recipients).where(recipients.c.job_id==job['id']).values(lease_token='another-worker'))
            self.session.commit()
            return snapshot
        self.source.load=takeover
        self.service.dispatch_one()
        self.assertEqual(self.provider.calls,[])

    def test_revoked_authorization_blocks_worker(self):
        job=self.preview(['1']);self.approve(job);self.allowed=False
        self.service.dispatch_one();self.assertEqual(self.provider.calls,[])
        self.assertEqual(self.service.get(job['id'])['counts']['blocked'],1)

    def test_documents_and_tokens_are_individual_and_not_persisted_plaintext(self):
        job=self.preview(kind='payments');self.approve(job)
        self.service.dispatch_one();self.service.dispatch_one()
        self.assertNotEqual(self.source.prepared[0][1],self.source.prepared[1][1])
        self.assertEqual(self.provider.calls[0][2]['documents'],['aviso-1.pdf','comprovante-1.pdf'])
        raw=str(self.session.execute(select(recipients)).mappings().all())
        self.assertNotIn(self.source.prepared[0][1],raw)

    def test_only_explicit_failures_can_be_retried(self):
        job=self.preview(['1']);self.approve(job);self.provider.mode='reject';self.service.dispatch_one()
        self.assertEqual(self.service.get(job['id'])['counts']['failed'],1)
        self.service.retry_failed(job['id'],7);self.provider.mode=None;self.service.dispatch_one()
        self.assertEqual(len(self.provider.calls),2)
        self.service.retry_failed(job['id'],7);self.service.dispatch_one()
        self.assertEqual(len(self.provider.calls),2)

    def test_uncertain_delivery_is_never_automatically_retried(self):
        job=self.preview(['1']);self.approve(job);self.provider.mode='uncertain';self.service.dispatch_one()
        self.assertEqual(self.service.get(job['id'])['counts']['unknown'],1)
        self.service.retry_failed(job['id'],7);self.service.dispatch_one()
        self.assertEqual(len(self.provider.calls),1)

    def test_changed_contact_or_document_blocks_before_send(self):
        job=self.preview(['1']);self.approve(job);self.source.changed=True
        self.service.dispatch_one()
        self.assertEqual(self.service.get(job['id'])['counts']['blocked'],1)
        self.assertEqual(self.provider.calls,[])

    def test_payment_reversal_or_permission_revocation_blocks_dispatch(self):
        job=self.preview(['1'],kind='payments');self.approve(job);self.source.revoked=True
        self.service.dispatch_one();self.assertEqual(self.provider.calls,[])
        self.assertEqual(self.service.get(job['id'])['counts']['blocked'],1)

    def test_draft_contact_can_be_corrected_before_approval(self):
        job=self.preview(['missing']);r=job['recipients'][0]
        updated=self.service.set_destination(job['id'],r['id'],'correto@example.com',7)
        self.assertEqual(updated['counts']['ready'],1)
        self.approve(updated);self.service.dispatch_one()
        self.assertEqual(self.provider.calls[0][1]['destination'],'correto@example.com')

    def test_cancelled_batch_does_not_send(self):
        job=self.preview(['1']);self.approve(job);self.service.cancel(job['id'],7)
        self.service.dispatch_one();self.assertEqual(self.provider.calls,[])

    def test_crashed_sending_attempt_becomes_unknown_not_queued(self):
        job=self.preview(['1']);self.approve(job)
        self.session.execute(update(recipients).values(status='sending',lease_until=now()-timedelta(minutes=1)));self.session.commit()
        self.service.recover()
        self.assertEqual(self.service.get(job['id'])['counts']['unknown'],1)
        self.service.dispatch_one();self.assertEqual(self.provider.calls,[])

    def test_unavailable_channel_cannot_be_approved(self):
        self.provider.available=False;job=self.preview(['1'])
        self.assertEqual(job['counts']['blocked'],1)
        with self.assertRaises(ValueError):self.approve(job)

    def test_claim_statement_is_bounded_to_one_recipient(self):
        job=self.preview();self.approve(job);statements=[]
        def capture(conn,cursor,statement,parameters,context,many):
            if 'JOIN' in statement and 'supplier_bulk_jobs' in statement and 'supplier_bulk_recipients' in statement:
                statements.append(statement)
        event.listen(self.engine,'before_cursor_execute',capture)
        self.addCleanup(lambda:event.remove(self.engine,'before_cursor_execute',capture))
        self.service.dispatch_one()
        self.assertTrue(statements)
        self.assertIn('LIMIT',statements[0])


if __name__=='__main__':unittest.main()
