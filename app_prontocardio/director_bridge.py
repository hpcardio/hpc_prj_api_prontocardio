"""Default-OFF, exact-sender bridge; never log bodies or credentials."""

# Limites de segurança explícitos e validação em sequência (código
# recuperado de produção sem refatorar a lógica).
# ruff: noqa: PLR2004, PLR0912

import asyncio
import hashlib
import hmac
import json
import os
import re

import httpx
from fastapi import HTTPException, Request

DIRECTOR_TARGET_URL = (
    'https://prontogestao.hospitalprontocardio.com.br'
    '/api/whatsapp-indicadores/webhook'
)
MAX_BYTES = 256 * 1024
TOTAL_TIMEOUT_SECONDS = 8
MAX_ACK_BYTES = 64 * 1024


def validate_director_ack(ack, expected_receipts):
    if not isinstance(ack, dict) or ack.get('received') is not True:
        raise unavailable()
    director = ack.get('director')
    if (
        not isinstance(director, dict)
        or director.get('enabled') is not True
        or director.get('liveReady') is not True
    ):
        raise unavailable()
    receipts = director.get('receipts')
    if not isinstance(receipts, list) or len(receipts) > 100:
        raise unavailable()
    actual = set()
    for receipt in receipts:
        if not isinstance(receipt, dict):
            raise unavailable()
        phone = receipt.get('phoneNumberId')
        message = receipt.get('metaMessageId')
        if (
            not isinstance(phone, str)
            or not 1 <= len(phone) <= 32
            or not isinstance(message, str)
            or not 1 <= len(message) <= 256
        ):
            raise unavailable()
        actual.add((phone, message))
    if actual != expected_receipts:
        raise unavailable()


def unavailable():
    return HTTPException(
        503, 'Director ingress unavailable.', headers={'Retry-After': '30'}
    )


def configuration():
    senders = os.environ.get('DIRECTOR_BRIDGE_SENDER_IDS', '').split(',')
    ids = [
        os.environ.get(key, '')
        for key in (
            'DIRECTOR_BRIDGE_PHONE_NUMBER_ID',
            'DIRECTOR_BRIDGE_WABA_ID',
            'DIRECTOR_BRIDGE_META_APP_ID',
        )
    ]
    secret = os.environ.get('DIRECTOR_BRIDGE_META_APP_SECRET', '')
    if (
        len(senders) != 2
        or len(set(senders)) != 2
        or any(not re.fullmatch(r'[0-9]{8,15}', value) for value in senders)
        or any(not re.fullmatch(r'[0-9]{1,32}', value) for value in ids)
        or not secret
        or len(secret) > 512
    ):
        raise unavailable()
    # Meta message envelopes expose WABA, not app ID. The configured app ID
    # identifies the app whose secret authenticates these original bytes.
    return set(senders), ids[0], ids[1], secret


def bounded_payload(raw):
    try:
        payload = json.loads(raw.decode('utf-8'))
    except (ValueError, UnicodeError, RecursionError) as error:
        raise HTTPException(400, 'Invalid envelope.') from error
    pending = [(payload, 0)]
    nodes = 0
    while pending:
        value, depth = pending.pop()
        nodes += 1
        if nodes > 10000 or depth > 16:
            raise HTTPException(413, 'Envelope too complex.')
        if isinstance(value, (list, dict)):
            if len(value) > 100:
                raise HTTPException(413, 'Too many envelope items.')
            pending.extend(
                (item, depth + 1)
                for item in (
                    value.values() if isinstance(value, dict) else value
                )
            )
    return payload


