"""Atomic SMS outbox, encrypted transient payloads, quotas and crash-safe attempts."""
import asyncio
import random
from datetime import timedelta
from uuid import uuid4

from fastapi import HTTPException

from .automation import scope
from .email_config import digest, seal, unseal
from .sms_config import api_key, config, global_limits, render
from .sms_providers import ADAPTERS, Outcome

PUBLIC = 'id,template,template_version,provider,status,attempts,provider_id,failure_code,next_at,expires_at,created_at,updated_at,segments,provider_job_id'
TERMINAL = {'MOCK_SENT','ACCEPTED','FAILED','OUTCOME_UNKNOWN','EXPIRED'}


async def counts(conn, table, date_column, app_id=None):
    # Identifiers are constants supplied only by this module.
    return await (await conn.execute('SELECT * FROM agent_sms_counts(%s,%s)',(table,app_id))).fetchone()


def exceeded(used, limits, *, minute=False, units=1, no_volume_caps=False):
    volume = not no_volume_caps and (used['daily']+units>limits.daily or used['monthly']+units>limits.monthly)
    return volume or (minute and used['per_minute']+units>limits.per_minute)


async def enqueue(pool, claims, body):
    settings=config(claims['app_id'])
    fingerprint=digest(body.model_dump(mode='json'))
    payload=render(body,settings)
    row={'id':uuid4(),'tenant_id':claims['tenant'],'app_id':claims['app_id'],'owner_id':claims['user']}
    cipher=seal(payload,row)
    async with pool.connection() as conn, conn.transaction():
        await scope(conn, worker=True)  # Explicit app/owner predicates; aggregate quotas span owners.
        await conn.execute("SELECT pg_advisory_xact_lock(79342022)")
        prior=await (await conn.execute(f'''SELECT {PUBLIC},payload_hash FROM notification_sms_jobs
            WHERE app_id=%s AND owner_id=%s AND request_key=%s''',
            (claims['app_id'],claims['user'],body.idempotency_key))).fetchone()
        if prior:
            if prior.pop('payload_hash')!=fingerprint:
                raise HTTPException(409,'Idempotency key belongs to different SMS content')
            return prior
        api_key(settings)  # No missing-key fallback to mock or another provider.
        for app,limits in [(claims['app_id'],settings.limits),(None,global_limits())]:
            if exceeded(await counts(conn,'notification_sms_jobs','created_at',app),limits,units=payload['segments'],no_volume_caps=settings.testing_no_volume_caps):
                raise HTTPException(429,'SMS segment request limit reached; no message queued')
        return await (await conn.execute(f'''INSERT INTO notification_sms_jobs
            (id,tenant_id,app_id,owner_id,request_key,payload_hash,payload_cipher,template,provider,config_hash,expires_at,segments)
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,now()+%s*interval '1 second',%s) RETURNING {PUBLIC}''',
            (row['id'],row['tenant_id'],row['app_id'],row['owner_id'],body.idempotency_key,fingerprint,cipher,
             body.template,settings.provider,settings.fingerprint,body.expires_in_seconds,payload['segments']))).fetchone()


async def recover(conn):
    await conn.execute("""UPDATE notification_sms_attempts a SET outcome='OUTCOME_UNKNOWN',failure_code='worker_interrupted',finished_at=now()
        FROM notification_sms_jobs j WHERE a.job_id=j.id AND a.attempt=j.attempts
        AND j.status='SENDING' AND j.lease_until<now()""")
    await conn.execute("""UPDATE notification_sms_jobs SET status='OUTCOME_UNKNOWN',payload_cipher=NULL,
        may_have_sent=true,failure_code='worker_interrupted',lease_token=NULL,lease_until=NULL,updated_at=now()
        WHERE status='SENDING' AND lease_until<now()""")
    await conn.execute('''UPDATE notification_sms_jobs SET status=CASE WHEN may_have_sent THEN 'OUTCOME_UNKNOWN' ELSE 'EXPIRED' END,
        payload_cipher=NULL,failure_code='notification_expired',updated_at=now()
        WHERE status IN ('QUEUED','RETRY','CONFIGURATION_REQUIRED') AND expires_at<=now()''')


