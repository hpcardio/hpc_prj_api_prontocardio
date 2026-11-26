"""Supplier contact campaign and financial review; MV is read-only."""
import json
import hashlib
import logging
import re
import smtplib
import ssl
from datetime import date, timedelta
from decimal import Decimal
from email.message import EmailMessage
from typing import Annotated, Literal
from urllib.parse import quote, urlsplit
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import select, update, text, bindparam
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from starlette.formparsers import MultiPartException
from starlette.exceptions import HTTPException as StarletteHTTPException

from app_prontocardio.database import get_session_oracle, get_session_postgres
from app_prontocardio.security import valida_token_usuario_atual
from app_prontocardio.settings import Settings
from app_prontocardio.services.supplier_updates import SupplierResponse, classify_difference, crosscheck_titles, money
from app_prontocardio.services.supplier_updates_files import MAX_FILE, MAX_TOTAL, validate_attachment
from app_prontocardio.services.supplier_updates_mv import read_position
from app_prontocardio.services.supplier_updates_store import SupplierStore, cases, events, submissions, attachments, reviews, invites, now

router = APIRouter(tags=['atualizacao-fornecedores'])
BASE = '/app_glosas/financeiro/contas-a-pagar/fornecedores-atualizacao'
PUBLIC = BASE + '/publico'
settings = Settings()
PG = Annotated[Session, Depends(get_session_postgres)]
Oracle = Annotated[Session, Depends(get_session_oracle)]
User = Annotated[object, Depends(valida_token_usuario_atual)]

MESSAGE = '''Prezado fornecedor,

O Hospital Prontocardio está realizando um processo de atualização cadastral e conciliação financeira, com o objetivo de aprimorar seus controles internos e fortalecer o relacionamento com seus parceiros comerciais.

Solicitamos a atualização das seguintes informações:

- Razão social e CNPJ;
- Responsável pelo relacionamento financeiro;
- Telefone, WhatsApp e e-mail;
- Demonstrativo de títulos em aberto, vencidos e a vencer;
- Notas fiscais vinculadas aos títulos;
- Acordos financeiros vigentes e respectivas parcelas;
- Eventuais divergências financeiras identificadas.

Para facilitar esse processo, disponibilizamos um formulário eletrônico para atualização e envio das informações:

{link}

As informações encaminhadas serão submetidas à conferência e validação pelo nosso departamento financeiro.

Agradecemos pela colaboração e pela parceria.

Atenciosamente,

Departamento Financeiro
Hospital Prontocardio'''


def access(user, financial=False):
    screens = set(user.telas_permitidas or [])
    if user.perfil in {'ti', 'administrador'}:
        return
    if financial and 'contas_pagar_nfse_regras' in screens:
        return
    if not financial and screens.intersection({'contas_pagar_gestao', 'contas_pagar_nfse_regras'}):
        return
    raise HTTPException(403, 'Acesso restrito ao financeiro.' if financial else 'Sem acesso à atualização de fornecedores.')


def failure(session, exc, status=422):
    session.rollback()
    raise HTTPException(404 if isinstance(exc, LookupError) else status, str(exc)) from exc


def position_for(oracle, case):
    positions = read_position(oracle, case['company'], case['supplier'])
    if not positions:
        raise HTTPException(409, 'Fornecedor não localizado na posição atual do MV. Solicite conferência.')
    return dict(positions[0])


def manual_payments(session, position):
    if not position['titles']:
        return []
    return [dict(r) for r in session.execute(text('''SELECT id, codigo_parcela, data_pagamento, valor_pago, observacao
        FROM api_prontocardio.contas_pagar_pagamentos WHERE codigo_parcela IN :ids ORDER BY id''')
        .bindparams(bindparam('ids', expanding=True)), {'ids': [int(t['parcel']) for t in position['titles']]}).mappings()]


def include_manual_fingerprint(position, payments):
    if position['titles']:
        raw = position['fingerprint'] + json.dumps(payments, default=str, sort_keys=True)
        position['fingerprint'] = hashlib.sha256(raw.encode()).hexdigest()