def messages(payload):
    if not isinstance(payload, dict) or not isinstance(
        payload.get('entry', []), list
    ):
        raise HTTPException(400, 'Invalid envelope.')
    total = 0
    for ei, entry in enumerate(payload.get('entry', [])):
        if not isinstance(entry, dict) or not isinstance(
            entry.get('changes', []), list
        ):
            raise HTTPException(400, 'Invalid envelope.')
        for ci, change in enumerate(entry.get('changes', [])):
            if not isinstance(change, dict):
                raise HTTPException(400, 'Invalid envelope.')
            value = change.get('value') or {}
            if not isinstance(value, dict) or not isinstance(
                value.get('messages', []), list
            ):
                raise HTTPException(400, 'Invalid envelope.')
            for mi, message in enumerate(value.get('messages', [])):
                total += 1
                if total > 100:
                    raise HTTPException(413, 'Too many messages.')
                if not isinstance(message, dict):
                    raise HTTPException(400, 'Invalid envelope.')
                yield (ei, ci, mi), entry, change, value, message


def supported_director_message(message):
    if message.get('type') in ('text', 'audio', 'button'):
        return True
    interactive = message.get('interactive')
    return (
        message.get('type') == 'interactive'
        and isinstance(interactive, dict)
        and interactive.get('type') == 'button_reply'
    )


async def forward_original(raw, signature, expected_receipts):
    try:
        # HTTPX per-operation deadlines are supplemented by an absolute
        # deadline. Disable ambient proxy credentials and redirects to any
        # other endpoint.
        async with asyncio.timeout(TOTAL_TIMEOUT_SECONDS):
            async with httpx.AsyncClient(
                timeout=7.0, follow_redirects=False, trust_env=False
            ) as client:
                async with client.stream(
                    'POST',
                    DIRECTOR_TARGET_URL,
                    content=raw,
                    headers={
                        'Content-Type': 'application/json',
                        'X-Hub-Signature-256': signature,
                    },
                ) as response:
                    if response.status_code != 200:
                        raise unavailable()
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > MAX_ACK_BYTES:
                            raise unavailable()
                    ack = json.loads(body)
                    validate_director_ack(ack, expected_receipts)
    except (httpx.HTTPError, TimeoutError, ValueError) as error:
        raise unavailable() from error


async def route_director_envelope(request: Request):
    if os.environ.get('DIRECTOR_BRIDGE_ENABLED') != 'true':
        return await request.json(), set()
    size = 0
    chunks = []
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_BYTES:
            raise HTTPException(413, 'Payload too large.')
        chunks.append(chunk)
    raw = b''.join(chunks)
    payload = bounded_payload(raw)
    senders, phone_id, waba_id, secret = configuration()
    selected = set()
    expected_receipts = set()
    selected_senders = {}
    for position, entry, change, value, message in messages(payload):
        sender = message.get('from')
        if sender is not None and not isinstance(sender, str):
            raise HTTPException(400, 'Invalid sender.')
        if sender not in senders:
            continue
        # Never fall through to patient auto-reply for a blocked director
        # event.
        metadata = value.get('metadata')
        if (
            payload.get('object') != 'whatsapp_business_account'
            or entry.get('id') != waba_id
            or change.get('field') != 'messages'
            or not isinstance(metadata, dict)
            or metadata.get('phone_number_id') != phone_id
        ):
            raise HTTPException(403, 'Director source mismatch.')
        selected.add(position)
        if not supported_director_message(message):
            continue
        message_id = message.get('id')
        if not isinstance(message_id, str) or not re.fullmatch(
            r'[A-Za-z0-9_.+/=-]{1,256}', message_id
        ):
            raise HTTPException(400, 'Invalid director message ID.')
        receipt = (phone_id, message_id)
        if receipt in selected_senders and selected_senders[receipt] != sender:
            raise HTTPException(400, 'Conflicting director message identity.')
        selected_senders[receipt] = sender
        expected_receipts.add(receipt)
    if not selected:
        return payload, selected
    signature = request.headers.get('x-hub-signature-256', '')
    if not re.fullmatch(r'sha256=[a-f0-9]{64}', signature):
        raise HTTPException(403, 'Invalid signature.')
    expected = hmac.new(
        secret.encode('utf-8'), raw, hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(signature[7:], expected):
        raise HTTPException(403, 'Invalid signature.')
    if expected_receipts:
        await forward_original(raw, signature, expected_receipts)
    return payload, selected
