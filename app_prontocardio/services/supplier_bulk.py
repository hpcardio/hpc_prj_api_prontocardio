"""Explicitly approved persistent bulk outbox with conservative delivery recovery."""
import base64
import hashlib
import hmac
import json
import re
import uuid
from collections import Counter
from datetime import timedelta

from sqlalchemy import select,update
from sqlalchemy.exc import IntegrityError
from app_prontocardio.services.supplier_bulk_models import jobs,recipients,attempts
from app_prontocardio.services.supplier_updates_store import now


class Blocked(ValueError):pass
class Rejected(Exception):pass
class Uncertain(Exception):pass


def normalize_destination(channel,value):
    value=value.strip()
    if channel=='email':
        if not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+',value):raise Blocked('Informe um e-mail válido.')
        return value
    phone=re.sub(r'\D','',value)
    if len(phone) in (10,11):phone='55'+phone
    if not 12<=len(phone)<=15:raise Blocked('Informe WhatsApp com DDI e DDD.')
    return phone


class BulkService:
    def __init__(self,session,source,provider,authorize,secret):
        self.session,self.source,self.provider,self.authorize,self.secret=session,source,provider,authorize,secret

    def row(self,id,lock=False):
        query=select(jobs).where(jobs.c.id==id)
        row=self.session.execute(query.with_for_update() if lock else query).mappings().first()
        if not row:raise LookupError('Lote não encontrado.')
        return dict(row)

    def get(self,id):
        job=self.row(id)
        rows=[dict(r) for r in self.session.execute(select(recipients).where(recipients.c.job_id==id).order_by(recipients.c.target)).mappings()]
        job['recipients']=rows
        job['counts']={s:sum(r['status']==s for r in rows) for s in ('ready','blocked','queued','preparing','sending','accepted','failed','unknown','cancelled')}
        job['attempts']=[dict(r) for r in self.session.execute(select(attempts).join(recipients).where(recipients.c.job_id==id).order_by(attempts.c.created_at.desc())).mappings()]
        return job

    def evaluate(self,kind,target,channel,configuration,destination=None):
        snapshot={}
        try:
            snapshot=self.source.load(kind,target,channel)
            recipient=normalize_destination(channel,snapshot.get('destination','') if destination is None else destination)
            if not configuration['available']:raise Blocked(configuration['reason'])
            return snapshot,recipient,'ready',''
        except (Blocked,LookupError) as exc:
            return snapshot,destination or snapshot.get('destination',''),'blocked',str(exc)

    def preview(self,kind,ids,channel,actor,request_key):
        if kind not in ('updates','payments') or channel not in ('email','whatsapp'):raise ValueError('Tipo de lote ou canal inválido.')
        if not ids or len(ids)>2000 or not request_key or len(request_key)>64:raise ValueError('Selecione de 1 a 2.000 registros.')
        self.authorize(actor,kind)
        input_fingerprint=hashlib.sha256(json.dumps([kind,channel,sorted(set(str(v) for v in ids))]).encode()).hexdigest()
        existing=self.session.execute(select(jobs).where(jobs.c.created_by==actor,jobs.c.request_key==request_key)).mappings().first()
        if existing:
            if existing['input_fingerprint']!=input_fingerprint:raise ValueError('Esta prévia pertence a outra seleção. Prepare um novo lote.')
            return self.get(existing['id'])
        configuration=self.provider.configuration(kind,channel)
        id=str(uuid.uuid4())
        try:
            self.session.execute(jobs.insert().values(id=id,kind=kind,channel=channel,status='draft',created_by=actor,
                request_key=request_key,input_fingerprint=input_fingerprint,version=1,configuration=configuration,created_at=now()))
            for target in sorted(set(str(v) for v in ids)):
                if len(target)>100:raise ValueError('Identificador inválido.')
                snapshot,destination,status,error=self.evaluate(kind,target,channel,configuration)
                self.session.execute(recipients.insert().values(id=str(uuid.uuid4()),job_id=id,target=target,snapshot=snapshot,
                    destination=destination,status=status,error=error,link_nonce=str(uuid.uuid4())))
            self.session.commit()
        except IntegrityError:
            self.session.rollback()
            existing=self.session.execute(select(jobs).where(jobs.c.created_by==actor,jobs.c.request_key==request_key)).mappings().first()
            if not existing:raise
            if existing['input_fingerprint']!=input_fingerprint:raise ValueError('Esta prévia pertence a outra seleção. Prepare um novo lote.')
            id=existing['id']
        return self.get(id)

    def set_destination(self,id,recipient_id,destination,actor):
        job=self.row(id,True);self.authorize(actor,job['kind'])
        if job['status']!='draft':raise ValueError('O lote já foi autorizado. Prepare outra prévia para alterar contatos.')
        row=self.session.execute(select(recipients).where(recipients.c.job_id==id,recipients.c.id==recipient_id)).mappings().first()
        if not row:raise LookupError('Destinatário não encontrado neste lote.')
        destination=normalize_destination(job['channel'],destination)
        snapshot,destination,status,error=self.evaluate(job['kind'],row['target'],job['channel'],job['configuration'],destination)
        self.session.execute(update(recipients).where(recipients.c.id==recipient_id).values(snapshot=snapshot,destination=destination,
            destination_override=True,status=status,error=error))
        self.session.execute(update(jobs).where(jobs.c.id==id).values(version=jobs.c.version+1))
        self.session.commit();return self.get(id)

    def refresh(self,id,actor):
        job=self.row(id,True);self.authorize(actor,job['kind'])
        if job['status']!='draft':raise ValueError('Somente prévias podem ser atualizadas.')
        configuration=self.provider.configuration(job['kind'],job['channel'])
        for row in self.session.execute(select(recipients).where(recipients.c.job_id==id)).mappings():
            snapshot,destination,status,error=self.evaluate(job['kind'],row['target'],job['channel'],configuration,
                row['destination'] if row['destination_override'] else None)
            self.session.execute(update(recipients).where(recipients.c.id==row['id']).values(snapshot=snapshot,destination=destination,status=status,error=error))
        self.session.execute(update(jobs).where(jobs.c.id==id).values(configuration=configuration,version=jobs.c.version+1))
        self.session.commit();return self.get(id)

    def approve(self,id,version,actor,confirm_resend):
        job=self.row(id,True);self.authorize(actor,job['kind'])
        if job['status']!='draft':return self.get(id)
        if job['version']!=version:raise ValueError('A prévia foi alterada. Confira a versão atual antes de autorizar.')
        configuration=self.provider.configuration(job['kind'],job['channel'])
        if not configuration['available'] or not configuration['enabled']:raise ValueError(configuration.get('reason') or 'Envio automático não liberado neste ambiente.')
        if configuration['binding']!=job['configuration']['binding']:raise ValueError('A configuração do canal mudou. Atualize a prévia.')
        ready=list(self.session.execute(select(recipients).where(recipients.c.job_id==id,recipients.c.status=='ready')).mappings())
        if not ready:raise ValueError('Não há destinatários aptos para envio.')
        if any(r['snapshot'].get('previous_send') for r in ready) and not confirm_resend:raise ValueError('Confirme o reenvio aos destinatários já comunicados.')
        for r in ready:
            fresh=self.source.load(job['kind'],r['target'],job['channel'])
            if fresh['fingerprint']!=r['snapshot']['fingerprint']:raise ValueError('Dados de um destinatário foram alterados. Atualize a prévia.')
        self.session.execute(update(recipients).where(recipients.c.job_id==id,recipients.c.status=='ready').values(status='queued'))
        self.session.execute(update(jobs).where(jobs.c.id==id).values(status='queued',approved_by=actor,approved_at=now(),version=version+1))
        self.session.commit();return self.get(id)

    def cancel(self,id,actor):
        job=self.row(id,True);self.authorize(actor,job['kind'])
        self.session.execute(update(jobs).where(jobs.c.id==id).values(status='cancelled',cancelled_by=actor))
        self.session.execute(update(recipients).where(recipients.c.job_id==id,recipients.c.status.in_(['ready','queued'])).values(status='cancelled'))
        self.session.commit();return self.get(id)

    def retry_failed(self,id,actor):
        job=self.row(id,True);self.authorize(actor,job['kind'])
        if job['status']=='cancelled':raise ValueError('O lote foi cancelado.')
        result=self.session.execute(update(recipients).where(recipients.c.job_id==id,recipients.c.status=='failed').values(status='queued',error=''))
        if result.rowcount:self.session.execute(update(jobs).where(jobs.c.id==id).values(status='queued',approved_by=actor))
        self.session.commit();return self.get(id)

    def finish(self,id):
        row=self.row(id)
        if row['status']=='cancelled':return
        states=list(self.session.execute(select(recipients.c.status).where(recipients.c.job_id==id)).scalars())
        if states and not any(s in ('queued','preparing','sending') for s in states):
            self.session.execute(update(jobs).where(jobs.c.id==id).values(status='completed' if all(s=='accepted' for s in states) else 'completed_with_issues'))

    def recover(self):
        expired=list(self.session.execute(select(recipients).where(recipients.c.status.in_(['preparing','sending']),recipients.c.lease_until<now())).mappings())
        for row in expired:
            state='unknown' if row['status']=='sending' else ('cancelled' if self.row(row['job_id'])['status']=='cancelled' else 'queued')
            self.session.execute(update(recipients).where(recipients.c.id==row['id'],recipients.c.lease_token==row['lease_token'],recipients.c.status==row['status']).values(
                status=state,error='Envio sem confirmação. Conferir antes de reenviar.' if state=='unknown' else '',lease_token=None,lease_until=None))
            if state=='unknown':self.session.execute(update(attempts).where(attempts.c.recipient_id==row['id'],attempts.c.status=='sending').values(status='unknown',finished_at=now()))
            self.finish(row['job_id'])
        self.session.commit()

    def dispatch_one(self):
        self.recover()
        query=select(recipients).join(jobs).where(recipients.c.status=='queued',jobs.c.status.in_(['queued','running'])).order_by(jobs.c.created_at,recipients.c.target).limit(1)
        row=self.session.execute(query.with_for_update(of=recipients,skip_locked=True)).mappings().first()
        if not row:self.session.rollback();return False
        row=dict(row);lease=str(uuid.uuid4());job=self.row(row['job_id'])
        self.session.execute(update(recipients).where(recipients.c.id==row['id']).values(status='preparing',lease_token=lease,lease_until=now()+timedelta(minutes=5)))
        self.session.execute(update(jobs).where(jobs.c.id==job['id']).values(status='running'));self.session.commit()
        attempt_id=None
        try:
            actor=job['approved_by'];self.authorize(actor,job['kind'])
            configuration=self.provider.configuration(job['kind'],job['channel'])
            if not configuration['available'] or not configuration['enabled'] or configuration['binding']!=job['configuration']['binding']:
                raise Blocked(configuration.get('reason') or 'Configuração do canal alterada. Prepare uma nova prévia.')
            snapshot=self.source.load(job['kind'],row['target'],job['channel'])
            if snapshot['fingerprint']!=row['snapshot']['fingerprint']:raise Blocked('Contato ou documentos alterados. Prepare uma nova prévia.')
            self.source.verify(snapshot)
            current=self.session.execute(select(recipients).where(recipients.c.id==row['id']).with_for_update()).mappings().one()
            if current['lease_token']!=lease or current['status']!='preparing':self.session.rollback();return True
            if self.row(job['id'])['status']=='cancelled':
                self.session.execute(update(recipients).where(recipients.c.id==row['id']).values(status='cancelled'));self.session.commit();return True
            snapshot={**snapshot,'destination':row['destination'],'channel':job['channel'],'kind':job['kind']}
            token=base64.urlsafe_b64encode(hmac.new(self.secret.encode(),('supplier-bulk:'+row['id']+':'+row['link_nonce']).encode(),hashlib.sha256).digest()).decode().rstrip('=')
            prepared=self.source.prepare(snapshot,token,row['link_ref'],actor)
            attempt_id=str(uuid.uuid4())
            self.session.execute(attempts.insert().values(id=attempt_id,recipient_id=row['id'],status='sending',actor=actor,created_at=now()))
            self.session.execute(update(recipients).where(recipients.c.id==row['id']).values(status='sending',link_ref=prepared['link_ref'],lease_until=now()+timedelta(minutes=10)))
            self.session.commit()
            provider_id=self.provider.send(job['channel'],snapshot,prepared)
            self.source.record_accept(snapshot,actor,job['channel'],provider_id,job['id'],row['id'])
            self.session.execute(update(recipients).where(recipients.c.id==row['id'],recipients.c.lease_token==lease,recipients.c.status=='sending').values(
                status='accepted',provider_id=provider_id,accepted_at=now(),lease_token=None,lease_until=None,error=''))
            self.session.execute(update(attempts).where(attempts.c.id==attempt_id).values(status='accepted',provider_id=provider_id,finished_at=now()))
        except Exception as exc:
            self.session.rollback()
            state='failed' if isinstance(exc,Rejected) else ('blocked' if attempt_id is None else 'unknown')
            reason=str(exc) if isinstance(exc,(Blocked,Rejected,Uncertain)) else ('Falha ao conferir o destinatário.' if state=='blocked' else 'Envio sem confirmação. Conferir antes de reenviar.')
            self.session.execute(update(recipients).where(recipients.c.id==row['id'],recipients.c.lease_token==lease).values(status=state,error=reason,lease_token=None,lease_until=None))
            if attempt_id:self.session.execute(update(attempts).where(attempts.c.id==attempt_id).values(status=state,error=reason,finished_at=now()))
        self.finish(job['id']);self.session.commit();return True