def invitation_message(token):
    root = settings.FRONTEND_BASE_URL.rstrip('/')
    parsed = urlsplit(root)
    if parsed.scheme not in ('http', 'https') or not parsed.netloc:
        raise HTTPException(503, 'Endereço público do Receita Certa não configurado.')
    # Fragments stay in the browser and are not sent to reverse-proxy/access logs.
    link = f'{root}/fornecedor/atualizacao/#{token}'
    return {'link': link, 'message': MESSAGE.format(link=link)}


class CaseInput(BaseModel):
    company: int = Field(gt=0)
    supplier: str = Field(min_length=1, max_length=100)


class ContactInput(BaseModel):
    contact: str = Field(default='', max_length=150)
    email: str = Field(default='', max_length=254)
    phone: str = Field(default='', max_length=30)
    whatsapp: str = Field(default='', max_length=30)
    responsible: str = Field(default='', max_length=150)
    next_action: str = Field(default='', max_length=2000)
    next_date: date | None = None
    version: int


class Communication(BaseModel):
    channel: Literal['email', 'whatsapp', 'telefone']
    result: Literal['preparado', 'enviado_confirmado', 'contato_realizado', 'falhou']
    destination: str = Field(min_length=3, max_length=254)
    notes: str = Field(default='', max_length=2000)


class TokenInput(BaseModel):
    token: str = Field(min_length=40, max_length=64)


class EmailInput(TokenInput):
    destination: str = Field(min_length=5, max_length=254)


class ReviewInput(BaseModel):
    decision: Literal['aprovar', 'divergencia', 'solicitar_correcao', 'conferir']
    balance: Decimal | None = Field(default=None, ge=0, max_digits=15, decimal_places=2)
    reason: str = Field(min_length=15, max_length=5000)
    version: int = Field(gt=0)
    fingerprint: str = Field(min_length=1, max_length=64)
    confirm: bool


