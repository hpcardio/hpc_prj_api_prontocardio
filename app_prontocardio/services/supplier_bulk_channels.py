"""Provider adapters: accepted is not delivered. Unknown outcomes are not retried."""
import hashlib
import json
import re
import smtplib
import socket
import time
import unicodedata
from urllib.parse import urlencode
from urllib.request import Request,urlopen
from fastapi import HTTPException
from app_prontocardio.services.supplier_bulk import Rejected,Uncertain
from app_prontocardio.routers.supplier_updates import send_invitation_email
from app_prontocardio.routers.supplier_payment_notices import send_payment_email
from app_prontocardio.services.supplier_payment_notices import brl
from app_prontocardio.whatsapp_service import enviar_template_whatsapp

_templates={}


class SupplierBulkProvider:
    def __init__(self,settings):self.settings=settings

    def graph_get(self,path,params):
        s=self.settings
        url=f'https://graph.facebook.com/{s.WHATSAPP_GRAPH_API_VERSION}/{path}?'+urlencode(params)
        with urlopen(Request(url,headers={'Authorization':'Bearer '+s.WHATSAPP_ACCESS_TOKEN}),timeout=15) as response:
            return json.load(response)

    def template(self,kind):
        s=self.settings
        name=s.SUPPLIER_UPDATE_WHATSAPP_TEMPLATE if kind=='updates' else s.SUPPLIER_PAYMENT_WHATSAPP_TEMPLATE
        if not (s.WHATSAPP_PHONE_NUMBER_ID and s.WHATSAPP_ACCESS_TOKEN and s.SUPPLIER_WHATSAPP_WABA_ID and name):
            raise ValueError('WhatsApp: conta e modelo de fornecedores aguardam configuração.')
        if not re.fullmatch(r'\d+',s.SUPPLIER_WHATSAPP_WABA_ID) or not re.fullmatch(r'[a-z0-9_]{1,512}',name):raise ValueError('Configuração do modelo WhatsApp inválida.')
        key=(kind,s.SUPPLIER_WHATSAPP_WABA_ID,s.WHATSAPP_PHONE_NUMBER_ID,name,s.SUPPLIER_WHATSAPP_LANGUAGE)
        cached=_templates.get(key)
        if cached and time.monotonic()-cached[0]<60:return cached[1]
        phones=self.graph_get(s.SUPPLIER_WHATSAPP_WABA_ID+'/phone_numbers',{'fields':'id','limit':100})
        if not any(str(p['id'])==str(s.WHATSAPP_PHONE_NUMBER_ID) for p in phones.get('data',[])):raise ValueError('O número WhatsApp não corresponde à conta configurada.')
        data=self.graph_get(s.SUPPLIER_WHATSAPP_WABA_ID+'/message_templates',{'name':name,'fields':'id,name,status,language,components','limit':100})
        template=next((t for t in data.get('data',[]) if t.get('name')==name and t.get('language')==s.SUPPLIER_WHATSAPP_LANGUAGE and t.get('status')=='APPROVED'),None)
        if not template:raise ValueError('O modelo WhatsApp desta finalidade ainda não está aprovado ou disponível.')
        components=template.get('components',[])
        if any(c['type'] not in ('BODY','FOOTER','HEADER') or (c['type']!='BODY' and '{{' in c.get('text','')) or (c['type']=='HEADER' and c.get('format')!='TEXT') for c in components):
            raise ValueError('Use o modelo de fornecedores documentado, com parâmetros somente no corpo da mensagem.')
        body=next((c.get('text','') for c in components if c['type']=='BODY'),'')
        placeholders=set(re.findall(r'{{\s*(\d+)\s*}}',body))
        expected={'1','2'} if kind=='updates' else {'1','2','3'}
        normalized=''.join(c for c in unicodedata.normalize('NFD',body.casefold()) if not unicodedata.combining(c))
        if placeholders!=expected or 'fornecedor' not in normalized or ('atualiz' if kind=='updates' else 'pagamento') not in normalized:
            raise ValueError('O modelo aprovado não corresponde à mensagem de fornecedores desta finalidade.')
        result={'id':template['id'],'name':name,'body':body,'language':template['language']}
        _templates[key]=(time.monotonic(),result)
        return result

    def configuration(self,kind,channel):
        s=self.settings;body='Mensagem institucional de atualização cadastral com link individual.' if kind=='updates' else 'Aviso com títulos e valores, acompanhado dos comprovantes bancários.'
        available=True;reason='';config={}
        if channel=='email':
            available=bool(s.SMTP_HOST)
            if not available:reason='Envio de e-mail não configurado.'
            config={'host':s.SMTP_HOST,'port':s.SMTP_PORT,'sender':s.SMTP_FROM or s.SMTP_FROM_EMAIL}
        else:
            try:config=self.template(kind);body=config['body']
            except Exception as exc:
                available=False
                reason=str(exc) if isinstance(exc,ValueError) else 'Não foi possível conferir a aprovação do modelo WhatsApp.'
            config={**config,'phone':s.WHATSAPP_PHONE_NUMBER_ID}
        if available and not s.SUPPLIER_BULK_SEND_ENABLED:reason='O envio automático deste ambiente aguarda liberação.'
        return dict(available=available,enabled=bool(s.SUPPLIER_BULK_SEND_ENABLED),reason=reason,body=body,
            binding=hashlib.sha256(json.dumps(config,sort_keys=True).encode()).hexdigest(),template=config if channel=='whatsapp' and available else None)

    def send(self,channel,snapshot,prepared):
        try:
            if channel=='email':
                if snapshot['kind']=='updates':send_invitation_email(snapshot['destination'],prepared['message'])
                else:send_payment_email(destination=snapshot['destination'],message=prepared['message'],files=prepared['files'])
                return 'smtp-accepted'
            template=self.template(snapshot['kind'])
            parameters=[snapshot['name'],prepared['link']] if snapshot['kind']=='updates' else [snapshot['name'],brl(snapshot['total']),prepared['link']]
            result=enviar_template_whatsapp(telefone=snapshot['destination'],nome_template=template['name'],idioma=template['language'],parametros=parameters)
            id=(result.get('retorno_meta',{}).get('messages') or [{}])[0].get('id')
            if not id:raise Uncertain('A API não confirmou a identificação da mensagem.')
            return str(id)
        except (smtplib.SMTPRecipientsRefused,smtplib.SMTPSenderRefused,smtplib.SMTPAuthenticationError,smtplib.SMTPDataError,smtplib.SMTPConnectError,smtplib.SMTPHeloError,ConnectionRefusedError,socket.gaierror) as exc:
            raise Rejected('O servidor recusou o envio. Confira destinatário e configuração do canal.') from exc
        except HTTPException as exc:
            if exc.status_code>=500 or exc.status_code==408:raise Uncertain('A API não confirmou o resultado. Conferir antes de reenviar.') from exc
            raise Rejected('A API recusou a mensagem. Confira o modelo, o contato e a situação do canal.') from exc
        except (Rejected,Uncertain):raise
        except Exception as exc:raise Uncertain('Envio sem confirmação do provedor. Conferir antes de reenviar.') from exc
