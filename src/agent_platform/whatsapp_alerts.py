"""App-scoped market alert delivery through an operator-approved WhatsApp sender.

Only recipients who opted in and recently messaged that sender can receive pilot
session text. A future approved-template path is required outside that window.
"""
import asyncio
import base64
import hashlib
import re
import secrets
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, HTTPException, Request
from pydantic import Field

from .channel_trials import reserve
from .meta_worker import managed_config
from .service_identity import authenticate
from .tasks import Strict
from .whatsapp_worker import send


class Subscription(Strict):
    recipient: str = Field(pattern=r'^\+?[1-9]\d{7,14}$')
    consented: Literal[True]


class VerificationRequest(Strict):
    recipient: str = Field(pattern=r'^\+?[1-9]\d{7,14}$')


class AlertDelivery(Strict):
    request_key: str = Field(min_length=8, max_length=100)
    exchange: Literal['NSE', 'BSE']
    symbol: str = Field(pattern=r'^[A-Z0-9&_.-]{1,120}$')
    timeframe: Literal['live', 'hourly', 'daily', 'weekly']
    condition: Literal['price_above', 'price_below', 'close_above', 'close_below', 'high_reaches', 'low_reaches']
    target: Decimal = Field(gt=0)
    observed: Decimal = Field(gt=0)
    triggered_at: datetime
    test_mode: bool = False


def message(alert: AlertDelivery) -> str:
    rule = {
        'price_above': 'price at or above', 'price_below': 'price at or below',
        'close_above': 'close at or above', 'close_below': 'close at or below',
        'high_reaches': 'high reached', 'low_reaches': 'low reached',
    }[alert.condition]
    moment = alert.triggered_at.astimezone(timezone(timedelta(hours=5, minutes=30)))
    label = 'MyAlgoDesk TEST alert (simulated)' if alert.test_mode else 'MyAlgoDesk alert'
    return (f'{label}: {alert.exchange}:{alert.symbol}\n'
            f'Rule: {rule} ₹{alert.target} ({alert.timeframe})\n'
            f'Observed: ₹{alert.observed}\n'
            f'Time: {moment:%d %b %Y, %I:%M %p} IST\n'
            'View the alert in MyAlgoDesk. No trade was placed.')


async def recent_conversation(conn, channel_id, recipient):
    return await (await conn.execute('''SELECT v.id FROM whatsapp_conversations v
        WHERE v.channel_id=%s AND v.customer=%s AND NOT v.opted_out
        AND EXISTS(SELECT 1 FROM whatsapp_messages m WHERE m.conversation_id=v.id
            AND m.received_at>now()-interval '23 hours 55 minutes')''',
        (channel_id, recipient))).fetchone()


def verification_hash(code: str) -> str:
    return hashlib.sha256(code.strip().upper().encode()).hexdigest()


async def consume_verification(conn, channel_id, recipient: str, body: str) -> bool:
    """Consume an inbound VERIFY command without exposing it to the business agent."""
    command = body.strip().upper()
    if not re.fullmatch(r'VERIFY [A-Z2-7]{10}', command):
        return False
    await conn.execute('''UPDATE whatsapp_alert_verifications SET state='EXPIRED'
        WHERE channel_id=%s AND recipient=%s AND state='PENDING' AND expires_at<=now()''',
        (channel_id, recipient))
    matched = await (await conn.execute('''SELECT id,state FROM whatsapp_alert_verifications
        WHERE channel_id=%s AND recipient=%s AND code_hash=%s
        AND created_at>now()-interval '1 day' ORDER BY created_at DESC LIMIT 1''',
        (channel_id, recipient, verification_hash(command)))).fetchone()
    if matched:
        await conn.execute('''UPDATE whatsapp_alert_verifications SET state='VERIFIED',verified_at=now()
        WHERE channel_id=%s AND recipient=%s AND state='PENDING' AND expires_at>now()
        AND code_hash=%s''', (channel_id, recipient, verification_hash(command)))
        return True
    # Hide a mistyped proof from the business agent while a challenge is active.
    return bool(await (await conn.execute('''SELECT id FROM whatsapp_alert_verifications
        WHERE channel_id=%s AND recipient=%s AND state='PENDING' AND expires_at>now()
        LIMIT 1''', (channel_id, recipient))).fetchone())


