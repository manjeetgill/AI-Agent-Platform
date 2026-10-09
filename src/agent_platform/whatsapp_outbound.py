"""Reviewed, durable template sends. No retry after a possibly accepted dispatch."""
import asyncio
import hashlib
import hmac
import re
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID, uuid4

import httpx
from fastapi import APIRouter, HTTPException, Request
from psycopg.types.json import Jsonb
from pydantic import Field

from .meta_worker import managed_config
from .service_identity import authenticate
from .tasks import Strict
from .whatsapp import audit


class Draft(Strict):
    recipient: str = Field(pattern=r'^\+?[1-9]\d{7,14}$')
    template_id: str = Field(pattern=r'^\d{5,30}$')
    idempotency_key: str = Field(min_length=8, max_length=100)


class Approval(Strict):
    confirm: Literal[True]
    recipient_consented: Literal[True]


class SessionDraft(Strict):
    recipient: str = Field(pattern=r'^\+?[1-9]\d{7,14}$')
    text: str = Field(min_length=1, max_length=1500)
    idempotency_key: str = Field(min_length=8, max_length=100)


async def session_available(conn, channel_id, recipient):
    # Use verified provider ingress, never a caller-supplied timestamp.
    return bool(await (await conn.execute("""SELECT c.id FROM whatsapp_conversations c
        WHERE c.channel_id=%s AND c.customer=%s AND c.mode='AUTO' AND NOT c.opted_out
        AND EXISTS(SELECT 1 FROM whatsapp_messages m WHERE m.conversation_id=c.id
          AND m.received_at>now()-interval '23 hours 55 minutes' AND m.received_at<=now())""",
        (channel_id, recipient))).fetchone())


class BatchApproval(Approval):
    message_ids: list[UUID] = Field(min_length=1, max_length=50)


async def graph(config, resource, *, method='GET', payload=None):
    if config.get('provider') == 'chakra':
        # Never send a Chakra credential to Meta. Template catalogue integration
        # has its own transport; inbound session replies use the Chakra worker adapter.
        raise HTTPException(409, 'Use the configured Chakra transport for this connection')
    if not re.fullmatch(r'\d{5,30}(/message_templates|/messages)?', resource):
        raise HTTPException(422, 'Invalid WhatsApp resource')
    try:
        params = {'limit': 100} if resource.endswith('/message_templates') else {}
        if config.get('app_secret'):
            params['appsecret_proof'] = hmac.new(config['app_secret'].encode(), config['access_token'].encode(), hashlib.sha256).hexdigest()
        async with httpx.AsyncClient(timeout=httpx.Timeout(12, connect=3)) as client:
            r = await client.request(method, f"https://graph.facebook.com/{config['graph_version']}/{resource}",
                headers={'Authorization': 'Bearer '+config['access_token']}, json=payload,
                params=params)
        if r.status_code >= 400:
            try:
                code = r.json().get('error', {}).get('code')
            except (ValueError, AttributeError):
                code = None
            if code == 190:
                raise HTTPException(502, 'Meta access token is invalid or expired. Refresh this connection privately, then test it again.')
            if r.status_code in {401, 403}:
                raise HTTPException(502, 'Meta denied access. Check that this token is authorised for the connected business number.')
            raise HTTPException(502, 'Meta rejected the request. Check token, number permissions, template and verified recipient.')
        return r.json()
    except (httpx.HTTPError, ValueError):
        raise HTTPException(503, 'Meta connection unavailable. Sending outcomes must be checked before retrying.') from None