async def claim(pool):
    async with pool.connection() as conn, conn.transaction():
        await scope(conn,worker=True)
        await conn.execute('SELECT pg_advisory_xact_lock(79342022)')
        await recover(conn)
        row=await (await conn.execute('''SELECT * FROM notification_sms_jobs WHERE status IN ('QUEUED','RETRY','CONFIGURATION_REQUIRED')
            AND next_at<=now() ORDER BY next_at,created_at FOR UPDATE SKIP LOCKED LIMIT 1''')).fetchone()
        if not row:
            return None
        try:
            settings=config(row['app_id'])
            api_key(settings)
            limits=global_limits()
            if settings.fingerprint!=row['config_hash']:
                raise ValueError('Configuration changed')
            payload=unseal(row['payload_cipher'],row)
        except Exception:  # noqa: BLE001 -- never expose credentials, ciphertext or payloads
            await conn.execute("""UPDATE notification_sms_jobs SET status='CONFIGURATION_REQUIRED',failure_code='configuration_required',
                next_at=now()+interval '5 minutes',updated_at=now() WHERE id=%s""",(row['id'],))
            return False
        for app,budget in [(row['app_id'],settings.limits),(None,limits)]:
            if exceeded(await counts(conn,'notification_sms_attempts','started_at',app),budget,minute=True,units=row['segments'],no_volume_caps=settings.testing_no_volume_caps):
                await conn.execute("""UPDATE notification_sms_jobs SET status='RETRY',failure_code='usage_limit_wait',
                    next_at=now()+interval '1 minute',updated_at=now() WHERE id=%s""",(row['id'],))
                return False
        token=uuid4()
        claimed=await (await conn.execute('''UPDATE notification_sms_jobs SET status='SENDING',attempts=attempts+1,
            lease_token=%s,lease_until=now()+interval '60 seconds',first_attempt_at=coalesce(first_attempt_at,now()),updated_at=now()
            WHERE id=%s RETURNING *''',(token,row['id']))).fetchone()
        await conn.execute('''INSERT INTO notification_sms_attempts(job_id,tenant_id,app_id,owner_id,attempt,provider,segments)
            VALUES(%s,%s,%s,%s,%s,%s,%s)''',(row['id'],row['tenant_id'],row['app_id'],row['owner_id'],claimed['attempts'],row['provider'],row['segments']))
        return claimed,settings,payload


async def finish(pool, row, outcome):
    async with pool.connection() as conn, conn.transaction():
        await scope(conn,worker=True)
        live=await (await conn.execute("SELECT *,now() AS clock FROM notification_sms_jobs WHERE id=%s AND lease_token=%s AND status='SENDING' FOR UPDATE",
                                     (row['id'],row['lease_token']))).fetchone()
        if not live:
            return
        uncertain=live['may_have_sent'] or outcome.uncertain
        status=outcome.status
        delay=max(outcome.retry_after,min(600,30*2**(live['attempts']-1))+random.randint(0,10))
        code=outcome.code
        if status=='FAILED' and uncertain:
            status='OUTCOME_UNKNOWN'
        if status=='RETRY' and (live['attempts']>=5 or live['clock']+timedelta(seconds=delay)>=live['expires_at']):
            status='OUTCOME_UNKNOWN' if uncertain else 'FAILED'
            code='retry_limit_or_expiry'
        await conn.execute('''UPDATE notification_sms_jobs SET status=%s,may_have_sent=%s,provider_id=%s,provider_job_id=%s,failure_code=%s,
            next_at=now()+%s*interval '1 second',lease_token=NULL,lease_until=NULL,updated_at=now(),
            payload_cipher=CASE WHEN %s THEN NULL ELSE payload_cipher END WHERE id=%s''',
            (status,uncertain,outcome.provider_id,outcome.provider_job_id,code,delay,status in TERMINAL,live['id']))
        await conn.execute('''UPDATE notification_sms_attempts SET finished_at=now(),outcome=%s,failure_code=%s,
            http_status=%s,provider_id=%s,provider_job_id=%s WHERE job_id=%s AND attempt=%s''',
            (status,code,outcome.http_status,outcome.provider_id,outcome.provider_job_id,live['id'],live['attempts']))


async def tick(pool, *, transport=None):
    item=await claim(pool)
    if not item:
        return item is False
    row,settings,payload=item
    try:
        outcome=await ADAPTERS[row['provider']].send(settings,payload,str(row['id']),transport=transport)
    except asyncio.CancelledError:
        await asyncio.shield(finish(pool,row,Outcome('OUTCOME_UNKNOWN','worker_stopped',uncertain=True)))
        raise
    except Exception:  # noqa: BLE001 -- provider exceptions can embed tokens or recipient data
        outcome=Outcome('OUTCOME_UNKNOWN','unexpected_provider_error',uncertain=True)
    await finish(pool,row,outcome)
    return True