def install_alerts(app):
    router = APIRouter(prefix='/v1/whatsapp/alerts')

    async def context(request):
        pool = request.app.state.repository.pool
        claims = await authenticate(pool, request.headers.get('authorization', ''),
                                    request.headers.get('X-External-User-ID'), 'alerts:send')
        async with pool.connection() as conn:
            route = await (await conn.execute('''SELECT r.*,c.phone_id,c.owner_id AS channel_owner
                FROM whatsapp_alert_routes r JOIN whatsapp_channels c ON c.id=r.channel_id
                WHERE r.app_id=%s AND r.enabled AND c.enabled AND c.webhook_verified_at IS NOT NULL''',
                (claims['app_id'],))).fetchone()
        if not route:
            raise HTTPException(503, 'Alert sender is not configured for this app')
        if route['allowed_external_user'] and route['allowed_external_user'] != request.headers.get('X-External-User-ID'):
            raise HTTPException(403, 'WhatsApp alert pilot is not enabled for this user')
        return pool, claims, route

    @router.get('/subscription')
    async def subscription(request: Request):
        pool, claims, route = await context(request)
        async with pool.connection() as conn:
            row = await (await conn.execute('SELECT recipient,active,consented_at FROM whatsapp_alert_subscriptions WHERE app_id=%s AND owner_id=%s',
                (claims['app_id'], claims['user']))).fetchone()
        return {**(row or {'active': False}), 'sender_number': route['sender_number']}

    @router.post('/verifications', status_code=201)
    async def create_verification(body: VerificationRequest, request: Request):
        pool, claims, route = await context(request)
        if not route['sender_number']:
            raise HTTPException(503, 'Alert sender number is not configured for this app')
        recipient = body.recipient.lstrip('+')
        code = base64.b32encode(secrets.token_bytes(8)).decode().rstrip('=')[:10]
        command = f'VERIFY {code}'
        ident = uuid4()
        async with pool.connection() as conn, conn.transaction():
            await conn.execute('''UPDATE whatsapp_alert_verifications SET state='EXPIRED'
                WHERE state='PENDING' AND expires_at<=now()''')
            await conn.execute('''UPDATE whatsapp_alert_verifications SET state='CANCELLED'
                WHERE app_id=%s AND owner_id=%s AND state='PENDING' ''',
                (claims['app_id'], claims['user']))
            row = await (await conn.execute('''INSERT INTO whatsapp_alert_verifications
                (id,app_id,owner_id,channel_id,recipient,code_hash,expires_at)
                VALUES(%s,%s,%s,%s,%s,%s,now()+interval '10 minutes')
                RETURNING expires_at''', (ident, claims['app_id'], claims['user'], route['channel_id'],
                                          recipient, verification_hash(command)))).fetchone()
        return {'id': ident, 'state': 'PENDING', 'recipient': recipient,
                'sender_number': route['sender_number'], 'message': command,
                'instruction': f'Send this exact message to {route["sender_number"]} from the number being verified',
                'expires_at': row['expires_at']}

    @router.get('/verifications/{verification_id}')
    async def verification_status(verification_id: UUID, request: Request):
        pool, claims, _ = await context(request)
        async with pool.connection() as conn, conn.transaction():
            await conn.execute('''UPDATE whatsapp_alert_verifications SET state='EXPIRED'
                WHERE id=%s AND app_id=%s AND owner_id=%s AND state='PENDING' AND expires_at<=now()''',
                (verification_id, claims['app_id'], claims['user']))
            row = await (await conn.execute('''SELECT id,recipient,state,expires_at,verified_at,consumed_at
                FROM whatsapp_alert_verifications WHERE id=%s AND app_id=%s AND owner_id=%s''',
                (verification_id, claims['app_id'], claims['user']))).fetchone()
        if not row:
            raise HTTPException(404, 'Verification not found')
        return row

    @router.put('/subscription')
    async def subscribe(body: Subscription, request: Request):
        pool, claims, route = await context(request)
        recipient = body.recipient.lstrip('+')
        async with pool.connection() as conn, conn.transaction():
            current = await (await conn.execute('''SELECT recipient FROM whatsapp_alert_subscriptions
                WHERE app_id=%s AND owner_id=%s AND active FOR UPDATE''',
                (claims['app_id'], claims['user']))).fetchone()
            if route['require_recipient_verification'] and (not current or current['recipient'] != recipient):
                verified = await (await conn.execute('''SELECT id FROM whatsapp_alert_verifications
                    WHERE app_id=%s AND owner_id=%s AND channel_id=%s AND recipient=%s
                    AND state='VERIFIED' AND expires_at>now() ORDER BY verified_at DESC
                    FOR UPDATE LIMIT 1''', (claims['app_id'], claims['user'], route['channel_id'], recipient))).fetchone()
                if not verified:
                    raise HTTPException(409, 'Verify this WhatsApp number before enabling alerts')
                await conn.execute('''UPDATE whatsapp_alert_verifications
                    SET state='CONSUMED',consumed_at=now() WHERE id=%s''', (verified['id'],))
            if not await recent_conversation(conn, route['channel_id'], recipient):
                raise HTTPException(409, 'Send a WhatsApp message to the connected sender first, then try again')
            await conn.execute('''INSERT INTO whatsapp_alert_subscriptions(app_id,owner_id,recipient,consented_at)
                VALUES(%s,%s,%s,now()) ON CONFLICT(app_id,owner_id) DO UPDATE
                SET recipient=EXCLUDED.recipient,consented_at=now(),active=true''',
                (claims['app_id'], claims['user'], recipient))
        return {'recipient': recipient, 'active': True, 'sender_number': route['sender_number']}

    @router.delete('/subscription', status_code=204)
    async def unsubscribe(request: Request):
        pool, claims, _ = await context(request)
        async with pool.connection() as conn, conn.transaction():
            await conn.execute('UPDATE whatsapp_alert_subscriptions SET active=false WHERE app_id=%s AND owner_id=%s',
                               (claims['app_id'], claims['user']))
            await conn.execute('''UPDATE whatsapp_alert_deliveries SET status='CANCELLED',error='Subscription disabled'
                WHERE app_id=%s AND owner_id=%s AND status='QUEUED' ''',
                (claims['app_id'], claims['user']))

    @router.post('/deliveries', status_code=202)
    async def deliver(body: AlertDelivery, request: Request):
        pool, claims, route = await context(request)
        content = message(body)
        async with pool.connection() as conn, conn.transaction():
            await conn.execute('SELECT app_id FROM whatsapp_alert_routes WHERE app_id=%s FOR UPDATE',
                               (claims['app_id'],))
            prior = await (await conn.execute('SELECT id,status,body FROM whatsapp_alert_deliveries WHERE app_id=%s AND request_key=%s',
                (claims['app_id'], body.request_key))).fetchone()
            if prior:
                if prior['body'] != content:
                    raise HTTPException(409, 'Idempotency key belongs to a different alert')
                return {'id': prior['id'], 'status': prior['status']}
            subscription = await (await conn.execute('''SELECT * FROM whatsapp_alert_subscriptions
                WHERE app_id=%s AND owner_id=%s AND active FOR UPDATE''',
                (claims['app_id'], claims['user']))).fetchone()
            if not subscription:
                raise HTTPException(409, 'Enable WhatsApp alerts first')
            if not await recent_conversation(conn, route['channel_id'], subscription['recipient']):
                raise HTTPException(409, 'WhatsApp session expired; an approved alert template is required')
            count = await (await conn.execute('''SELECT count(*) AS n FROM whatsapp_alert_deliveries
                WHERE app_id=%s AND created_at>now()-interval '24 hours' ''', (claims['app_id'],))).fetchone()
            if count['n'] >= route['daily_limit']:
                raise HTTPException(429, 'Daily WhatsApp alert limit reached')
            ident = uuid4()
            await conn.execute('''INSERT INTO whatsapp_alert_deliveries
                (id,app_id,owner_id,channel_id,request_key,recipient,body,status)
                VALUES(%s,%s,%s,%s,%s,%s,%s,'QUEUED')''',
                (ident, claims['app_id'], claims['user'], route['channel_id'], body.request_key,
                 subscription['recipient'], content))
        return {'id': ident, 'status': 'QUEUED'}

    @router.get('/deliveries/{delivery_id}')
    async def status(delivery_id: UUID, request: Request):
        pool, claims, _ = await context(request)
        async with pool.connection() as conn:
            row = await (await conn.execute('''SELECT id,status,delivery,error,created_at,updated_at
                FROM whatsapp_alert_deliveries WHERE id=%s AND app_id=%s AND owner_id=%s''',
                (delivery_id, claims['app_id'], claims['user']))).fetchone()
        if not row:
            raise HTTPException(404, 'Delivery not found')
        return row

    app.include_router(router)