async def template_options(config):
    waba = config.get('waba_id') or ''
    if not re.fullmatch(r'\d{5,30}', waba):
        raise HTTPException(503, 'Platform owner must complete WhatsApp business-account setup')
    if config.get('provider') == 'chakra':
        from .chakra_client import ChakraError, list_templates
        try:
            result = {'data': await list_templates(config)}
        except ChakraError:
            raise HTTPException(503, 'WhatsApp templates unavailable. Check the provider connection.') from None
    else:
        result = await graph(config, waba+'/message_templates')
    available = []
    for item in result.get('data', []):
        parts = item.get('components', [])
        # Pilot supports approved text-only templates without substitution parameters or buttons.
        if item.get('status') not in {'APPROVED', 'PENDING'} or not parts or not any(p.get('type') == 'BODY' for p in parts):
            continue
        if any(p.get('type') not in {'HEADER', 'BODY', 'FOOTER'} or not p.get('text')
               or '{{' in p['text'] or (p.get('type') == 'HEADER' and p.get('format') != 'TEXT') for p in parts):
            continue
        available.append({key: item[key] for key in ('id', 'name', 'language')} |
                         {'text': '\n\n'.join(p['text'] for p in parts), 'status': item['status']})
    return available


async def templates(config):
    return [{k: v for k, v in t.items() if k != 'status'}
            for t in await template_options(config) if t['status'] == 'APPROVED']


