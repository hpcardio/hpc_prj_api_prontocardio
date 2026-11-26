"""Additive reconciliation records. No dependency on, or writes to, Oracle."""
import hashlib
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import (MetaData, Table, Column, Integer, String, Text, DateTime, JSON,
                        LargeBinary, Numeric, ForeignKey, UniqueConstraint, select, update)

metadata = MetaData(schema='api_prontocardio')
cases = Table('supplier_update_cases', metadata,
    Column('id', Integer, primary_key=True), Column('company', Integer, nullable=False),
    Column('supplier', String(100), nullable=False), Column('name', String(200), nullable=False),
    Column('document', String(20), nullable=False, default=''), Column('contact', JSON, nullable=False, default=dict),
    Column('stage', String(30), nullable=False, default='a_comunicar'),
    Column('responsible', String(150), nullable=False, default=''), Column('next_action', Text, nullable=False, default=''),
    Column('next_date', String(10)), Column('version', Integer, nullable=False, default=1),
    Column('validated_balance', Numeric(15, 2)), Column('created_by', Integer, nullable=False),
    Column('created_at', DateTime(timezone=True), nullable=False),
    UniqueConstraint('company', 'supplier', name='uq_supplier_update_company_supplier'))
invites = Table('supplier_update_invites', metadata,
    Column('id', String(36), primary_key=True), Column('case_id', ForeignKey(cases.c.id), nullable=False, index=True),
    Column('token_hash', String(64), nullable=False, unique=True), Column('expires_at', DateTime(timezone=True), nullable=False),
    Column('revoked_at', DateTime(timezone=True)), Column('used_at', DateTime(timezone=True)),
    Column('created_by', Integer, nullable=False), Column('created_at', DateTime(timezone=True), nullable=False))
events = Table('supplier_update_events', metadata,
    Column('id', Integer, primary_key=True), Column('case_id', ForeignKey(cases.c.id), nullable=False, index=True),
    Column('actor', Integer, nullable=False), Column('kind', String(40), nullable=False),
    Column('details', JSON, nullable=False), Column('created_at', DateTime(timezone=True), nullable=False))
submissions = Table('supplier_update_submissions', metadata,
    Column('id', String(36), primary_key=True), Column('case_id', ForeignKey(cases.c.id), nullable=False, index=True),
    Column('invite_id', ForeignKey(invites.c.id), nullable=False, unique=True), Column('payload', JSON, nullable=False),
    Column('declared_balance', Numeric(15, 2), nullable=False), Column('created_at', DateTime(timezone=True), nullable=False))
attachments = Table('supplier_update_attachments', metadata,
    Column('id', String(36), primary_key=True), Column('submission_id', ForeignKey(submissions.c.id), nullable=False, index=True),
    Column('name', String(200), nullable=False), Column('content_type', String(100), nullable=False),
    Column('sha256', String(64), nullable=False), Column('content', LargeBinary, nullable=False))
reviews = Table('supplier_update_reviews', metadata,
    Column('id', Integer, primary_key=True), Column('case_id', ForeignKey(cases.c.id), nullable=False, index=True),
    Column('submission_id', ForeignKey(submissions.c.id), nullable=False), Column('actor', Integer, nullable=False),
    Column('decision', String(25), nullable=False), Column('balance', Numeric(15, 2)), Column('reason', Text, nullable=False),
    Column('as_of', String(10), nullable=False), Column('fingerprint', String(64), nullable=False),
    Column('system_position', JSON, nullable=False), Column('created_at', DateTime(timezone=True), nullable=False))


def now():
    return datetime.now(timezone.utc)


