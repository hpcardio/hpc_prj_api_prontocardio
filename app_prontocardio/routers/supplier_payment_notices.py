"""Separate payment confirmation and supplier notice workflow. No Oracle writes."""
from datetime import date,timedelta,datetime
from zoneinfo import ZoneInfo
from decimal import Decimal
import re
import smtplib
import ssl
from email.message import EmailMessage
from urllib.parse import quote

from fastapi import APIRouter,HTTPException,Request,Response,Query
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel,Field,ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app_prontocardio.routers.supplier_updates import access,PG,Oracle,User,settings
from app_prontocardio.database import get_session_postgres,get_session_oracle
from app_prontocardio.security import valida_token_usuario_atual
from app_prontocardio.services.supplier_updates_mv import read_position
from app_prontocardio.services.supplier_updates_store import cases,now
from app_prontocardio.services.supplier_updates_files import MAX_FILE,MAX_TOTAL,validate_attachment
from app_prontocardio.services.supplier_uploads import limited_form
from app_prontocardio.services.supplier_payment_notices import PaymentSelection,PaymentConfirmation,payment_items,select_payments,notice_pdf,notice_message
from app_prontocardio.services.supplier_payment_store import PaymentStore,confirmations,confirmed_items,payment_events,payment_links

BASE='/app_glosas/financeiro/contas-a-pagar/fornecedores-atualizacao/pagamentos'
PUBLIC='/app_glosas/financeiro/contas-a-pagar/fornecedores-atualizacao/publico/pagamentos'
router=APIRouter(tags=['confirmacao-pagamentos-fornecedores'])


def fail(session,exc,status=409):
    session.rollback()
    raise HTTPException(404 if isinstance(exc,LookupError) else status,str(exc)) from exc


def current_selection(oracle,payload):
    positions=read_position(oracle,payload.company,payload.supplier)
    result=select_payments(positions,payload.keys)
    first=result['items'][0]
    if first['company']!=payload.company or first['supplier']!=payload.supplier:
        raise ValueError('Os pagamentos não pertencem à empresa e ao fornecedor selecionados.')
    return result


def check_current(oracle,confirmation):
    payload=PaymentSelection(company=confirmation['company'],supplier=confirmation['supplier'],keys=[p['key'] for p in confirmation['items']])
    selected=current_selection(oracle,payload)
    if selected['fingerprint']!=confirmation['fingerprint']:
        raise ValueError('O pagamento foi alterado no MV. Solicite nova conferência financeira antes de comunicar.')


def packet(confirmation,link):
    url=settings.FRONTEND_BASE_URL.rstrip('/')+'/fornecedor/pagamento/#'+link['token']
    return {**link,'id':confirmation['id'],'link':url,'message':notice_message(confirmation,url)}


def delivery_state(session,id):
    events=[dict(r) for r in session.execute(select(payment_events).where(payment_events.c.confirmation_id==id)
        .order_by(payment_events.c.created_at.desc(),payment_events.c.id.desc())).mappings()]
    sent=next((e for e in events if e['kind'] in ('email_aceito_smtp','whatsapp_enviado_confirmado','whatsapp_aceito_meta')),None)
    return {'stage':'aviso_registrado' if sent else 'confirmado','last_send':sent,'events':events}