def install_outbound(app):
    router = APIRouter(prefix='/v1/whatsapp/channels')

    async def context(request, channel_id):
        pool = request.app.state.repository.pool
        claims = await authenticate(pool, request.headers.get('authorization', ''),
                                    request.headers.get('X-External-User-ID'), 'channels:write')
        async with pool.connection() as conn:
            channel = await (await conn.execute(
                'SELECT * FROM whatsapp_channels WHERE id=%s AND app_id=%s AND owner_id=%s',
                (channel_id, claims['app_id'], claims['user']))).fetchone()
        if not channel:
            raise HTTPException(404, 'Agent not found')
        return pool, claims, channel

    @router.get('/{channel_id}/send-setup')
    async def setup(channel_id: UUID, request: Request):
        pool, _, channel = await context(request, channel_id)
        config = await managed_config(pool, channel['phone_id'], channel['app_id'], channel['owner_id'])
        if config.get('provider') == 'chakra':
            from .chakra_client import ChakraError, phone_details
            try:
                sender = await phone_details(config)
            except ChakraError:
                raise HTTPException(503, 'WhatsApp number verification unavailable. Check the provider connection.') from None
        else:
            sender = await graph(config, channel['phone_id'])
        options = await template_options(config)
        approved = [{k: v for k, v in t.items() if k != 'status'} for t in options if t['status'] == 'APPROVED']
        pending = [{k: v for k, v in t.items() if k != 'status'} for t in options if t['status'] == 'PENDING']
        return {'sender': sender.get('display_phone_number', ''),
                'name': sender.get('verified_name', ''), 'templates': approved, 'pending_templates': pending,
                'webhook_verified': bool(channel['webhook_verified_at'])}

    @router.post('/{channel_id}/connection-test')
    async def check_connection(channel_id: UUID, request: Request):
        pool, claims, channel = await context(request, channel_id)
        config = await managed_config(pool, channel['phone_id'], channel['app_id'], channel['owner_id'])
        sender = await graph(config, channel['phone_id'])
        if str(sender.get('id', '')) != channel['phone_id']:
            raise HTTPException(502, 'Provider response did not identify the requested number; connection is unverified.')
        async with pool.connection() as conn:
            await audit(conn, channel_id, None, 'sender_credentials_checked', claims['principal'])
        return {'credentials_checked': True, 'checked_at': datetime.now(UTC),
                'webhook_verified': bool(channel['webhook_verified_at']),
                'channel_enabled': channel['enabled'], 'message_sent': False,
                'next_action': 'Verify the public webhook and test a real incoming message before relying on replies.'}

    @router.post('/{channel_id}/outbound', status_code=201)
    async def draft(channel_id: UUID, body: Draft, request: Request):
        pool, claims, channel = await context(request, channel_id)
        recipient = body.recipient.lstrip('+')
        async with pool.connection() as conn:
            prior = await (await conn.execute('SELECT * FROM whatsapp_outbound WHERE channel_id=%s AND request_key=%s',
                (channel_id, body.idempotency_key))).fetchone()
        if prior:
            if prior['recipient'] != recipient or prior['template']['id'] != body.template_id:
                raise HTTPException(409, 'Request key belongs to a different message')
            return prior
        selected = next((t for t in await templates(await managed_config(pool, channel['phone_id'], channel['app_id'], channel['owner_id']))
                         if t['id'] == body.template_id), None)
        if not selected:
            raise HTTPException(422, 'Select an available approved template')
        async with pool.connection() as conn, conn.transaction():
            # Serialize daily cap and duplicate preparations for this sender.
            await conn.execute('SELECT id FROM whatsapp_channels WHERE id=%s FOR UPDATE', (channel_id,))
            prior = await (await conn.execute('SELECT * FROM whatsapp_outbound WHERE channel_id=%s AND request_key=%s',
                (channel_id, body.idempotency_key))).fetchone()
            if prior:
                if prior['recipient'] != recipient or prior['template']['id'] != body.template_id:
                    raise HTTPException(409, 'Request key belongs to a different message')
                return prior
            uncertain = await (await conn.execute("SELECT id FROM whatsapp_outbound WHERE channel_id=%s AND recipient=%s AND status IN ('QUEUED','SENDING','UNKNOWN') LIMIT 1", (channel_id, recipient))).fetchone()
            if uncertain:
                raise HTTPException(409, 'A message to this recipient is pending or uncertain. Check its outcome before preparing another.')
            count = await (await conn.execute("SELECT count(*) AS n FROM whatsapp_outbound WHERE channel_id=%s AND created_at>now()-interval '24 hours'", (channel_id,))).fetchone()
            if count['n'] >= 100:
                raise HTTPException(429, 'Pilot limit reached: 100 prepared messages per sender per day')
            row = await (await conn.execute("INSERT INTO whatsapp_outbound(id,channel_id,request_key,recipient,template,status) VALUES(%s,%s,%s,%s,%s,'DRAFT') RETURNING *",
                (uuid4(), channel_id, body.idempotency_key, recipient, Jsonb(selected)))).fetchone()
            await audit(conn, channel_id, None, 'outbound:prepared:'+str(row['id']), claims['principal'])
        return row

    @router.post('/{channel_id}/session-outbound', status_code=201)
    async def session_draft(channel_id: UUID, body: SessionDraft, request: Request):
        pool, claims, channel = await context(request, channel_id)
        recipient = body.recipient.lstrip('+')
        content = {'id': 'session', 'kind': 'session', 'text': body.text}
        async with pool.connection() as conn, conn.transaction():
            await conn.execute('SELECT id FROM whatsapp_channels WHERE id=%s FOR UPDATE', (channel_id,))
            prior = await (await conn.execute('SELECT * FROM whatsapp_outbound WHERE channel_id=%s AND request_key=%s',
                (channel_id, body.idempotency_key))).fetchone()
            if prior:
                if prior['recipient'] != recipient or prior['template'] != content:
                    raise HTTPException(409, 'Request key belongs to a different message')
                return prior
            if not channel['enabled'] or not channel['webhook_verified_at'] or not await session_available(conn, channel_id, recipient):
                raise HTTPException(409, 'No active patient conversation. Ask the patient to message the clinic or use an approved template.')
            if await (await conn.execute("SELECT id FROM whatsapp_outbound WHERE channel_id=%s AND recipient=%s AND status IN ('QUEUED','SENDING','UNKNOWN') LIMIT 1", (channel_id, recipient))).fetchone():
                raise HTTPException(409, 'A previous send is pending or uncertain; check its outcome first')
            count = await (await conn.execute("SELECT count(*) n FROM whatsapp_outbound WHERE channel_id=%s AND created_at>now()-interval '24 hours'", (channel_id,))).fetchone()
            if count['n'] >= 100:
                raise HTTPException(429, 'Pilot sending limit reached')
            row = await (await conn.execute("INSERT INTO whatsapp_outbound(id,channel_id,request_key,recipient,template,status,expires_at) VALUES(%s,%s,%s,%s,%s,'DRAFT',now()+interval '1 hour') RETURNING *",
                (uuid4(), channel_id, body.idempotency_key, recipient, Jsonb(content)))).fetchone()
            await audit(conn, channel_id, None, 'session:prepared:'+str(row['id']), claims['principal'])
        return row

    @router.get('/{channel_id}/outbound')
    async def history(channel_id: UUID, request: Request):
        pool, _, _ = await context(request, channel_id)
        async with pool.connection() as conn:
            return await (await conn.execute('SELECT *,request_key AS idempotency_key FROM whatsapp_outbound WHERE channel_id=%s ORDER BY created_at DESC LIMIT 100', (channel_id,))).fetchall()

    @router.post('/{channel_id}/outbound/approve-batch')
    async def approve_batch(channel_id: UUID, body: BatchApproval, request: Request):
        pool, claims, channel = await context(request, channel_id)
        if not channel['webhook_verified_at']:
            raise HTTPException(409, 'Complete webhook setup before sending to recipients')
        ids = sorted(set(body.message_ids))
        async with pool.connection() as conn, conn.transaction():
            rows = await (await conn.execute('SELECT * FROM whatsapp_outbound WHERE channel_id=%s AND id=ANY(%s) ORDER BY id FOR UPDATE', (channel_id, ids))).fetchall()
            if len(rows) != len(ids):
                raise HTTPException(404, 'One or more messages do not belong to this agent')
            if any(r['status'] == 'DRAFT' and r['expires_at'] <= datetime.now(UTC) for r in rows):
                raise HTTPException(409, 'Review expired. Prepare a new batch.')
            for row in rows:
                if row['status'] == 'DRAFT':
                    await conn.execute("UPDATE whatsapp_outbound SET status='QUEUED',approved_at=now(),updated_at=now() WHERE id=%s", (row['id'],))
                    await audit(conn, channel_id, None, 'outbound:approved:'+str(row['id']), claims['principal'])
        return {'message_ids': ids, 'queued': True}

    @router.post('/{channel_id}/outbound/{message_id}/approve')
    async def approve(channel_id: UUID, message_id: UUID, body: Approval, request: Request):
        pool, claims, channel = await context(request, channel_id)
        if not channel['webhook_verified_at']:
            raise HTTPException(409, 'Complete webhook setup before sending to recipients')
        async with pool.connection() as conn, conn.transaction():
            row = await (await conn.execute('SELECT * FROM whatsapp_outbound WHERE id=%s AND channel_id=%s FOR UPDATE', (message_id, channel_id))).fetchone()
            if not row:
                raise HTTPException(404, 'Message not found')
            if row['status'] != 'DRAFT':
                return row
            if row['expires_at'] <= datetime.now(UTC):
                raise HTTPException(409, 'Review expired. Prepare a new message.')
            await conn.execute("UPDATE whatsapp_outbound SET status='QUEUED',approved_at=now(),updated_at=now() WHERE id=%s", (message_id,))
            await audit(conn, channel_id, None, 'outbound:approved:'+str(message_id), claims['principal'])
        return {'id': message_id, 'status': 'QUEUED'}

    app.include_router(router)