async def tick_alerts(pool, sender=send):
    async with pool.connection() as conn, conn.transaction():
        await conn.execute('''UPDATE whatsapp_alert_deliveries SET status='UNKNOWN',
            error='Interrupted dispatch; check provider before retrying',updated_at=now()
            WHERE status='SENDING' AND updated_at<now()-interval '2 minutes' ''')
        job = await (await conn.execute('''SELECT d.*,c.phone_id,c.app_id AS channel_app,c.owner_id AS channel_owner
            FROM whatsapp_alert_deliveries d JOIN whatsapp_channels c ON c.id=d.channel_id
            JOIN whatsapp_alert_routes r ON r.app_id=d.app_id AND r.channel_id=d.channel_id
            JOIN whatsapp_alert_subscriptions s ON s.app_id=d.app_id AND s.owner_id=d.owner_id
            WHERE d.status='QUEUED' AND r.enabled AND c.enabled AND s.active
            AND s.recipient=d.recipient ORDER BY d.created_at FOR UPDATE OF d SKIP LOCKED LIMIT 1''')).fetchone()
        if not job:
            return False
        if not await recent_conversation(conn, job['channel_id'], job['recipient']):
            await conn.execute("UPDATE whatsapp_alert_deliveries SET status='CANCELLED',error='Session expired' WHERE id=%s", (job['id'],))
            return True
        if not await reserve(conn, job['app_id'], 'whatsapp_sends', job['id']):
            await conn.execute("UPDATE whatsapp_alert_deliveries SET status='CANCELLED',error='Trial limit reached' WHERE id=%s", (job['id'],))
            return True
        await conn.execute("UPDATE whatsapp_alert_deliveries SET status='SENDING',updated_at=now() WHERE id=%s", (job['id'],))
    try:
        config = await managed_config(pool, job['phone_id'], job['channel_app'], job['channel_owner'])
        provider_id = await asyncio.wait_for(sender(config, job['phone_id'], job['recipient'], job['body']), 15)
    except Exception:  # noqa: BLE001 -- any provider error leaves the send outcome uncertain
        async with pool.connection() as conn:
            await conn.execute("UPDATE whatsapp_alert_deliveries SET status='UNKNOWN',error='Provider outcome uncertain; check before retry',updated_at=now() WHERE id=%s AND status='SENDING'", (job['id'],))
    else:
        async with pool.connection() as conn:
            await conn.execute("UPDATE whatsapp_alert_deliveries SET status='ACCEPTED',provider_id=%s,updated_at=now() WHERE id=%s AND status='SENDING'", (provider_id, job['id']))
    return True
