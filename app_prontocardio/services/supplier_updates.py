"""Supplier declarations are evidence, never instructions to change MV."""
import hashlib
import json
import re
from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

Money = Decimal


def money(value):
    return Decimal(str(value or 0)).quantize(Decimal('0.01'))


def date_value(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date) or value is None:
        return value
    return date.fromisoformat(str(value)[:10])


class Title(BaseModel):
    reference: str = Field(min_length=1, max_length=120)
    invoice: str = Field(default='', max_length=120)
    issued: date | None = None
    due: date
    amount: Decimal = Field(ge=0, max_digits=15, decimal_places=2)
    balance: Decimal = Field(ge=0, max_digits=15, decimal_places=2)


class Installment(BaseModel):
    reference: str = Field(min_length=1, max_length=120)
    due: date
    amount: Decimal = Field(ge=0, max_digits=15, decimal_places=2)


class Agreement(BaseModel):
    reference: str = Field(min_length=1, max_length=120)
    replaces: list[str] = Field(default_factory=list, max_length=1000)
    balance: Decimal = Field(ge=0, max_digits=15, decimal_places=2)
    installments: list[Installment] = Field(default_factory=list, max_length=240)
    notes: str = Field(default='', max_length=3000)

    @model_validator(mode='after')
    def validate_installments(self):
        if self.installments and sum((p.amount for p in self.installments), Decimal(0)) != self.balance:
            raise ValueError('O saldo do acordo deve corresponder às parcelas informadas.')
        return self


class SupplierResponse(BaseModel):
    legal_name: str = Field(min_length=2, max_length=200)
    document: str = Field(min_length=11, max_length=20)
    contact: str = Field(min_length=2, max_length=150)
    email: str = Field(min_length=5, max_length=254)
    phone: str = Field(min_length=8, max_length=30)
    whatsapp: str = Field(default='', max_length=30)
    as_of: date
    no_debt: bool = False
    titles: list[Title] = Field(default_factory=list, max_length=1000)
    agreements: list[Agreement] = Field(default_factory=list, max_length=100)
    statement_only: bool = False
    statement_balance: Decimal | None = Field(default=None, ge=0, max_digits=15, decimal_places=2)
    discrepancies: str = Field(default='', max_length=10000)
    confirmed: bool

    @field_validator('document')
    @classmethod
    def clean_document(cls, value):
        value = re.sub(r'\D', '', value)
        if len(value) not in (11, 14):
            raise ValueError('Informe CPF ou CNPJ com todos os dígitos.')
        return value

    @field_validator('email')
    @classmethod
    def clean_email(cls, value):
        value = value.strip()
        if not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', value):
            raise ValueError('Informe um e-mail válido.')
        return value

    @model_validator(mode='after')
    def validate_position(self):
        if not self.confirmed:
            raise ValueError('Confirme os dados antes de enviar.')
        if self.as_of > date.today():
            raise ValueError('A data-base não pode ser futura.')
        if self.no_debt and (self.titles or self.agreements or self.statement_only or self.statement_balance):
            raise ValueError('A declaração sem pendências não pode conter saldos ou títulos.')
        if not self.no_debt and not (self.titles or self.agreements or self.statement_only):
            raise ValueError('Informe os títulos/acordos ou envie um demonstrativo.')
        if self.statement_only and (self.statement_balance is None or self.titles or self.agreements):
            raise ValueError('Para demonstrativo anexo, informe apenas o saldo total declarado.')
        refs = [t.reference for t in self.titles]
        if len(set(refs)) != len(refs):
            raise ValueError('Use referências diferentes para cada título/parcela.')
        replaced = [r for a in self.agreements for r in a.replaces]
        if len(set(replaced)) != len(replaced):
            raise ValueError('Um título não pode ser substituído por mais de um acordo.')
        if any(len(r) > 120 for r in replaced):
            raise ValueError('Referência do título muito longa.')
        return self

    def declared_balance(self):
        if self.no_debt:
            return money(0)
        if self.statement_only:
            return money(self.statement_balance)
        replaced = {r for a in self.agreements for r in a.replaces}
        return money(sum((t.balance for t in self.titles if t.reference not in replaced), Decimal(0))
                     + sum((a.balance for a in self.agreements), Decimal(0)))


def classify_difference(system_balance, response):
    # Same aggregate is only a review aid, not a title-level reconciliation.
    return 'valores_iguais' if money(system_balance) == response.declared_balance() else 'divergencia'