def aware(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


class SupplierStore:
    def __init__(self, session):
        self.session = session

    def case(self, case_id, lock=False):
        query = select(cases).where(cases.c.id == case_id)
        row = self.session.execute(query.with_for_update() if lock else query).mappings().first()
        if not row:
            raise LookupError('Acompanhamento não encontrado.')
        return dict(row)

    def event(self, case_id, actor, kind, details):
        self.session.execute(events.insert().values(case_id=case_id, actor=actor, kind=kind, details=details, created_at=now()))

    def create_case(self, supplier, actor):
        existing = self.session.execute(select(cases).where(cases.c.company == supplier['company'], cases.c.supplier == supplier['supplier'])).mappings().first()
        if existing:
            return dict(existing)
        result = self.session.execute(cases.insert().values(company=supplier['company'], supplier=supplier['supplier'],
            name=supplier['name'], document=supplier.get('document') or '', contact={}, created_by=actor, created_at=now()))
        case_id = result.inserted_primary_key[0]
        self.event(case_id, actor, 'criado', {})
        return self.case(case_id)

    def invite(self, case_id, actor, token=None):
        case = self.case(case_id, lock=True)
        self.revoke(case_id, actor, audit=False)
        token = token or secrets.token_urlsafe(32)
        invitation_id = str(uuid.uuid4())
        expires = now() + timedelta(days=14)
        self.session.execute(invites.insert().values(id=invitation_id, case_id=case_id,
            token_hash=hashlib.sha256(token.encode()).hexdigest(), expires_at=expires, created_by=actor, created_at=now()))
        self.event(case_id, actor, 'convite_criado', {'expires_at': expires.isoformat()})
        if case['stage'] in ('validado', 'em_conferencia', 'respondido'):
            self.session.execute(update(cases).where(cases.c.id == case_id).values(stage='a_comunicar', version=cases.c.version + 1))
        return {'id': invitation_id, 'token': token, 'expires_at': expires.isoformat()}

    def revoke(self, case_id, actor, audit=True):
        self.case(case_id, lock=True)
        self.session.execute(update(invites).where(invites.c.case_id == case_id, invites.c.revoked_at.is_(None), invites.c.used_at.is_(None)).values(revoked_at=now()))
        if audit:
            self.event(case_id, actor, 'convite_revogado', {})

    def resolve(self, token, lock=False):
        if not 40 <= len(token) <= 64:
            raise ValueError('Convite indisponível. Solicite um novo link ao financeiro.')
        query = select(invites).where(invites.c.token_hash == hashlib.sha256(token.encode()).hexdigest())
        invite = self.session.execute(query.with_for_update() if lock else query).mappings().first()
        if not invite or invite['revoked_at'] or invite['used_at'] or aware(invite['expires_at']) <= now():
            raise ValueError('Convite indisponível. Solicite um novo link ao financeiro.')
        return dict(invite)

    def submit(self, token, payload, files):
        invitation = self.resolve(token, lock=True)
        case = self.case(invitation['case_id'], lock=True)
        if case['document'] and ''.join(filter(str.isdigit, case['document'])) != payload.document:
            raise ValueError('O CPF/CNPJ informado difere do convite. Solicite conferência ao financeiro.')
        if payload.statement_only and not files:
            raise ValueError('Anexe o demonstrativo financeiro informado.')
        used = self.session.execute(update(invites).where(invites.c.id == invitation['id'], invites.c.used_at.is_(None),
            invites.c.revoked_at.is_(None)).values(used_at=now()))
        if used.rowcount != 1:
            raise ValueError('Este convite já foi utilizado.')
        protocol = str(uuid.uuid4())
        self.session.execute(submissions.insert().values(id=protocol, case_id=case['id'], invite_id=invitation['id'],
            payload=payload.model_dump(mode='json'), declared_balance=payload.declared_balance(), created_at=now()))
        for item in files:
            self.session.execute(attachments.insert().values(id=str(uuid.uuid4()), submission_id=protocol,
                name=item['name'], content_type=item['content_type'], sha256=hashlib.sha256(item['content']).hexdigest(), content=item['content']))
        self.session.execute(update(cases).where(cases.c.id == case['id']).values(stage='respondido', version=cases.c.version + 1))
        self.event(case['id'], 0, 'resposta_recebida', {'protocol': protocol})
        return {'protocol': protocol, 'status': 'aguardando_validacao'}

    def review(self, case_id, actor, decision, balance, reason, version, fingerprint, current_fingerprint, position=None):
        case = self.case(case_id, lock=True)
        if fingerprint != current_fingerprint:
            raise ValueError('A posição do sistema foi atualizada. Reabra a conferência.')
        if version != case['version']:
            raise ValueError('A conferência foi atualizada por outra ação. Reabra a tela.')
        response = self.session.execute(select(submissions).where(submissions.c.case_id == case_id).order_by(submissions.c.created_at.desc())).mappings().first()
        if not response:
            raise ValueError('Aguarde uma resposta do fornecedor.')
        previous_approval = self.session.execute(select(reviews.c.id).where(reviews.c.submission_id == response['id'], reviews.c.decision == 'aprovar')).first()
        if previous_approval:
            raise ValueError('Esta resposta já possui validação. Solicite nova resposta para revisar a posição.')
        stage = {'aprovar': 'validado', 'divergencia': 'divergencia', 'solicitar_correcao': 'aguardando_correcao', 'conferir': 'em_conferencia'}[decision]
        values = {'stage': stage, 'version': case['version'] + 1}
        if decision == 'aprovar':
            values['validated_balance'] = Decimal(balance)
        self.session.execute(update(cases).where(cases.c.id == case_id, cases.c.version == version).values(**values))
        self.session.execute(reviews.insert().values(case_id=case_id, submission_id=response['id'], actor=actor,
            decision=decision, balance=Decimal(balance) if decision == 'aprovar' else None, reason=reason,
            as_of=response['payload']['as_of'], fingerprint=fingerprint, system_position=position or {}, created_at=now()))
        self.event(case_id, actor, 'revisao_' + decision, {'protocol': response['id'], 'reason': reason})
        return {'stage': stage, 'version': case['version'] + 1}