@router.get(BASE+'/itens')
def listing(user:User,session:PG,oracle:Oracle,company:int|None=None,q:str=Query(default='',max_length=200),
        start:date|None=None,end:date|None=None,state:str='pendente',page:int=Query(default=1,ge=1)):
    access(user)
    today=datetime.now(ZoneInfo('America/Sao_Paulo')).date()
    start,end=start or today.replace(day=1),end or today
    if start>end:raise HTTPException(422,'Confira o período de pagamentos.')
    positions=read_position(oracle,company,start=start,end=end)
    items=payment_items(positions,start,end)
    saved={r['key']:r['confirmation_id'] for r in session.execute(select(confirmed_items)).mappings()}
    companies={str(p['company']):p['company_name'] for p in positions}
    for item in items:item['confirmation_id']=saved.get(item['key'])
    if q:items=[p for p in items if q.casefold() in f"{p['name']} {p['document']} {p['reference']}".casefold()]
    if state=='pendente':items=[p for p in items if not p['confirmation_id']]
    if state=='confirmado':items=[p for p in items if p['confirmation_id']]
    try:access(user,financial=True);can_confirm=True
    except HTTPException:can_confirm=False
    return jsonable_encoder(dict(items=items[(page-1)*20:page*20],total=len(items),page=page,pages=max(1,(len(items)+19)//20),
        start=start,end=end,companies=companies,can_confirm=can_confirm))


@router.post(BASE+'/preparar')
def prepare(payload:PaymentSelection,user:User,session:PG,oracle:Oracle):
    access(user,financial=True)
    try:
        result=current_selection(oracle,payload)
        PaymentStore(session).ensure_new(payload.keys)
        return result
    except ValueError as exc:fail(session,exc)


@router.post(BASE+'/confirmar')
async def confirm(request:Request,user:User,session:PG,oracle:Oracle):
    access(user,financial=True)
    try:
        async with limited_form(request) as form:
            payload=PaymentConfirmation.model_validate_json(str(form.get('payload') or ''))
            if not payload.confirmed:raise HTTPException(422,'Confirme que o pagamento foi conferido no banco.')
            files,total=[],0
            for upload in form.getlist('files'):
                content=await upload.read(MAX_FILE+1)
                total+=len(content)
                if total>MAX_TOTAL:raise HTTPException(413,'Envie até 25 MB de comprovantes.')
                file=validate_attachment(upload.filename or '',content)
                if file['content_type'] not in ('application/pdf','image/png','image/jpeg'):
                    raise HTTPException(422,'O comprovante bancário deve ser PDF, PNG ou JPG.')
                files.append(file)
            if not files:raise HTTPException(422,'Anexe ao menos um comprovante bancário.')
            selected=current_selection(oracle,payload)
            if selected['fingerprint']!=payload.fingerprint:raise ValueError('O pagamento foi alterado. Reabra a conferência.')
            store=PaymentStore(session)
            confirmation=store.confirm(selected,user.id,files,payload.note)
            result=packet(confirmation,store.link(confirmation['id'],user.id))
            session.commit()
            return result
    except ValidationError as exc:
        session.rollback()
        raise HTTPException(422,'Confira os pagamentos selecionados e os campos obrigatórios.') from None
    except IntegrityError:
        session.rollback()
        raise HTTPException(409,'Um pagamento já foi confirmado por outra ação. Atualize a listagem.') from None
    except (ValueError,LookupError) as exc:fail(session,exc)


@router.get(BASE+'/confirmacoes')
def history(user:User,session:PG,company:int|None=None,q:str='',state:str='',page:int=Query(default=1,ge=1),all_ids:bool=False):
    access(user)
    result=[];companies={}
    store=PaymentStore(session)
    for row in session.execute(select(confirmations).order_by(confirmations.c.confirmed_at.desc())).mappings():
        companies[str(row['company'])]=row['company_name'] or f"Empresa {row['company']}"
        if company and row['company']!=company:continue
        if q and q.casefold() not in f"{row['name']} {row['document']}".casefold():continue
        item=store.get(row['id']);item.update(delivery_state(session,row['id']))
        item.pop('events')
        if state and state!=item['stage']:continue
        result.append(item)
    try:access(user,financial=True);can_bulk=True
    except HTTPException:can_bulk=False
    return jsonable_encoder(dict(items=result[(page-1)*20:page*20],total=len(result),page=page,pages=max(1,(len(result)+19)//20),companies=companies,can_bulk=can_bulk,selection_ids=[r['id'] for r in result] if all_ids else []))


@router.get(BASE+'/confirmacoes/{id}')
def detail(id:str,user:User,session:PG):
    access(user)
    try:
        store=PaymentStore(session)
        confirmation=store.get(id)
        contact=session.execute(select(cases.c.contact).where(cases.c.company==confirmation['company'],cases.c.supplier==confirmation['supplier'])).scalar_one_or_none() or {}
        try:access(user,financial=True);can_manage=True
        except HTTPException:can_manage=False
        return jsonable_encoder(dict(confirmation=confirmation,files=store.files(id),contact=contact,can_manage=can_manage,
            **delivery_state(session,id),active_link_ids=list(session.execute(select(payment_links.c.id).where(
                payment_links.c.confirmation_id==id,payment_links.c.revoked_at.is_(None),payment_links.c.expires_at>now())).scalars())))
    except LookupError as exc:fail(session,exc)


@router.post(BASE+'/confirmacoes/{id}/link')
def new_link(id:str,user:User,session:PG,oracle:Oracle):
    access(user,financial=True)
    try:
        store=PaymentStore(session);confirmation=store.get(id,lock=True)
        check_current(oracle,confirmation)
        result=packet(confirmation,store.link(id,user.id));session.commit()
        return result
    except (ValueError,LookupError) as exc:fail(session,exc)


@router.post(BASE+'/confirmacoes/{id}/revogar')
def revoke_link(id:str,user:User,session:PG):
    access(user,financial=True)
    try:
        PaymentStore(session).revoke(id,user.id);session.commit()
        return {'ok':True}
    except LookupError as exc:fail(session,exc)


class Token(BaseModel):token:str=Field(min_length=40,max_length=64)
class Destination(Token):destination:str=Field(min_length=5,max_length=254)
class Delivery(Token):
    channel:str=Field(pattern='^(whatsapp)$')
    result:str=Field(pattern='^(preparado|enviado_confirmado|falhou)$')
    destination:str=Field(min_length=8,max_length=30)
class Download(Token):file:str=Field(min_length=1,max_length=60)


def send_payment_email(*,destination,message,files):
    if not settings.SMTP_HOST:raise RuntimeError('SMTP não configurado.')
    email=EmailMessage()
    email['Subject']='Hospital Prontocardio — aviso e comprovante de pagamento'
    email['From']=settings.SMTP_FROM or settings.SMTP_FROM_EMAIL
    email['To']=destination
    email.set_content(message)
    for file in files:
        main,sub=file['content_type'].split('/',1)
        email.add_attachment(file['content'],maintype=main,subtype=sub,filename=file['name'])
    use_ssl=settings.SMTP_USE_SSL if settings.SMTP_USE_SSL is not None else settings.SMTP_PORT==465
    with (smtplib.SMTP_SSL if use_ssl else smtplib.SMTP)(settings.SMTP_HOST,settings.SMTP_PORT,timeout=30) as server:
        if not use_ssl and settings.SMTP_USE_TLS:server.starttls(context=ssl.create_default_context())
        username=settings.SMTP_USER or settings.SMTP_USERNAME
        if username:server.login(username,settings.SMTP_PASSWORD or '')
        if server.send_message(email):raise RuntimeError('Destinatário recusado.')


@router.post(BASE+'/confirmacoes/{id}/email')
def email_notice(id:str,payload:Destination,user:User,session:PG,oracle:Oracle):
    access(user,financial=True)
    if not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+',payload.destination):raise HTTPException(422,'Informe um e-mail válido.')
    store=PaymentStore(session)
    try:
        confirmation=store.get(id,lock=True)
        resolved=store.resolve(payload.token)
        if resolved['id']!=id:raise ValueError('O link não pertence a este pagamento.')
        check_current(oracle,confirmation)
        if session.execute(select(payment_events.c.id).where(payment_events.c.confirmation_id==id,
            payment_events.c.kind.in_(['email_preparado','email_aceito_smtp']),payment_events.c.created_at>now()-timedelta(seconds=60))).first():
            raise ValueError('Aguarde um minuto antes de tentar novo envio.')
        files=[{'name':'aviso-pagamento.pdf','content_type':'application/pdf','content':notice_pdf(confirmation)},*store.files(id,with_content=True)]
        message=packet(confirmation,{'token':payload.token})['message']
        store.event(id,user.id,'email_preparado',{'destination':payload.destination});session.commit()
    except (LookupError,ValueError) as exc:fail(session,exc)
    try:send_payment_email(destination=payload.destination,message=message,files=files)
    except Exception:
        store.event(id,user.id,'email_falhou',{'destination':payload.destination});session.commit()
        raise HTTPException(503,'O envio não foi confirmado. Confira o histórico antes de tentar novamente.') from None
    store.event(id,user.id,'email_aceito_smtp',{'destination':payload.destination,'delivery_confirmed':False});session.commit()
    return {'status':'aceito_smtp','delivery_confirmed':False}


@router.post(BASE+'/confirmacoes/{id}/comunicacoes')
def communicate(id:str,payload:Delivery,user:User,session:PG,oracle:Oracle):
    access(user,financial=True)
    try:
        store=PaymentStore(session);confirmation=store.get(id,lock=True)
        if store.resolve(payload.token)['id']!=id:raise ValueError('O link não pertence a este pagamento.')
        check_current(oracle,confirmation)
        store.event(id,user.id,'whatsapp_'+payload.result,payload.model_dump());session.commit()
        return {'ok':True}
    except (ValueError,LookupError) as exc:fail(session,exc)


def document_response(confirmation,store,file):
    if file=='aviso':content,name=notice_pdf(confirmation),'aviso-pagamento.pdf'
    else:
        matched=next((f for f in store.files(confirmation['id'],with_content=True) if f['id']==file),None)
        if not matched:raise HTTPException(404,'Documento não encontrado neste aviso.')
        content,name=bytes(matched['content']),matched['name']
    return Response(content,media_type='application/octet-stream',headers={'Content-Disposition':f"attachment; filename*=UTF-8''{quote(name,safe='')}",
        'Cache-Control':'no-store, private','X-Content-Type-Options':'nosniff'})


@router.get(BASE+'/confirmacoes/{id}/arquivos/{file}')
def staff_document(id:str,file:str,user:User,session:PG):
    access(user)
    try:
        store=PaymentStore(session)
        return document_response(store.get(id),store,file)
    except LookupError as exc:fail(session,exc)


def public_confirmation(token,session,oracle):
    try:
        store=PaymentStore(session);confirmation=store.resolve(token)
        check_current(oracle,confirmation)
        return store,confirmation
    except (LookupError,ValueError):
        session.rollback()
        raise HTTPException(410,'Aviso indisponível ou em conferência. Entre em contato com o financeiro.') from None


@router.post(PUBLIC+'/consultar')
def public_info(payload:Token,session:PG,oracle:Oracle,response:Response):
    store,confirmation=public_confirmation(payload.token,session,oracle)
    response.headers['Cache-Control']='no-store, private'
    return jsonable_encoder({k:confirmation[k] for k in ('id','company','company_name','name','document','items','total')}|{'files':store.files(confirmation['id'])})


@router.post(PUBLIC+'/arquivo')
def public_file(payload:Download,session:PG,oracle:Oracle):
    store,confirmation=public_confirmation(payload.token,session,oracle)
    return document_response(confirmation,store,payload.file)
