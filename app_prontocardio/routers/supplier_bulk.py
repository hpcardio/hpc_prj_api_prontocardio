"""Preparation never sends. Only explicit approval makes recipients dispatchable."""
from typing import Literal
from datetime import date
from fastapi import APIRouter,HTTPException,Query
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel,Field
from sqlalchemy import select,func
from app_prontocardio.routers.supplier_updates import access,PG,Oracle,User,settings
from app_prontocardio.services.supplier_bulk import BulkService
from app_prontocardio.services.supplier_bulk_models import jobs,recipients
from app_prontocardio.services.supplier_bulk_sources import SupplierBulkSource
from app_prontocardio.services.supplier_bulk_channels import SupplierBulkProvider

BASE='/app_glosas/financeiro/contas-a-pagar/fornecedores-atualizacao/lotes'
router=APIRouter(tags=['envio-em-massa-fornecedores'])


class PreviewInput(BaseModel):
    kind:Literal['updates','payments']
    channel:Literal['email','whatsapp']
    ids:list[str]=Field(default_factory=list,max_length=2000)
    all_filtered:bool=False
    filters:dict[str,str]=Field(default_factory=dict,max_length=10)
    request_key:str=Field(min_length=10,max_length=64)


class ApproveInput(BaseModel):
    version:int=Field(gt=0)
    confirmed:bool
    confirm_resend:bool=False
    acknowledge_blocked:bool=False


class ContactInput(BaseModel):destination:str=Field(min_length=5,max_length=254)


def service(session,oracle,user):
    def authorize(actor,kind):
        if actor!=user.id:raise ValueError('Operador inválido.')
        access(user,financial=kind=='payments')
    return BulkService(session,SupplierBulkSource(session,oracle),SupplierBulkProvider(settings),authorize,settings.SECRET_KEY)


def guard(service,id,user):
    row=service.row(id)
    access(user,financial=row['kind']=='payments')


def output(data,page=1):
    rows=data.pop('recipients')
    data['total']=len(rows);data['page']=page;data['pages']=max(1,(len(rows)+49)//50)
    data['recipients']=[{k:r[k] for k in ('id','target','snapshot','destination','destination_override','status','error','provider_id','accepted_at')} for r in rows[(page-1)*50:page*50]]
    data['attempts']=data['attempts'][:100]
    data['needs_resend_confirmation']=any(r['snapshot'].get('previous_send') for r in rows if r['status']=='ready')
    return jsonable_encoder(data)


def error(session,exc):
    session.rollback()
    raise HTTPException(404 if isinstance(exc,LookupError) else 409,str(exc)) from exc


@router.post(BASE+'/prever')
def preview(payload:PreviewInput,user:User,session:PG,oracle:Oracle):
    access(user,financial=payload.kind=='payments')
    try:
        ids=payload.ids
        if payload.all_filtered:
            f=payload.filters
            if any(len(str(value))>200 for value in f.values()):raise ValueError('Filtros inválidos.')
            company=int(f['company']) if f.get('company') else None
            if payload.kind=='updates':
                from app_prontocardio.routers.supplier_updates import communication_history
                scope=f.get('scope') or 'enviadas'
                if scope not in ('enviadas','todas','falhas'):raise ValueError('Filtro inválido.')
                result=communication_history(user,session,company=company,q=f.get('q',''),channel=f.get('channel',''),stage=f.get('stage',''),scope=scope,
                    start=date.fromisoformat(f['start']) if f.get('start') else None,end=date.fromisoformat(f['end']) if f.get('end') else None,page=1,all_ids=True)
            else:
                from app_prontocardio.routers.supplier_payment_notices import history
                result=history(user,session,company=company,q=f.get('q',''),state=f.get('state',''),page=1,all_ids=True)
            ids=result['selection_ids']
        return output(service(session,oracle,user).preview(payload.kind,ids,payload.channel,user.id,payload.request_key))
    except (ValueError,LookupError) as exc:error(session,exc)


@router.get(BASE+'/jobs/{id}')
def detail(id:str,user:User,session:PG,oracle:Oracle,page:int=Query(default=1,ge=1)):
    access(user)
    try:
        engine=service(session,oracle,user);guard(engine,id,user)
        return output(engine.get(id),page)
    except (ValueError,LookupError) as exc:error(session,exc)


@router.post(BASE+'/jobs/{id}/aprovar')
def approve(id:str,payload:ApproveInput,user:User,session:PG,oracle:Oracle):
    access(user)
    if not payload.confirmed:raise HTTPException(422,'Confirme a revisão dos destinatários e documentos.')
    try:
        engine=service(session,oracle,user);guard(engine,id,user)
        if engine.get(id)['counts']['blocked'] and not payload.acknowledge_blocked:
            raise ValueError('Confirme que os destinatários bloqueados permanecerão pendentes.')
        return output(engine.approve(id,payload.version,user.id,payload.confirm_resend))
    except (ValueError,LookupError) as exc:error(session,exc)


@router.post(BASE+'/jobs/{id}/destinatarios/{recipient_id}')
def change_contact(id:str,recipient_id:str,payload:ContactInput,user:User,session:PG,oracle:Oracle):
    access(user)
    try:
        engine=service(session,oracle,user);guard(engine,id,user)
        return output(engine.set_destination(id,recipient_id,payload.destination,user.id))
    except (ValueError,LookupError) as exc:error(session,exc)


@router.post(BASE+'/jobs/{id}/acao/{action}')
def action(id:str,action:Literal['atualizar','cancelar','repetir-falhas'],user:User,session:PG,oracle:Oracle):
    access(user)
    try:
        engine=service(session,oracle,user);guard(engine,id,user)
        method={'atualizar':engine.refresh,'cancelar':engine.cancel,'repetir-falhas':engine.retry_failed}[action]
        return output(method(id,user.id))
    except (ValueError,LookupError) as exc:error(session,exc)


@router.get(BASE+'/historico')
def history(user:User,session:PG,kind:Literal['updates','payments']='updates',page:int=Query(default=1,ge=1)):
    access(user,financial=kind=='payments')
    total=session.execute(select(func.count()).select_from(jobs).where(jobs.c.kind==kind)).scalar_one()
    result=[]
    for row in session.execute(select(jobs).where(jobs.c.kind==kind).order_by(jobs.c.created_at.desc()).limit(20).offset((page-1)*20)).mappings():
        counts={r[0]:r[1] for r in session.execute(select(recipients.c.status,func.count()).where(recipients.c.job_id==row['id']).group_by(recipients.c.status))}
        result.append({**dict(row),'counts':counts})
    return jsonable_encoder({'items':result,'total':total,'page':page,'pages':max(1,(total+19)//20),'kind':kind})
