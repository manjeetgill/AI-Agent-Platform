"""Authenticated provider events, monotonic outcomes and app-scoped suppression."""
import base64
import hashlib
import hmac
import json
import os
import re
import time
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from fastapi import HTTPException, Request

from .automation import scope
from .email_config import digest, email_address
from .flags import enabled

RANK = {'UNCONFIRMED':0,'SENT':10,'DELAYED':20,'DELIVERED':30,'FAILED':40,
        'HARD_BOUNCED':50,'UNSUBSCRIBED':60,'COMPLAINED':70}
SUPPRESS = {'HARD_BOUNCED','UNSUBSCRIBED','COMPLAINED'}
RESEND = {'email.sent':'SENT','email.delivered':'DELIVERED','email.delivery_delayed':'DELAYED',
          'email.failed':'FAILED','email.suppressed':'FAILED','email.bounced':'HARD_BOUNCED','email.complained':'COMPLAINED'}
BREVO = {'request':'SENT','sent':'SENT','delivered':'DELIVERED','deferred':'DELAYED',
         'soft_bounce':'DELAYED','hard_bounce':'HARD_BOUNCED','spam':'COMPLAINED',
         'invalid_email':'FAILED','blocked':'FAILED','error':'FAILED','unsubscribed':'UNSUBSCRIBED'}


def recipient_hash(app_id, address):
    return digest({'purpose':'email-recipient','app':str(app_id),'email':email_address(address).lower()})


def message_id(provider, value):
    if not isinstance(value,str) or not re.fullmatch(r'[A-Za-z0-9_@.<>:+/=-]{1,300}',value):
        raise ValueError('Invalid provider identifier')
    return value.strip('<>') if provider=='brevo' else value


def webhook_key(app_id, provider):
    # Separate from send routing so old-provider callbacks survive a route switch.
    ref=json.loads(os.environ.get('AI_EMAIL_WEBHOOKS','{}'))[str(app_id)][provider]['key_env']
    if not isinstance(ref,str) or not re.fullmatch(r'[A-Z][A-Z0-9_]{1,100}',ref):
        raise ValueError()
    key=os.environ.get(ref,'')
    if any(c.isspace() for c in key):
        raise ValueError()
    if provider=='resend':
        if not key.startswith('whsec_') or len(base64.b64decode(key[6:],validate=True))<16:
            raise ValueError()
    elif provider!='brevo' or len(key)<32:
        raise ValueError()
    return key


def webhook_ready(app_id, provider):
    try:
        webhook_key(app_id,provider)
        return provider in {'resend','brevo'}
    except (KeyError,ValueError,TypeError,AttributeError):
        return False


def verify(provider,key,headers,raw):
    if provider=='brevo':
        if not hmac.compare_digest(headers.get('authorization','').encode(),('Bearer '+key).encode()):
            raise ValueError()
        return None
    identifier=headers.get('svix-id','');timestamp=headers.get('svix-timestamp','')
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,200}',identifier) or not timestamp.isdigit() or len(timestamp)>12 or abs(time.time()-int(timestamp))>300:
        raise ValueError()
    if not key.startswith('whsec_'):
        raise ValueError()
    secret=base64.b64decode(key[6:],validate=True)
    if len(secret)<16:
        raise ValueError()
    expected=base64.b64encode(hmac.new(secret,identifier.encode()+b'.'+timestamp.encode()+b'.'+raw,hashlib.sha256).digest()).decode()
    signatures=headers.get('svix-signature','')
    if len(signatures)>4096 or not any(hmac.compare_digest(s.encode(),('v1,'+expected).encode()) for s in signatures.split()):
        raise ValueError()
    return identifier


def normalize(provider, app_id, payload, event_key):
    if not isinstance(payload,dict):
        raise TypeError()
    if provider=='resend':
        outcome=RESEND.get(payload.get('type'))
        if not outcome:
            return None
        data=payload['data'];recipients=data['to']
        if not isinstance(recipients,list) or len(recipients)!=1:
            raise ValueError()  # Platform sends exactly one recipient per job.
        address=recipients[0];identifier=data['email_id']
        occurred=datetime.fromisoformat(payload['created_at'])
    else:
        outcome=BREVO.get(payload.get('event'))
        if not outcome:
            return None
        address=payload['email'];identifier=payload['message-id']
        occurred=datetime.fromtimestamp(float(payload['ts_event']),UTC)
    if occurred.tzinfo is None or occurred>datetime.now(UTC)+timedelta(minutes=5):
        raise ValueError()
    row={'provider_id':message_id(provider,identifier),'recipient_hash':recipient_hash(app_id,address),
         'outcome':outcome,'occurred_at':occurred}
    # Brevo id identifies the webhook configuration, not a unique delivery event.
    row['event_key']=event_key or digest({'purpose':'brevo-event','app':str(app_id),
        **{k:str(v) for k,v in row.items()}})
    return row


async def lock(conn):
    # Always acquire before job/event locks; no network requests under this lock.
    await conn.execute('SELECT pg_advisory_xact_lock(79342014)')


async def suppressed(conn, app_id, recipient):
    return bool(await (await conn.execute('SELECT 1 FROM notification_email_suppressions WHERE app_id=%s AND recipient_hash=%s',
                                         (app_id,recipient))).fetchone())


