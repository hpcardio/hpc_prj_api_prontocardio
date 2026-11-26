import hashlib
import secrets
import uuid
from datetime import timedelta

from sqlalchemy import Table,Column,Integer,String,Text,DateTime,JSON,Numeric,LargeBinary,ForeignKey,select,update
from app_prontocardio.services.supplier_updates_store import metadata,now,aware

confirmations=Table('supplier_payment_confirmations',metadata,
    Column('id',String(36),primary_key=True),Column('company',Integer,nullable=False),
    Column('company_name',String(200),nullable=False),Column('supplier',String(100),nullable=False),
    Column('name',String(200),nullable=False),Column('document',String(20),nullable=False),
    Column('items',JSON,nullable=False),Column('fingerprint',String(64),nullable=False),
    Column('total',Numeric(15,2),nullable=False),Column('note',Text,nullable=False),
    Column('confirmed_by',Integer,nullable=False),Column('confirmed_at',DateTime(timezone=True),nullable=False))
confirmed_items=Table('supplier_payment_items',metadata,
    Column('key',String(100),primary_key=True),Column('confirmation_id',ForeignKey(confirmations.c.id),nullable=False,index=True))
payment_files=Table('supplier_payment_files',metadata,
    Column('id',String(36),primary_key=True),Column('confirmation_id',ForeignKey(confirmations.c.id),nullable=False,index=True),
    Column('name',String(200),nullable=False),Column('content_type',String(100),nullable=False),
    Column('sha256',String(64),nullable=False),Column('content',LargeBinary,nullable=False))
payment_links=Table('supplier_payment_links',metadata,
    Column('id',String(36),primary_key=True),Column('confirmation_id',ForeignKey(confirmations.c.id),nullable=False,index=True),
    Column('token_hash',String(64),nullable=False,unique=True),Column('expires_at',DateTime(timezone=True),nullable=False),
    Column('revoked_at',DateTime(timezone=True)),Column('created_by',Integer,nullable=False),
    Column('created_at',DateTime(timezone=True),nullable=False))
payment_events=Table('supplier_payment_events',metadata,
    Column('id',Integer,primary_key=True),Column('confirmation_id',ForeignKey(confirmations.c.id),nullable=False,index=True),
    Column('actor',Integer,nullable=False),Column('kind',String(40),nullable=False),Column('details',JSON,nullable=False),
    Column('created_at',DateTime(timezone=True),nullable=False))


class PaymentStore:
    def __init__(self,session):self.session=session

    def get(self,id,lock=False):
        query=select(confirmations).where(confirmations.c.id==id)
        row=self.session.execute(query.with_for_update() if lock else query).mappings().first()
        if not row:raise LookupError('Confirmação de pagamento não encontrada.')
        result=dict(row)
        result['total']=f"{result['total']:.2f}"
        return result

    def event(self,id,actor,kind,details=None):
        self.session.execute(payment_events.insert().values(confirmation_id=id,actor=actor,kind=kind,details=details or {},created_at=now()))

    def ensure_new(self,keys):
        if self.session.execute(select(confirmed_items.c.key).where(confirmed_items.c.key.in_(keys))).first():
            raise ValueError('Um dos pagamentos já foi confirmado. Consulte o aviso existente.')

    def confirm(self,selection,actor,files,note):
        self.ensure_new([p['key'] for p in selection['items']])
        if not files:raise ValueError('Anexe ao menos um comprovante bancário.')
        first=selection['items'][0]
        id=str(uuid.uuid4())
        self.session.execute(confirmations.insert().values(id=id,company=first['company'],company_name=first['company_name'],
            supplier=first['supplier'],name=first['name'],document=first['document'],items=selection['items'],
            fingerprint=selection['fingerprint'],total=selection['total'],note=note,confirmed_by=actor,confirmed_at=now()))
        for payment in selection['items']:
            self.session.execute(confirmed_items.insert().values(key=payment['key'],confirmation_id=id))
        for file in files:
            self.session.execute(payment_files.insert().values(id=str(uuid.uuid4()),confirmation_id=id,name=file['name'],
                content_type=file['content_type'],sha256=hashlib.sha256(file['content']).hexdigest(),content=file['content']))
        self.event(id,actor,'pagamento_confirmado',{'items':len(selection['items']),'total':selection['total']})
        return self.get(id)

    def link(self,id,actor,token=None):
        self.get(id,lock=True)
        self.session.execute(update(payment_links).where(payment_links.c.confirmation_id==id,payment_links.c.revoked_at.is_(None)).values(revoked_at=now()))
        token=token or secrets.token_urlsafe(32)
        link_id=str(uuid.uuid4())
        expires=now()+timedelta(days=14)
        self.session.execute(payment_links.insert().values(id=link_id,confirmation_id=id,
            token_hash=hashlib.sha256(token.encode()).hexdigest(),expires_at=expires,created_by=actor,created_at=now()))
        self.event(id,actor,'link_gerado',{'expires_at':expires.isoformat()})
        return {'link_id':link_id,'token':token,'expires_at':expires.isoformat()}

    def resolve(self,token):
        if not 40<=len(token)<=64:raise ValueError('Link indisponível. Solicite um novo aviso ao financeiro.')
        row=self.session.execute(select(payment_links).where(payment_links.c.token_hash==hashlib.sha256(token.encode()).hexdigest())).mappings().first()
        if not row or row['revoked_at'] or aware(row['expires_at'])<=now():
            raise ValueError('Link indisponível. Solicite um novo aviso ao financeiro.')
        return self.get(row['confirmation_id'])

    def revoke(self,id,actor):
        self.get(id,lock=True)
        self.session.execute(update(payment_links).where(payment_links.c.confirmation_id==id,
            payment_links.c.revoked_at.is_(None)).values(revoked_at=now()))
        self.event(id,actor,'links_revogados')

    def files(self,id,with_content=False):
        columns=list(payment_files.c) if with_content else [payment_files.c.id,payment_files.c.name,payment_files.c.content_type]
        return [dict(r) for r in self.session.execute(select(*columns).where(payment_files.c.confirmation_id==id)).mappings()]