def crosscheck_titles(position, response):
    """Candidate matching is a review aid: never evidence of settlement."""
    if response.statement_only:
        return [dict(reference='Demonstrativo anexo', status='conferencia_documental',
                     declared=f'{response.declared_balance():.2f}', system=None, parcels=[],
                     reason='Identificar e conferir cada documento do demonstrativo.')]
    rows, matched = [], set()
    replaced = {r for a in response.agreements for r in a.replaces}
    for title in response.titles:
        candidates = [t for t in position['titles'] if t['reference'].casefold() == title.reference.casefold()]
        if not candidates and title.invoice:
            candidates = [t for t in position['titles'] if t['invoice'].casefold() == title.invoice.casefold()]
        system = None
        if len(candidates) > 1:
            status, reason = 'correspondencia_ambigua', 'Mais de uma parcela candidata; identificar a parcela correta.'
        elif not candidates:
            status, reason = 'nao_localizado_mv', 'Documento declarado sem correspondência identificada no MV.'
        else:
            candidate = candidates[0]
            matched.add(candidate['parcel'])
            system = candidate['balance']
            status, reason = 'conferir_documentos', 'Valores e vencimento coincidem; conferir documentos e baixas.'
            if title.reference in replaced:
                status, reason = 'substituido_por_acordo', 'Conferir a vinculação e a substituição no acordo.'
            elif money(system) != title.balance:
                status, reason = 'valor_divergente', 'Saldo informado difere do saldo atual do MV.'
            elif candidate['due'] != title.due.isoformat():
                status, reason = 'vencimento_divergente', 'Vencimentos diferentes; verificar prorrogação ou acordo.'
        rows.append(dict(reference=title.reference, status=status, reason=reason, declared=f'{title.balance:.2f}',
                         system=system, parcels=[t['parcel'] for t in candidates]))
    for title in position['titles']:
        if money(title['balance']) > 0 and title['parcel'] not in matched:
            is_replaced = title['reference'] in replaced
            rows.append(dict(reference=title['reference'], declared=None, system=title['balance'], parcels=[title['parcel']],
                status='substituido_por_acordo' if is_replaced else 'nao_declarado',
                reason='Conferir a substituição no acordo.' if is_replaced else 'Título aberto no MV não identificado na declaração. Não considerar quitado.'))
    for agreement in response.agreements:
        rows.append(dict(reference=f'Acordo {agreement.reference}', status='conferir_acordo', declared=f'{agreement.balance:.2f}',
            system=None, parcels=[], reason='Conferir instrumento, parcelas e títulos de origem. Vinculação pendente.' if not agreement.replaces else 'Conferir instrumento, parcelas e baixa/substituição dos títulos de origem.'))
    return rows


def aggregate_position(rows, start, end):
    groups = {}
    for raw in rows:
        r = dict(raw)
        supplier = str(r.get('supplier') or ('sem-' + hashlib.sha256(
            str(r.get('account') or f"parcela-{r['parcel']}").encode()).hexdigest()[:24]))
        key = (int(r['company']), supplier)
        group = groups.setdefault(key, dict(company=key[0], supplier=supplier, company_name=r['company_name'],
            name=r.get('name') or 'Fornecedor sem cadastro', document=r.get('document') or '', _titles={}))
        title = group['_titles'].setdefault(str(r['parcel']), dict(parcel=str(r['parcel']), account=r.get('account'),
            reference=f"{r.get('invoice') or ''}/{r.get('installment') or ''}", invoice=str(r.get('invoice') or ''),
            due=str(date_value(r.get('due')) or ''), amount=f"{money(r['amount']):.2f}",
            status=r.get('status') or '', classification=r.get('classification') or '', observations=r.get('observations') or '', _payments={}))
        if r.get('payment') is not None and not r.get('reversed_at'):
            title['_payments'][str(r['payment'])] = dict(id=str(r['payment']), amount=f"{money(r.get('paid')):.2f}",
                date=str(date_value(r.get('paid_at')) or ''), source='MV')
    result = []
    for group in groups.values():
        titles, payments, balance = [], [], Decimal(0)
        for title in group.pop('_titles').values():
            title['payments'] = sorted(title.pop('_payments').values(), key=lambda x: x['id'])
            paid = sum((money(p['amount']) for p in title['payments']), Decimal(0))
            title['paid_mv'] = f'{paid:.2f}'
            title['balance'] = f"{(Decimal(0) if title['status'].casefold() == 'quitado' else max(money(title['amount']) - paid, Decimal(0))):.2f}"
            balance += money(title['balance'])
            payments.extend(p for p in title['payments'] if p['date'] and start <= date_value(p['date']) <= end)
            titles.append(title)
        group.update(system_balance=f'{balance:.2f}', paid_period=f"{sum((money(p['amount']) for p in payments), Decimal(0)):.2f}",
            last_payment=max((p['date'] for p in payments), default=None), titles=sorted(titles, key=lambda t: t['parcel']))
        fingerprint_data = json.dumps(group['titles'], sort_keys=True, default=str, ensure_ascii=False)
        group['fingerprint'] = hashlib.sha256(fingerprint_data.encode()).hexdigest()
        result.append(group)
    return sorted(result, key=lambda g: (g['company'], g['name'].casefold(), g['supplier']))
