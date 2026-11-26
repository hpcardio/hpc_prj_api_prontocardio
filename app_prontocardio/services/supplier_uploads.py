from contextlib import asynccontextmanager
from fastapi import HTTPException
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.formparsers import MultiPartException
from app_prontocardio.services.supplier_updates_files import MAX_TOTAL


@asynccontextmanager
async def limited_form(request):
    limit=MAX_TOTAL+1024*1024
    try:declared=int(request.headers.get('content-length','0'))
    except ValueError:raise HTTPException(400,'Tamanho da requisição inválido.') from None
    if declared<0 or declared>limit:raise HTTPException(413,'Envie até 25 MB de comprovantes.')
    receive=request._receive
    consumed=0
    async def bounded():
        nonlocal consumed
        message=await receive()
        consumed+=len(message.get('body',b''))
        if consumed>limit:raise MultiPartException('supplier-payment-upload-limit')
        return message
    request._receive=bounded
    try:
        async with request.form(max_files=10,max_fields=3,max_part_size=512*1024) as form:
            yield form
    except StarletteHTTPException as exc:
        if exc.detail=='supplier-payment-upload-limit':raise HTTPException(413,'Envie até 25 MB de comprovantes.') from None
        raise
    finally:request._receive=receive