async def reconcile(conn, job):
    if not job['provider_id'] or job['provider']=='mock':
        return
    rows=await (await conn.execute('''SELECT * FROM notification_email_events
        WHERE app_id=%s AND provider=%s AND provider_id=%s AND job_id IS NULL ORDER BY occurred_at,id''',
        (job['app_id'],job['provider'],message_id(job['provider'],job['provider_id'])))).fetchall()
    for event in rows:
        if job['recipient_hash'] and event['recipient_hash']!=job['recipient_hash']:
            continue
        await conn.execute('UPDATE notification_email_events SET job_id=%s,owner_id=%s WHERE id=%s',
                           (job['id'],job['owner_id'],event['id']))
        outcome=event['outcome']
        if RANK[outcome]>RANK[job['delivery_status']]:
            await conn.execute('UPDATE notification_email_jobs SET delivery_status=%s,delivery_at=%s,updated_at=now() WHERE id=%s',
                               (outcome,event['occurred_at'],job['id']))
            job['delivery_status']=outcome
        if outcome in SUPPRESS:
            await conn.execute('''INSERT INTO notification_email_suppressions(tenant_id,app_id,recipient_hash,reason,event_id)
                VALUES(%s,%s,%s,%s,%s) ON CONFLICT(app_id,recipient_hash) DO NOTHING''',
                (job['tenant_id'],job['app_id'],event['recipient_hash'],outcome,event['id']))
            # Includes other owners in this application, never another app.
            await conn.execute('''UPDATE notification_email_jobs SET status=CASE WHEN may_have_sent THEN 'OUTCOME_UNKNOWN' ELSE 'SUPPRESSED' END,
                payload_cipher=NULL,failure_code='recipient_suppressed',updated_at=now()
                WHERE app_id=%s AND recipient_hash=%s AND status IN ('QUEUED','RETRY','CONFIGURATION_REQUIRED')''',
                (job['app_id'],event['recipient_hash']))


async def receive(pool, app_id, provider, events):
    async with pool.connection() as conn,conn.transaction():
        await scope(conn,worker=True)
        await lock(conn)
        app=await (await conn.execute('SELECT tenant_id FROM platform_apps WHERE id=%s',(app_id,))).fetchone()
        if not app:
            raise HTTPException(404,'Webhook not configured')
        await conn.execute("DELETE FROM notification_email_events WHERE job_id IS NULL AND received_at<now()-interval '7 days'")
        for event in events:
            existing=await (await conn.execute('SELECT 1 FROM notification_email_events WHERE app_id=%s AND provider=%s AND event_key=%s',
                                               (app_id,provider,event['event_key']))).fetchone()
            if existing:
                continue
            pending=await (await conn.execute('SELECT count(*) AS n FROM notification_email_events WHERE app_id=%s AND job_id IS NULL',(app_id,))).fetchone()
            if pending['n']>=10000:
                raise HTTPException(503,'Webhook reconciliation backlog is full')
            await conn.execute('''INSERT INTO notification_email_events(id,tenant_id,app_id,provider,event_key,provider_id,recipient_hash,outcome,occurred_at)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)''',(uuid4(),app['tenant_id'],app_id,provider,
                event['event_key'],event['provider_id'],event['recipient_hash'],event['outcome'],event['occurred_at']))
            # Brackets differ between Brevo API responses and callbacks.
            jobs=await (await conn.execute('''SELECT * FROM notification_email_jobs WHERE app_id=%s AND provider=%s
                AND trim(both '<>' from provider_id)=%s FOR UPDATE''',(app_id,provider,event['provider_id']))).fetchall()
            if len(jobs)==1:
                await reconcile(conn,jobs[0])


def install(app):
    @app.post('/v1/notifications/email/webhooks/{app_id}/{provider}',include_in_schema=False)
    async def webhook(app_id:UUID,provider:str,request:Request):
        if not enabled('AI_EMAIL_NOTIFICATIONS') or provider not in {'resend','brevo'}:
            raise HTTPException(404,'Webhook not configured')
        try:
            key=webhook_key(app_id,provider)
        except (KeyError,ValueError,TypeError,AttributeError):
            raise HTTPException(404,'Webhook not configured') from None
        raw=bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw)>65536:
                raise HTTPException(413,'Webhook too large')
        try:
            event_key=verify(provider,key,request.headers,bytes(raw))
        except (ValueError,TypeError):
            raise HTTPException(401,'Webhook authentication failed') from None
        try:
            payload=json.loads(raw)
            batch=payload if isinstance(payload,list) and provider=='brevo' else [payload]
            if not 1<=len(batch)<=100:
                raise ValueError()
            events=[event for item in batch if (event:=normalize(provider,app_id,item,event_key))]
        except (ValueError,TypeError,KeyError,AttributeError,OverflowError):
            raise HTTPException(400,'Invalid webhook event') from None
        try:
            from .db_scope import connection_identity
            await connection_identity(request.app.state.repository.pool,'email',app_id)
            await receive(request.app.state.repository.pool,app_id,provider,events)
        except HTTPException:
            raise
        except Exception:  # noqa: BLE001 -- never echo provider payloads or secrets
            raise HTTPException(503,'Webhook processing unavailable; retry later') from None
        return {'received':True}
