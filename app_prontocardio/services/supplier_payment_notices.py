"""Payment notices describe verified MV events; they never create a payment."""
import hashlib
import io
import json
from datetime import date,datetime,timezone
from zoneinfo import ZoneInfo
from decimal import Decimal
from xml.sax.saxutils import escape

from pydantic import BaseModel, Field, model_validator
from app_prontocardio.services.supplier_updates import money, date_value


class PaymentSelection(BaseModel):
    company: int = Field(gt=0)
    supplier: str = Field(min_length=1,max_length=100)
    keys: list[str] = Field(min_length=1,max_length=100)

    @model_validator(mode='after')
    def unique_keys(self):
        if len(set(self.keys))!=len(self.keys) or any(len(k)>100 for k in self.keys):
            raise ValueError('Seleção de pagamentos inválida.')
        return self


class PaymentConfirmation(PaymentSelection):
    fingerprint: str = Field(min_length=64,max_length=64)
    confirmed: bool
    note: str = Field(default='',max_length=3000)


def payment_items(positions,start=None,end=None):
    start,end=start or date.min,end or date.max
    items={}
    for position in positions:
        for title in position['titles']:
            for payment in title['payments']:
                if not payment.get('date') or money(payment['amount'])<=0:
                    continue
                if not start<=date_value(payment['date'])<=end:
                    continue
                key=f"mv:{position['company']}:{title['parcel']}:{payment['id']}"
                items[key]=dict(key=key,company=position['company'],company_name=position.get('company_name',''),
                    supplier=position['supplier'],name=position['name'],document=position.get('document',''),
                    parcel=title['parcel'],payment_id=payment['id'],reference=title['reference'],invoice=title['invoice'],
                    date=payment['date'],amount=f"{money(payment['amount']):.2f}",source='MV')
    return sorted(items.values(),key=lambda p:(p['date'],p['key']),reverse=True)


def select_payments(positions,keys):
    by_key={item['key']:item for item in payment_items(positions)}
    if not keys or any(k not in by_key for k in keys):
        raise ValueError('Pagamento não localizado, alterado ou estornado. Atualize a consulta antes de confirmar.')
    selected=[by_key[k] for k in sorted(set(keys))]
    if len({(p['company'],p['supplier']) for p in selected})!=1:
        raise ValueError('Selecione pagamentos da mesma empresa e do mesmo fornecedor.')
    fingerprint=hashlib.sha256(json.dumps(selected,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    return dict(items=selected,total=f"{sum((money(p['amount']) for p in selected),Decimal(0)):.2f}",fingerprint=fingerprint)


def brl(value):
    return 'R$ '+f'{money(value):,.2f}'.translate(str.maketrans(',.','.,'))


def date_pt(value,with_time=False):
    parsed=datetime.fromisoformat(str(value).replace('Z','+00:00'))
    if with_time:
        if parsed.tzinfo is None:parsed=parsed.replace(tzinfo=timezone.utc)
        parsed=parsed.astimezone(ZoneInfo('America/Sao_Paulo'))
    return parsed.strftime('%d/%m/%Y %H:%M' if with_time else '%d/%m/%Y')


def notice_message(confirmation,link):
    lines=['Prezado fornecedor,','',
        'O Hospital Prontocardio informa a confirmação financeira dos pagamentos abaixo:', '',
        f"Fornecedor: {confirmation['name']}", f"Empresa pagadora: {confirmation.get('company_name') or confirmation['company']}",'']
    for item in confirmation['items']:
        lines.append(f"Documento/parcela {item['reference']} — {date_pt(item['date'])} — {brl(item['amount'])}")
    lines.extend(['',f"Total comunicado: {brl(confirmation['total'])}",'',
        'O aviso de pagamento e os comprovantes bancários estão disponíveis no link individual:',link,'',
        'Em caso de divergência na identificação dos títulos, entre em contato com o nosso departamento financeiro.','',
        'Atenciosamente,','Departamento Financeiro','Hospital Prontocardio'])
    return '\n'.join(lines)


def notice_pdf(confirmation):
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
    buffer=io.BytesIO()
    styles=getSampleStyleSheet()
    doc=SimpleDocTemplate(buffer,pagesize=A4,rightMargin=36,leftMargin=36,topMargin=36,bottomMargin=36)
    p=lambda value:Paragraph(escape(str(value)),styles['BodyText'])
    content=[Paragraph('Hospital Prontocardio',styles['Title']),Paragraph('Aviso de pagamento',styles['Heading1']),
        p(f"Protocolo: {confirmation['id']}"),p(f"Fornecedor: {confirmation['name']} — {confirmation.get('document','')}"),
        p(f"Empresa pagadora: {confirmation.get('company_name') or confirmation['company']}"),
        p(f"Conferência financeira registrada em: {date_pt(confirmation['confirmed_at'],True)}"),Spacer(1,18)]
    rows=[[p('Documento/parcela'),p('Data do pagamento'),p('Valor pago')]]
    rows += [[p(t['reference']),p(date_pt(t['date'])),p(brl(t['amount']))] for t in confirmation['items']]
    table=Table(rows,colWidths=[250,135,138],repeatRows=1)
    table.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,0),colors.HexColor('#e7f4f8')),
        ('VALIGN',(0,0),(-1,-1),'TOP'),('BOTTOMPADDING',(0,0),(-1,-1),10),
        ('TOPPADDING',(0,0),(-1,-1),8),('LINEBELOW',(0,0),(-1,-1),.4,colors.HexColor('#d0d5dd'))]))
    content += [table,Spacer(1,16),Paragraph('Total comunicado: '+brl(confirmation['total']),styles['Heading2'])]
    if confirmation.get('note'):
        content += [p('Observação: '+confirmation['note'])]
    content += [Spacer(1,18),p('Este aviso identifica os títulos comunicados pelo hospital. Os comprovantes bancários acompanham este documento separadamente.')]
    doc.build(content)
    return buffer.getvalue()
