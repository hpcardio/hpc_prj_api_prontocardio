"""Load only one recipient's documents; never combine companies or confirmations."""
import hashlib
import json
from sqlalchemy import select,update
from app_prontocardio.services.supplier_bulk import Blocked
from app_prontocardio.services.supplier_updates_store import SupplierStore,cases,events
from app_prontocardio.services.supplier_payment_store import PaymentStore,payment_events,payment_files
from app_prontocardio.services.supplier_payment_notices import notice_pdf
from app_prontocardio.routers.supplier_updates import invitation_message
from app_prontocardio.routers.supplier_payment_notices import check_current,packet


class SupplierBulkSource:
    def __init__(self,session,oracle):self.session,self.oracle=session,oracle

    def load(self,kind,target,channel):
        if kind=='updates':
            try:case=SupplierStore(self.session).case(int(target))
            except ValueError:raise Blocked('Fornecedor inválido.') from None
            contact=case['contact'] or {}
            previous=self.session.execute(select(events.c.created_at).where(events.c.case_id==case['id'],events.c.kind.in_(
                ['email_aceito_smtp','email_enviado_confirmado'] if channel=='email' else ['whatsapp_enviado_confirmado','whatsapp_aceito_meta'])).order_by(events.c.created_at.desc())).scalar()
            snapshot=dict(target=str(target),kind=kind,company=case['company'],supplier=case['supplier'],name=case['name'],document=case['document'],
                destination=contact.get('email' if channel=='email' else 'whatsapp',''),documents=[],total=None)
        else:
            confirmation=PaymentStore(self.session).get(target)
            contact=self.session.execute(select(cases.c.contact).where(cases.c.company==confirmation['company'],cases.c.supplier==confirmation['supplier'])).scalar_one_or_none() or {}
            file_rows=[dict(r) for r in self.session.execute(select(payment_files.c.id,payment_files.c.name,payment_files.c.sha256)
                .where(payment_files.c.confirmation_id==target)).mappings()]
            if not file_rows:raise Blocked('Pagamento sem comprovante bancário.')
            previous_event=self.session.execute(select(payment_events).where(payment_events.c.confirmation_id==target,payment_events.c.kind.in_(
                ['email_aceito_smtp'] if channel=='email' else ['whatsapp_enviado_confirmado','whatsapp_aceito_meta'])).order_by(payment_events.c.created_at.desc())).mappings().first()
            destination=contact.get('email' if channel=='email' else 'whatsapp','') or (previous_event['details'].get('destination','') if previous_event else '')
            previous=previous_event['created_at'] if previous_event else None
            snapshot=dict(target=target,kind=kind,company=confirmation['company'],supplier=confirmation['supplier'],name=confirmation['name'],document=confirmation['document'],
                destination=destination,total=confirmation['total'],payment_fingerprint=confirmation['fingerprint'],
                documents=[{'name':'Aviso de pagamento em PDF','id':'aviso'},*file_rows])
        snapshot['fingerprint']=hashlib.sha256(json.dumps(snapshot,sort_keys=True,default=str).encode()).hexdigest()
        snapshot['previous_send']=str(previous) if previous else None
        return snapshot

    def verify(self,snapshot):
        if snapshot['kind']=='payments':
            try:check_current(self.oracle,PaymentStore(self.session).get(snapshot['target']))
            except ValueError as exc:raise Blocked(str(exc)) from exc

    def prepare(self,snapshot,token,link_ref,actor):
        if snapshot['kind']=='updates':
            store=SupplierStore(self.session)
            if link_ref:
                invitation=store.resolve(token)
                if invitation['id']!=link_ref or invitation['case_id']!=int(snapshot['target']):raise Blocked('Convite revogado ou substituído. Prepare novo lote.')
                ref=link_ref
            else:ref=store.invite(int(snapshot['target']),actor,token=token)['id']
            invitation=invitation_message(token)
            return dict(link_ref=ref,message=invitation['message'],link=invitation['link'],files=[])
        store=PaymentStore(self.session);confirmation=store.get(snapshot['target'])
        if link_ref:
            if store.resolve(token)['id']!=snapshot['target']:raise Blocked('Aviso revogado ou substituído. Prepare novo lote.')
            ref=link_ref
        else:ref=store.link(snapshot['target'],actor,token=token)['link_id']
        invitation=packet(confirmation,{'token':token})
        files=[{'name':'aviso-pagamento.pdf','content_type':'application/pdf','content':notice_pdf(confirmation)},*store.files(snapshot['target'],with_content=True)]
        return dict(link_ref=ref,message=invitation['message'],link=invitation['link'],files=files)

    def record_accept(self,snapshot,actor,channel,provider_id,job_id,recipient_id):
        kind='email_aceito_smtp' if channel=='email' else 'whatsapp_aceito_meta'
        details={'destination':snapshot['destination'],'provider_id':provider_id,'batch_id':job_id,'recipient_id':recipient_id,'delivery_confirmed':False}
        if snapshot['kind']=='updates':
            store=SupplierStore(self.session);case=store.case(int(snapshot['target']),lock=True)
            store.event(case['id'],actor,kind,details)
            if case['stage']=='a_comunicar':self.session.execute(update(cases).where(cases.c.id==case['id']).values(stage='aguardando',version=cases.c.version+1))
        else:PaymentStore(self.session).event(snapshot['target'],actor,kind,details)