@router.get(BASE + '/comunicacoes')
def communication_history(user: User, session: PG, company: int | None = None,
        q: str = Query(default='', max_length=200), channel: str = '', stage: str = '',
        scope: Literal['enviadas','todas','falhas'] = 'enviadas', start: date | None = None,
        end: date | None = None, page: int = Query(default=1, ge=1),all_ids:bool=False):
    access(user)
    if start and end and start > end:
        raise HTTPException(422,'Confira o período de envio.')
    sent_kinds = {'email_aceito_smtp','email_enviado_confirmado','whatsapp_enviado_confirmado','whatsapp_aceito_meta',
                  'telefone_enviado_confirmado','telefone_contato_realizado'}
    all_events = {}
    for row in session.execute(select(events).order_by(events.c.created_at.desc(),events.c.id.desc())).mappings():
        kind=row['kind']
        if kind not in sent_kinds and not kind.endswith(('_preparado','_falhou')):
            continue
        when=row['created_at'].replace(tzinfo=ZoneInfo('UTC')) if row['created_at'].tzinfo is None else row['created_at']
        day=when.astimezone(ZoneInfo('America/Sao_Paulo')).date()
        if (start and day < start) or (end and day > end) or (channel and not kind.startswith(channel+'_')):
            continue
        all_events.setdefault(row['case_id'],[]).append(dict(row))
    responses={}
    for row in session.execute(select(submissions.c.case_id,submissions.c.created_at).order_by(submissions.c.created_at.desc())).mappings():
        responses.setdefault(row['case_id'],row['created_at'])
    result=[]
    companies={}
    for row in session.execute(select(cases)).mappings():
        companies[str(row['company'])]=f"Empresa {row['company']}"
        if company and row['company'] != company:
            continue
        if q and q.casefold() not in f"{row['name']} {row['document']}".casefold():
            continue
        if stage and row['stage'] != stage:
            continue
        history=all_events.get(row['id'],[])
        sends=[e for e in history if e['kind'] in sent_kinds]
        failures=[e for e in history if e['kind'].endswith('_falhou')]
        if (scope=='enviadas' and not sends) or (scope=='falhas' and not failures):
            continue
        latest=(history or [None])[0]
        result.append(dict(id=row['id'],company=row['company'],name=row['name'],document=row['document'],
            contact=row['contact'],stage=row['stage'],sent_count=len(sends),last_sent_at=sends[0]['created_at'] if sends else None,
            last_attempt_at=latest['created_at'] if latest else None,
            last_channel=latest['kind'].split('_')[0] if latest else None,last_result=latest['kind'] if latest else None,
            destination=latest['details'].get('destination','') if latest else '',
            has_response=row['id'] in responses,response_received_at=responses.get(row['id'])))
    result.sort(key=lambda r: str(r['last_sent_at'] or ''),reverse=True)
    total=len(result)
    return jsonable_encoder(dict(items=result[(page-1)*20:page*20],total=total,page=page,pages=max(1,(total+19)//20),
        companies=companies,selection_ids=[str(r['id']) for r in result] if all_ids else [],summary={'suppliers':total,'sends':sum(r['sent_count'] for r in result),
            'answered':sum(r['has_response'] for r in result),'waiting':sum(not r['has_response'] for r in result)}))


@router.get(BASE)
def listing(user: User, session: PG, oracle: Oracle, company: int | None = None,
            q: str = Query(default='', max_length=200), stage: str = '', payment: str = '', responsible: str = '',
            start: date | None = None, end: date | None = None, page: int = Query(default=1, ge=1),
            page_size: int = Query(default=20, ge=1, le=100)):
    access(user)
    start = start or date(date.today().year, 1, 1)
    end = end or date.today()
    if start > end:
        raise HTTPException(422, 'A data inicial deve ser anterior à final.')
    stored = [dict(r) for r in session.execute(select(cases)).mappings()]
    last_contacts = {}
    for event in session.execute(select(events).where(events.c.kind.in_([
            'whatsapp_enviado_confirmado', 'email_enviado_confirmado', 'telefone_enviado_confirmado',
            'telefone_contato_realizado', 'email_aceito_smtp'])).order_by(events.c.created_at.desc())).mappings():
        last_contacts.setdefault(event['case_id'], dict(event))
    for case in stored:
        case['last_communication'] = last_contacts.get(case['id'])
    by_key = {(r['company'], r['supplier']): r for r in stored}
    positions = read_position(oracle, company, start=start, end=end)
    companies = {}
    found = set()
    rows = []
    for position in positions:
        key = (position['company'], position['supplier'])
        found.add(key)
        companies[position['company']] = position['company_name']
        case = by_key.get(key)
        if not case and money(position['system_balance']) == 0 and money(position['paid_period']) == 0:
            continue
        rows.append({**{k: v for k, v in position.items() if k not in ('titles', 'fingerprint')},
                     'case': case, 'stage': case['stage'] if case else 'a_comunicar'})
    for case in stored:
        if (company is None or company == case['company']) and (case['company'], case['supplier']) not in found:
            rows.append(dict(company=case['company'], company_name=f"Empresa {case['company']}", supplier=case['supplier'],
                name=case['name'], document=case['document'], system_balance=None, paid_period=None, last_payment=None,
                case=case, stage=case['stage']))
    if q:
        rows = [r for r in rows if q.casefold() in f"{r['name']} {r['document']} {r['supplier']}".casefold()]
    if responsible:
        rows = [r for r in rows if responsible.casefold() in (r['case'] or {}).get('responsible', '').casefold()]
    counts = {key: sum(r['stage'] == key for r in rows) for key in ('a_comunicar', 'aguardando', 'respondido', 'em_conferencia', 'divergencia', 'aguardando_correcao', 'validado')}
    if stage:
        rows = [r for r in rows if r['stage'] == stage]
    if payment == 'pagos':
        rows = [r for r in rows if r['paid_period'] is not None and money(r['paid_period']) > 0]
    elif payment == 'saldo':
        rows = [r for r in rows if r['system_balance'] is not None and money(r['system_balance']) > 0]
    total = len(rows)
    return jsonable_encoder(dict(items=rows[(page-1)*page_size:page*page_size], total=total, page=page,
        pages=max(1, (total+page_size-1)//page_size), counts=counts, companies=companies, start=start, end=end))


@router.post(BASE)
def create_case(payload: CaseInput, user: User, session: PG, oracle: Oracle):
    access(user)
    positions = read_position(oracle, payload.company, payload.supplier)
    if not positions:
        raise HTTPException(404, 'Fornecedor não localizado nesta empresa.')
    try:
        result = SupplierStore(session).create_case(positions[0], user.id)
        session.commit()
        return jsonable_encoder(result)
    except IntegrityError:
        session.rollback()
        return jsonable_encoder(dict(session.execute(select(cases).where(cases.c.company == payload.company, cases.c.supplier == payload.supplier)).mappings().one()))


@router.get(BASE + '/{case_id}')
def detail(case_id: int, user: User, session: PG, oracle: Oracle):
    access(user)
    store = SupplierStore(session)
    try:
        case = store.case(case_id)
    except LookupError as exc:
        failure(session, exc)
    position = position_for(oracle, case)
    response_rows = [dict(r) for r in session.execute(select(submissions).where(submissions.c.case_id == case_id).order_by(submissions.c.created_at.desc())).mappings()]
    file_rows = [dict(r) for r in session.execute(select(attachments.c.id, attachments.c.name, attachments.c.submission_id)
                .join(submissions).where(submissions.c.case_id == case_id)).mappings()]
    manual = manual_payments(session, position)
    include_manual_fingerprint(position, manual)
    comparison = classify_difference(position['system_balance'], SupplierResponse.model_validate(response_rows[0]['payload'])) if response_rows else None
    try:
        access(user, financial=True)
        can_review = True
    except HTTPException:
        can_review = False
    return jsonable_encoder(dict(case=case, position=position, responses=response_rows, attachments=file_rows, comparison=comparison,
        events=[dict(r) for r in session.execute(select(events).where(events.c.case_id == case_id).order_by(events.c.created_at.desc())).mappings()],
        reviews=[dict(r) for r in session.execute(select(reviews).where(reviews.c.case_id == case_id).order_by(reviews.c.created_at.desc())).mappings()],
        manual_payments=manual, can_review=can_review, as_of=now(), active_invite_ids=list(session.execute(select(invites.c.id).where(
            invites.c.case_id == case_id, invites.c.used_at.is_(None), invites.c.revoked_at.is_(None), invites.c.expires_at > now())).scalars()),
        crosscheck=crosscheck_titles(position, SupplierResponse.model_validate(response_rows[0]['payload'])) if response_rows else []))


@router.post(BASE + '/{case_id}/cadastro')
def contact(case_id: int, payload: ContactInput, user: User, session: PG):
    access(user)
    store = SupplierStore(session)
    try:
        case = store.case(case_id, lock=True)
        if case['version'] != payload.version:
            raise ValueError('Cadastro atualizado. Reabra a tela antes de salvar.')
        values = payload.model_dump(mode='json')
        contact_values = {k: values.pop(k) for k in ('contact', 'email', 'phone', 'whatsapp')}
        values['version'] += 1
        session.execute(update(cases).where(cases.c.id == case_id).values(contact=contact_values, **values))
        store.event(case_id, user.id, 'cadastro_atualizado', {'contact': contact_values, **values})
        session.commit()
        return {'ok': True}
    except (LookupError, ValueError) as exc:
        failure(session, exc, 409)


@router.get(BASE+'/{case_id}/composicao')
@router.get(BASE+'/{case_id}/resposta')
def saved_supplier_data(case_id:int,user:User,session:PG):
    access(user)
    try:
        case=SupplierStore(session).case(case_id)
        response_rows=[dict(r) for r in session.execute(select(submissions).where(submissions.c.case_id==case_id)
            .order_by(submissions.c.created_at.desc())).mappings()]
        file_rows=[dict(r) for r in session.execute(select(attachments.c.id,attachments.c.name,attachments.c.submission_id)
            .join(submissions).where(submissions.c.case_id==case_id)).mappings()]
        return jsonable_encoder(dict(case=case,responses=response_rows,attachments=file_rows,
            active_invite_ids=list(session.execute(select(invites.c.id).where(invites.c.case_id==case_id,
                invites.c.revoked_at.is_(None),invites.c.used_at.is_(None),invites.c.expires_at>now())).scalars())))
    except LookupError as exc:failure(session,exc)


@router.post(BASE + '/{case_id}/convites')
def invite(case_id: int, user: User, session: PG):
    access(user)
    store = SupplierStore(session)
    try:
        result = store.invite(case_id, user.id)
        result.update(invitation_message(result['token']))
        session.commit()
        return result
    except (LookupError, ValueError) as exc:
        failure(session, exc)


@router.post(BASE + '/{case_id}/revogar')
def revoke(case_id: int, user: User, session: PG):
    access(user)
    try:
        SupplierStore(session).revoke(case_id, user.id)
        session.commit()
        return {'ok': True}
    except LookupError as exc:
        failure(session, exc)


@router.post(BASE + '/{case_id}/comunicacoes')
def communication(case_id: int, payload: Communication, user: User, session: PG):
    access(user)
    store = SupplierStore(session)
    try:
        case = store.case(case_id, lock=True)
        store.event(case_id, user.id, f'{payload.channel}_{payload.result}', payload.model_dump())
        if payload.result in ('enviado_confirmado', 'contato_realizado') and case['stage'] == 'a_comunicar':
            session.execute(update(cases).where(cases.c.id == case_id).values(stage='aguardando', version=cases.c.version + 1))
        session.commit()
        return {'ok': True}
    except LookupError as exc:
        failure(session, exc)


def send_invitation_email(destination, message):
    if not settings.SMTP_HOST:
        raise RuntimeError('SMTP não configurado.')
    email = EmailMessage()
    email['Subject'] = 'Hospital Prontocardio — atualização cadastral e conciliação financeira'
    email['From'] = settings.SMTP_FROM or settings.SMTP_FROM_EMAIL
    email['To'] = destination
    email.set_content(message)
    use_ssl = settings.SMTP_USE_SSL if settings.SMTP_USE_SSL is not None else settings.SMTP_PORT == 465
    smtp_cls = smtplib.SMTP_SSL if use_ssl else smtplib.SMTP
    with smtp_cls(settings.SMTP_HOST, settings.SMTP_PORT, timeout=20) as server:
        if not use_ssl and settings.SMTP_USE_TLS:
            server.starttls(context=ssl.create_default_context())
        username = settings.SMTP_USER or settings.SMTP_USERNAME
        if username:
            server.login(username, settings.SMTP_PASSWORD or '')
        if server.send_message(email):
            raise RuntimeError('Destinatário recusado.')


@router.post(BASE + '/{case_id}/email')
def send_email(case_id: int, payload: EmailInput, user: User, session: PG):
    access(user)
    if not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', payload.destination):
        raise HTTPException(422, 'Informe um e-mail válido.')
    store = SupplierStore(session)
    try:
        case = store.case(case_id, lock=True)
        invitation = store.resolve(payload.token)
        if invitation['case_id'] != case_id:
            raise ValueError('O convite não pertence a este fornecedor.')
        recent = session.execute(select(events.c.id).where(events.c.case_id == case_id,
            events.c.kind.in_(['email_preparado', 'email_aceito_smtp']), events.c.created_at > now() - timedelta(seconds=60))).first()
        if recent:
            raise ValueError('Aguarde um minuto antes de tentar novo envio.')
        store.event(case_id, user.id, 'email_preparado', {'destination': payload.destination})
        session.commit()
    except (ValueError, LookupError) as exc:
        failure(session, exc)
    try:
        send_invitation_email(payload.destination, invitation_message(payload.token)['message'])
    except Exception:
        store.event(case_id, user.id, 'email_falhou', {'destination': payload.destination})
        session.commit()
        raise HTTPException(503, 'O envio não foi confirmado. Confira o histórico antes de tentar novamente.') from None
    case = store.case(case_id, lock=True)
    store.event(case_id, user.id, 'email_aceito_smtp', {'destination': payload.destination, 'delivery_confirmed': False})
    if case['stage'] == 'a_comunicar':
        session.execute(update(cases).where(cases.c.id == case_id).values(stage='aguardando', version=cases.c.version + 1))
    session.commit()
    return {'status': 'aceito_smtp', 'delivery_confirmed': False}


@router.post(BASE + '/{case_id}/revisoes')
def review(case_id: int, payload: ReviewInput, user: User, session: PG, oracle: Oracle):
    access(user, financial=True)
    if not payload.confirm:
        raise HTTPException(422, 'Confirme a conferência dos documentos e divergências.')
    if payload.decision == 'aprovar' and payload.balance is None:
        raise HTTPException(422, 'Informe explicitamente o saldo validado, inclusive quando for zero.')
    store = SupplierStore(session)
    try:
        case = store.case(case_id)
        position = position_for(oracle, case)
        include_manual_fingerprint(position, manual_payments(session, position))
        result = store.review(case_id, user.id, payload.decision, payload.balance, payload.reason, payload.version,
                              payload.fingerprint, position['fingerprint'], position)
        session.commit()
        return result
    except (LookupError, ValueError) as exc:
        failure(session, exc, 409)


@router.get(BASE + '/{case_id}/anexos/{attachment_id}')
def download(case_id: int, attachment_id: str, user: User, session: PG):
    access(user)
    row = session.execute(select(attachments).join(submissions).where(attachments.c.id == attachment_id, submissions.c.case_id == case_id)).mappings().first()
    if not row:
        raise HTTPException(404, 'Arquivo não encontrado.')
    return Response(bytes(row['content']), media_type='application/octet-stream', headers={
        'Content-Disposition': f"attachment; filename*=UTF-8''{quote(row['name'], safe='')}",
        'Cache-Control': 'no-store, private', 'X-Content-Type-Options': 'nosniff'})


@router.post(PUBLIC + '/consultar')
def public_info(payload: TokenInput, session: PG, response: Response):
    response.headers.update({'Cache-Control': 'no-store, private', 'Referrer-Policy': 'no-referrer'})
    try:
        store = SupplierStore(session)
        invitation = store.resolve(payload.token)
        case = store.case(invitation['case_id'])
        return {'name': case['name'], 'document': case['document'], 'company': case['company'], 'expires_at': invitation['expires_at']}
    except (LookupError, ValueError) as exc:
        failure(session, exc, 410)


@router.post(PUBLIC + '/respostas')
async def public_submit(request: Request, session: PG, response: Response):
    response.headers.update({'Cache-Control': 'no-store, private', 'Referrer-Policy': 'no-referrer'})
    store = SupplierStore(session)
    token_header = request.headers.get('X-Supplier-Invite', '')
    try:
        store.resolve(token_header)
    except ValueError as exc:
        failure(session, exc, 410)
    try:
        content_length = int(request.headers.get('content-length', '0'))
    except ValueError:
        raise HTTPException(400, 'Tamanho da requisição inválido.') from None
    limit = MAX_TOTAL + 1024 * 1024
    if content_length < 0 or content_length > limit:
        raise HTTPException(413, 'Envie no máximo 25 MB de anexos por resposta.')
    receive, consumed = request._receive, 0
    async def bounded_receive():
        nonlocal consumed
        message = await receive()
        consumed += len(message.get('body', b''))
        if consumed > limit:
            # MultiPartException makes Starlette close spooled temporary files.
            raise MultiPartException('supplier-upload-too-large')
        return message
    request._receive = bounded_receive
    try:
        async with request.form(max_files=10, max_fields=2, max_part_size=1024*1024) as form:
            token = str(form.get('token') or '')
            if token != token_header:
                raise ValueError('Convite inválido para este envio.')
            store.resolve(token)
            raw = str(form.get('payload') or '')
            if len(raw) > 512 * 1024:
                raise ValueError('Resposta muito extensa. Envie um demonstrativo anexo.')
            payload = SupplierResponse.model_validate_json(raw)
            files, total = [], 0
            for upload in form.getlist('files'):
                content = await upload.read(MAX_FILE + 1)
                total += len(content)
                if total > MAX_TOTAL:
                    raise ValueError('Os anexos devem somar no máximo 25 MB.')
                files.append(validate_attachment(upload.filename or '', content))
            result = store.submit(token, payload, files)
            session.commit()
            return result
    except ValidationError as exc:
        session.rollback()
        details = '; '.join(str(e['msg']).removeprefix('Value error, ') for e in exc.errors()[:4])
        raise HTTPException(422, details) from None
    except (LookupError, ValueError) as exc:
        failure(session, exc, 410 if 'Convite' in str(exc) or 'convite' in str(exc) else 422)
    except StarletteHTTPException as exc:
        session.rollback()
        if exc.detail == 'supplier-upload-too-large':
            raise HTTPException(413, 'Envie no máximo 25 MB de anexos por resposta.') from None
        raise
    finally:
        request._receive = receive