async def send_template(config, phone, recipient, template):
    if template.get('kind') == 'session':
        from .whatsapp_worker import send
        return await send(config, phone, recipient, template['text'])
    payload = {
        'messaging_product': 'whatsapp', 'to': recipient, 'type': 'template',
        'template': {'name': template['name'], 'language': {'code': template['language']}}}
    if config.get('provider') == 'chakra':
        from .chakra_client import send_message
        return await send_message(config, phone, payload)
    value = await graph(config, phone+'/messages', method='POST', payload=payload)
    return value['messages'][0]['id']


async def tick_outbound(pool, sender=send_template, catalogue=templates):
    async with pool.connection() as conn, conn.transaction():
        await conn.execute("UPDATE whatsapp_outbound SET status='UNKNOWN',error='Interrupted dispatch; check Meta before sending again',updated_at=now() WHERE status='SENDING' AND updated_at<now()-interval '2 minutes'")
        job = await (await conn.execute("SELECT * FROM whatsapp_outbound WHERE status='QUEUED' ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1")).fetchone()
        if not job:
            return False
        if job['expires_at'] <= datetime.now(UTC):
            await conn.execute("UPDATE whatsapp_outbound SET status='CANCELLED',error='Approval expired before dispatch' WHERE id=%s", (job['id'],))
            return True
        channel = await (await conn.execute('SELECT * FROM whatsapp_channels WHERE id=%s', (job['channel_id'],))).fetchone()
        if not channel['webhook_verified_at']:
            await conn.execute("UPDATE whatsapp_outbound SET status='CANCELLED',error='Business connection requires webhook verification' WHERE id=%s", (job['id'],))
            return True
        stopped = await (await conn.execute('SELECT id FROM whatsapp_conversations WHERE channel_id=%s AND customer=%s AND opted_out', (job['channel_id'], job['recipient']))).fetchone()
        if stopped:
            await conn.execute("UPDATE whatsapp_outbound SET status='CANCELLED',error='Recipient opted out' WHERE id=%s", (job['id'],))
            return True
        try:
            config = await managed_config(pool, channel['phone_id'], channel['app_id'], channel['owner_id'])
            if job['template'].get('kind') == 'session':
                if not channel['enabled'] or not await session_available(conn, channel['id'], job['recipient']):
                    raise ValueError('Patient conversation closed')
            else:
                async with asyncio.timeout(15):
                    current = await catalogue(config)
                if job['template'] not in current:
                    raise ValueError('Template changed')
        except Exception:  # noqa: BLE001 -- safe to fail before dispatch
            await conn.execute("UPDATE whatsapp_outbound SET status='FAILED',error='Template or connection unavailable; no message dispatched' WHERE id=%s", (job['id'],))
            return True
        from .channel_trials import reserve_committed
        if not await reserve_committed(pool, channel['app_id'], 'whatsapp_sends', job['id']):
            await conn.execute("UPDATE whatsapp_outbound SET status='CANCELLED',error='Pilot usage limit or expiry reached' WHERE id=%s", (job['id'],))
            return True
        # Fence commits independently before any network send. A rolled-back job cannot send twice.
        async with pool.connection() as fence:
            inserted = await (await fence.execute('INSERT INTO whatsapp_dispatches(message_id) VALUES(%s) ON CONFLICT DO NOTHING RETURNING message_id', (job['id'],))).fetchone()
        if not inserted:
            await conn.execute("UPDATE whatsapp_outbound SET status='UNKNOWN',error='Dispatch already attempted; check Meta before sending again' WHERE id=%s", (job['id'],))
            return True
        await conn.execute("UPDATE whatsapp_outbound SET status='SENDING',updated_at=now() WHERE id=%s", (job['id'],))
        try:
            async with asyncio.timeout(15):
                outbound_id = await sender(config, channel['phone_id'], job['recipient'], job['template'])
            if not isinstance(outbound_id, str) or not 1 <= len(outbound_id) <= 300:
                raise ValueError('Invalid provider result')
            await conn.execute("UPDATE whatsapp_outbound SET status='ACCEPTED',outbound_id=%s,updated_at=now() WHERE id=%s", (outbound_id, job['id']))
            await audit(conn, channel['id'], None, 'outbound:accepted:'+str(job['id']), 'worker')
        except Exception:  # noqa: BLE001 -- never retry possibly accepted sends
            await conn.execute("UPDATE whatsapp_outbound SET status='UNKNOWN',error='Meta acceptance is uncertain. Check provider records before another send.',updated_at=now() WHERE id=%s", (job['id'],))
    return True
