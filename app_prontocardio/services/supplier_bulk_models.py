"""Durable outbox: never persist bearer link credentials or file contents here."""
from sqlalchemy import Table,Column,String,Integer,DateTime,JSON,Text,Boolean,ForeignKey,UniqueConstraint
from app_prontocardio.services.supplier_updates_store import metadata

jobs=Table('supplier_bulk_jobs',metadata,
    Column('id',String(36),primary_key=True),Column('kind',String(15),nullable=False),
    Column('channel',String(15),nullable=False),Column('status',String(30),nullable=False),
    Column('created_by',Integer,nullable=False),Column('approved_by',Integer),
    Column('request_key',String(64),nullable=False),Column('version',Integer,nullable=False),
    Column('input_fingerprint',String(64),nullable=False),
    Column('configuration',JSON,nullable=False),Column('created_at',DateTime(timezone=True),nullable=False),
    Column('approved_at',DateTime(timezone=True)),Column('cancelled_by',Integer),
    UniqueConstraint('created_by','request_key',name='uq_supplier_bulk_request'))
recipients=Table('supplier_bulk_recipients',metadata,
    Column('id',String(36),primary_key=True),Column('job_id',ForeignKey(jobs.c.id),nullable=False,index=True),
    Column('target',String(100),nullable=False),Column('snapshot',JSON,nullable=False),
    Column('destination',String(254),nullable=False),Column('destination_override',Boolean,nullable=False,default=False),
    Column('status',String(25),nullable=False),Column('error',Text,nullable=False,default=''),
    Column('link_nonce',String(36),nullable=False),Column('link_ref',String(36)),
    Column('lease_token',String(36)),Column('lease_until',DateTime(timezone=True)),
    Column('provider_id',String(255)),Column('accepted_at',DateTime(timezone=True)),
    UniqueConstraint('job_id','target',name='uq_supplier_bulk_target'))
attempts=Table('supplier_bulk_attempts',metadata,
    Column('id',String(36),primary_key=True),Column('recipient_id',ForeignKey(recipients.c.id),nullable=False,index=True),
    Column('status',String(25),nullable=False),Column('actor',Integer,nullable=False),
    Column('created_at',DateTime(timezone=True),nullable=False),Column('finished_at',DateTime(timezone=True)),
    Column('error',Text,nullable=False,default=''),Column('provider_id',String(255)))
