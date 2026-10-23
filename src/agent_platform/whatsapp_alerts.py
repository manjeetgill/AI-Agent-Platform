"""App-scoped event delivery through an owned or operator-approved WhatsApp sender.

Only recipients who verified their number, opted in, and recently messaged
that sender can receive session text. A future approved-template path is required outside that window.
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
from pydantic import Field, field_validator

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


class RouteConfiguration(Strict):
    channel_id: UUID
    daily_limit: int | None = Field(default=20, ge=1, le=1000)
    template_name: str | None = Field(default=None, min_length=1, max_length=128)
    template_language: str = Field(default='en_US', min_length=2, max_length=10)


class NotificationDelivery(Strict):
    request_key: str = Field(min_length=8, max_length=100)
    category: str = Field(pattern=r'^[a-z][a-z0-9_.-]{0,49}$')
    title: str = Field(min_length=1, max_length=80)
    summary: str = Field(min_length=1, max_length=500)
    reference: str | None = Field(default=None, min_length=1, max_length=100)
    occurred_at: datetime
    test_mode: bool = False

    @field_validator('title', 'summary', 'reference')
    @classmethod
    def single_line(cls, value):
        if value is None:
            return None
        cleaned = ' '.join(value.split())
        if not cleaned:
            raise ValueError('must contain visible text')
        return cleaned

    @field_validator('occurred_at')
    @classmethod
    def timezone_required(cls, value):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError('timezone is required')
        return value


class AlertDelivery(Strict):
    request_key: str = Field(min_length=8, max_length=100)
    exchange: Literal['NSE', 'BSE']
    # Exchange index names contain spaces (for example NIFTY 50 and NIFTY BANK).
    symbol: str = Field(min_length=1, max_length=120, pattern=r'^[A-Z0-9&_.-]+(?: [A-Z0-9&_.-]+)*$')
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


def notification_message(app_name: str, notification: NotificationDelivery) -> str:
    moment = notification.occurred_at.astimezone(timezone(timedelta(hours=5, minutes=30)))
    name = ' '.join(app_name.split())[:80] or 'App'
    trade = notification.category in {
        'trade.placed', 'trade.executed', 'trade.paper.placed', 'trade.paper.executed',
    }
    label = name + (' TEST notification' if notification.test_mode else '' if trade else ' notification')
    title = f'*{notification.title}*' if trade else notification.title
    parts = [label, title, notification.summary]
    if notification.reference:
        parts.append('Reference: ' + notification.reference)
    parts.extend((f'Time: {moment:%d %b %Y, %I:%M %p} IST',
                  f'Open {name} for trade details.' if trade else
                  'Open the app for details. This message cannot make a payment or complete a transaction.'))
    return '\n'.join(parts)


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
            route = await (await conn.execute('''SELECT r.*,c.phone_id,c.owner_id AS channel_owner,
                a.name AS app_name FROM whatsapp_alert_routes r
                JOIN whatsapp_channels c ON c.id=r.channel_id JOIN platform_apps a ON a.id=r.app_id
                WHERE r.app_id=%s AND r.enabled AND c.enabled AND c.webhook_verified_at IS NOT NULL''',
                (claims['app_id'],))).fetchone()
        if not route:
            raise HTTPException(503, 'Alert sender is not configured for this app')
        return pool, claims, route

    async def manage_context(request, scope):
        pool = request.app.state.repository.pool
        claims = await authenticate(pool, request.headers.get('authorization', ''),
                                    request.headers.get('X-External-User-ID'), scope)
        return pool, claims

    @router.get('/capabilities')
    async def alert_capabilities(request: Request):
        pool, claims = await manage_context(request, 'alerts:send')
        async with pool.connection() as conn:
            route = await (await conn.execute('''SELECT template_name FROM whatsapp_alert_routes
                WHERE app_id=%s AND enabled''', (claims['app_id'],))).fetchone()
        has_template = bool(route and route.get('template_name'))
        return {'recipient_verification': 'inbound_one_time_code', 'verification_ttl_seconds': 600,
                'generic_notifications': True, 'market_alerts': True, 'approved_templates': True,
                'requires_recent_conversation': not has_template, 'payment_actions': False,
                'sender_ownership': 'consuming_app_or_operator_approved_shared_route'}

    @router.put('/route')
    async def configure_route(body: RouteConfiguration, request: Request):
        pool, claims = await manage_context(request, 'channels:write')
        async with pool.connection() as conn, conn.transaction():
            channel = await (await conn.execute('''SELECT id,display_number FROM whatsapp_channels
                WHERE id=%s AND app_id=%s AND owner_id=%s AND enabled
                AND webhook_verified_at IS NOT NULL FOR UPDATE''',
                (body.channel_id, claims['app_id'], claims['user']))).fetchone()
            if not channel:
                raise HTTPException(409, 'Enable and verify your owned WhatsApp channel first')
            if not channel['display_number']:
                raise HTTPException(409, 'Send one test message so the signed webhook can confirm the sender number')
            previous = await (await conn.execute('''SELECT channel_id FROM whatsapp_alert_routes
                WHERE app_id=%s FOR UPDATE''', (claims['app_id'],))).fetchone()
            if previous and previous['channel_id'] != channel['id']:
                await conn.execute('''UPDATE whatsapp_alert_deliveries SET status='CANCELLED',
                    error='Sender route changed',updated_at=now()
                    WHERE app_id=%s AND status='QUEUED' ''', (claims['app_id'],))
                await conn.execute('''UPDATE whatsapp_alert_subscriptions SET active=false
                    WHERE app_id=%s''', (claims['app_id'],))
                await conn.execute('''UPDATE whatsapp_alert_verifications SET state='CANCELLED'
                    WHERE app_id=%s AND state IN ('PENDING','VERIFIED')''', (claims['app_id'],))
            await conn.execute('''INSERT INTO whatsapp_alert_routes
                (app_id,channel_id,sender_number,enabled,daily_limit,require_recipient_verification,
                 template_name,template_language)
                VALUES(%s,%s,%s,true,%s,true,%s,%s) ON CONFLICT(app_id) DO UPDATE SET
                channel_id=EXCLUDED.channel_id,sender_number=EXCLUDED.sender_number,enabled=true,
                daily_limit=EXCLUDED.daily_limit,require_recipient_verification=true,
                template_name=EXCLUDED.template_name,template_language=EXCLUDED.template_language''',
                (claims['app_id'], channel['id'], channel['display_number'], body.daily_limit,
                 body.template_name, body.template_language))
        return {'channel_id': channel['id'], 'sender_number': channel['display_number'],
                'daily_limit': body.daily_limit, 'enabled': True, 'recipient_verification': True,
                'template_name': body.template_name, 'template_language': body.template_language}

    @router.get('/route')
    async def route_status(request: Request):
        pool, claims = await manage_context(request, 'channels:read')
        async with pool.connection() as conn:
            row = await (await conn.execute('''SELECT r.channel_id,r.sender_number,r.enabled,
                r.daily_limit,r.require_recipient_verification,r.template_name,r.template_language
                FROM whatsapp_alert_routes r
                JOIN whatsapp_channels c ON c.id=r.channel_id WHERE r.app_id=%s
                AND c.app_id=%s AND c.owner_id=%s''',
                (claims['app_id'], claims['app_id'], claims['user']))).fetchone()
        if not row:
            raise HTTPException(404, 'Owned notification route not found')
        return row

    @router.delete('/route', status_code=204)
    async def disable_route(request: Request):
        pool, claims = await manage_context(request, 'channels:write')
        async with pool.connection() as conn, conn.transaction():
            row = await (await conn.execute('''UPDATE whatsapp_alert_routes r SET enabled=false
                FROM whatsapp_channels c WHERE r.app_id=%s AND c.id=r.channel_id
                AND c.app_id=%s AND c.owner_id=%s RETURNING r.app_id''',
                (claims['app_id'], claims['app_id'], claims['user']))).fetchone()
            if not row:
                raise HTTPException(404, 'Owned notification route not found')
            await conn.execute('''UPDATE whatsapp_alert_deliveries SET status='CANCELLED',
                error='Sender route disabled',updated_at=now()
                WHERE app_id=%s AND status='QUEUED' ''', (claims['app_id'],))
            await conn.execute('UPDATE whatsapp_alert_subscriptions SET active=false WHERE app_id=%s',
                               (claims['app_id'],))
            await conn.execute('''UPDATE whatsapp_alert_verifications SET state='CANCELLED'
                WHERE app_id=%s AND state IN ('PENDING','VERIFIED')''', (claims['app_id'],))

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
            # A signed provider webhook may persist before its verification update is
            # observed. Recover only from that recipient's exact, timely inbound code.
            await conn.execute('''UPDATE whatsapp_alert_verifications v
                SET state='VERIFIED',verified_at=now()
                WHERE v.id=%s AND v.app_id=%s AND v.owner_id=%s
                AND v.state='PENDING' AND v.expires_at>now()
                AND EXISTS (SELECT 1 FROM whatsapp_conversations c
                    JOIN whatsapp_messages m ON m.conversation_id=c.id
                    WHERE c.channel_id=v.channel_id AND c.customer=v.recipient
                    AND m.received_at BETWEEN v.created_at AND v.expires_at
                    AND encode(sha256(convert_to(upper(btrim(m.body)),'UTF8')),'hex')=v.code_hash)''',
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
            if not route.get('template_name') and not await recent_conversation(conn, route['channel_id'], recipient):
                from .whatsapp_opt_in import subscription_blocker
                raise HTTPException(409, await subscription_blocker(conn, route['channel_id'], recipient, route['sender_number']))
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
            if not route.get('template_name') and not await recent_conversation(conn, route['channel_id'], subscription['recipient']):
                raise HTTPException(409, 'WhatsApp session expired; an approved alert template is required')
            count = await (await conn.execute('''SELECT count(*) AS n FROM whatsapp_alert_deliveries
                WHERE app_id=%s AND created_at>now()-interval '24 hours' ''', (claims['app_id'],))).fetchone()
            if route['daily_limit'] is not None and count['n'] >= route['daily_limit']:
                raise HTTPException(429, 'Daily WhatsApp alert limit reached')
            ident = uuid4()
            await conn.execute('''INSERT INTO whatsapp_alert_deliveries
                (id,app_id,owner_id,channel_id,request_key,recipient,body,status)
                VALUES(%s,%s,%s,%s,%s,%s,%s,'QUEUED')''',
                (ident, claims['app_id'], claims['user'], route['channel_id'], body.request_key,
                 subscription['recipient'], content))
        return {'id': ident, 'status': 'QUEUED'}

    @router.post('/notifications', status_code=202)
    async def notify(body: NotificationDelivery, request: Request):
        pool, claims, route = await context(request)
        content = notification_message(route['app_name'], body)
        async with pool.connection() as conn, conn.transaction():
            await conn.execute('SELECT app_id FROM whatsapp_alert_routes WHERE app_id=%s FOR UPDATE',
                               (claims['app_id'],))
            prior = await (await conn.execute('''SELECT id,status,body FROM whatsapp_alert_deliveries
                WHERE app_id=%s AND request_key=%s''',
                (claims['app_id'], body.request_key))).fetchone()
            if prior:
                if prior['body'] != content:
                    raise HTTPException(409, 'Idempotency key belongs to a different notification')
                return {'id': prior['id'], 'status': prior['status']}
            subscription = await (await conn.execute('''SELECT * FROM whatsapp_alert_subscriptions
                WHERE app_id=%s AND owner_id=%s AND active FOR UPDATE''',
                (claims['app_id'], claims['user']))).fetchone()
            if not subscription:
                raise HTTPException(409, 'Enable WhatsApp notifications first')
            if not route.get('template_name') and not await recent_conversation(conn, route['channel_id'], subscription['recipient']):
                raise HTTPException(409, 'WhatsApp session expired; an approved template is required')
            count = await (await conn.execute('''SELECT count(*) AS n FROM whatsapp_alert_deliveries
                WHERE app_id=%s AND created_at>now()-interval '24 hours' ''',
                (claims['app_id'],))).fetchone()
            if route['daily_limit'] is not None and count['n'] >= route['daily_limit']:
                raise HTTPException(429, 'Daily WhatsApp notification limit reached')
            ident = uuid4()
            await conn.execute('''INSERT INTO whatsapp_alert_deliveries
                (id,app_id,owner_id,channel_id,request_key,recipient,body,status,kind,category)
                VALUES(%s,%s,%s,%s,%s,%s,%s,'QUEUED','notification',%s)''',
                (ident, claims['app_id'], claims['user'], route['channel_id'], body.request_key,
                 subscription['recipient'], content, body.category))
        return {'id': ident, 'status': 'QUEUED'}

    @router.get('/deliveries/{delivery_id}')
    async def status(delivery_id: UUID, request: Request):
        pool, claims, _ = await context(request)
        async with pool.connection() as conn:
            row = await (await conn.execute('''SELECT id,status,delivery,error,created_at,updated_at,
                kind,category FROM whatsapp_alert_deliveries
                WHERE id=%s AND app_id=%s AND owner_id=%s''',
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
        job = await (await conn.execute('''SELECT d.*,c.phone_id,c.app_id AS channel_app,c.owner_id AS channel_owner,
            c.provider AS channel_provider,
            r.template_name,r.template_language
            FROM whatsapp_alert_deliveries d JOIN whatsapp_channels c ON c.id=d.channel_id
            JOIN whatsapp_alert_routes r ON r.app_id=d.app_id AND r.channel_id=d.channel_id
            JOIN whatsapp_alert_subscriptions s ON s.app_id=d.app_id AND s.owner_id=d.owner_id
            WHERE d.status='QUEUED' AND r.enabled AND c.enabled AND s.active
            AND s.recipient=d.recipient ORDER BY d.created_at FOR UPDATE OF d SKIP LOCKED LIMIT 1''')).fetchone()
        if not job:
            return False
        has_recent = await recent_conversation(conn, job['channel_id'], job['recipient'])
        if not has_recent and not job.get('template_name'):
            await conn.execute("UPDATE whatsapp_alert_deliveries SET status='CANCELLED',error='Session expired' WHERE id=%s", (job['id'],))
            return True
        if not await reserve(conn, job['app_id'], 'whatsapp_sends', job['id']):
            await conn.execute("UPDATE whatsapp_alert_deliveries SET status='CANCELLED',error='Trial limit reached' WHERE id=%s", (job['id'],))
            return True
        await conn.execute("UPDATE whatsapp_alert_deliveries SET status='SENDING',updated_at=now() WHERE id=%s", (job['id'],))
    try:
        config = await managed_config(pool, job['phone_id'], job['channel_app'], job['channel_owner'], provider=job.get('channel_provider'))
        if not has_recent and job.get('template_name'):
            if job['template_name'] == 'nraialgo_trading_alert':
                lines = [line.strip() for line in job['body'].splitlines() if line.strip()]
                title = lines[0] if lines else 'Trading Alert'
                details = '\n'.join(lines[1:]) if len(lines) > 1 else job['body']
                ref = str(job.get('request_key') or job['id'])[:100]
                parameters = [
                    {"type": "text", "text": title[:1024]},
                    {"type": "text", "text": details[:1024]},
                    {"type": "text", "text": ref[:1024]},
                ]
            else:
                parameters = [{"type": "text", "text": job["body"]}]
            payload = {
                "messaging_product": "whatsapp",
                "to": job["recipient"],
                "type": "template",
                "template": {
                    "name": job["template_name"],
                    "language": {"code": job.get("template_language") or "en_US"},
                    "components": [
                        {
                            "type": "body",
                            "parameters": parameters,
                        }
                    ]
                }
            }
            body_or_payload = payload
        else:
            body_or_payload = job['body']
        provider_id = await asyncio.wait_for(sender(config, job['phone_id'], job['recipient'], body_or_payload), 15)
    except Exception:  # noqa: BLE001 -- any provider error leaves the send outcome uncertain
        async with pool.connection() as conn:
            await conn.execute("UPDATE whatsapp_alert_deliveries SET status='UNKNOWN',error='Provider outcome uncertain; check before retry',updated_at=now() WHERE id=%s AND status='SENDING'", (job['id'],))
    else:
        async with pool.connection() as conn:
            await conn.execute("UPDATE whatsapp_alert_deliveries SET status='ACCEPTED',provider_id=%s,updated_at=now() WHERE id=%s AND status='SENDING'", (provider_id, job['id']))
    return True
