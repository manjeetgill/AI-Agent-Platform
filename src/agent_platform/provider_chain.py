"""Explicit provider routing with atomic Postgres reservations and UTC daily limits.
Amounts are conservative USD estimates in millionths, not provider invoices.
Unknown outcomes retain their full reservation; no uncertain request is retried.
"""
import json
import math
import os
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from .db_scope import identity, model_identity, scope
from .runtime_errors import RunFailure


class Entry(BaseModel):
    model_config = ConfigDict(extra='forbid')
    provider: str
    model: str = Field(min_length=1,max_length=200)
    key_env: str = Field(pattern=r'^[A-Z][A-Z0-9_]+$')
    daily_budget_microusd: int = Field(ge=0)
    # Conservative maximum of input/output rates; explicitly supplied by operator.
    microusd_per_token: float = Field(gt=0,allow_inf_nan=False)
    sensitivity: list[str] = Field(min_length=1)
    cooldown_seconds: int = Field(default=60,ge=1,le=3600)


def configuration():
    try:
        entries=[Entry.model_validate(x) for x in json.loads(os.environ['AI_MODEL_PROVIDER_CHAIN'])]
        if not 1<=len(entries)<=8 or len({x.provider for x in entries})!=len(entries):
            raise ValueError()
        return entries
    except (ValueError,TypeError,KeyError):
        raise RunFailure('INVALID_PROVIDER_CHAIN') from None


async def reserve(pool,claims,entry,amount):
    app=claims.get('app_id')
    if not app:
        raise RunFailure('MODEL_APP_ID_REQUIRED')
    async with pool.connection() as conn,conn.transaction():
        await scope(conn,claims)
        # Serialize a provider across apps before taking any app/usage row lock.
        await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",('agent-provider:'+entry.provider,))
        # One lock serializes all provider reservations for an app, including its total.
        plan=await (await conn.execute('SELECT * FROM platform_model_policies WHERE app_id=%s FOR UPDATE',(app,))).fetchone()
        if not plan:
            raise RunFailure('MODEL_APP_POLICY_REQUIRED')
        if entry.provider not in plan['allowed_providers'] or plan['sensitivity'] not in entry.sensitivity:
            return None
        day=(await (await conn.execute("SELECT (now() AT TIME ZONE 'UTC')::date AS day")).fetchone())['day']
        await conn.execute('INSERT INTO provider_chain_usage(app_id,day,provider) VALUES(%s,%s,%s) ON CONFLICT DO NOTHING',(app,day,entry.provider))
        await conn.execute('SELECT 1 FROM provider_chain_usage WHERE app_id=%s AND day=%s AND provider=%s FOR UPDATE',(app,day,entry.provider))
        total=await (await conn.execute('SELECT coalesce(sum(calls),0) AS calls,coalesce(sum(reserved_microusd),0) AS cost FROM provider_chain_usage WHERE app_id=%s AND day=%s',(app,day))).fetchone()
        # Cooldowns survive a midnight rollover (usage quotas reset, backoff does not).
        provider=await (await conn.execute('SELECT * FROM agent_provider_quota(%s,%s)',(entry.provider,day))).fetchone()
        if (provider['cooling'] or total['calls']>=plan['daily_calls'] or total['cost']+amount>plan['daily_budget_microusd']
                or provider['reserved_microusd']+amount>entry.daily_budget_microusd):
            return None
        ident=uuid4()
        await conn.execute('UPDATE provider_chain_usage SET calls=calls+1,reserved_microusd=reserved_microusd+%s WHERE app_id=%s AND day=%s AND provider=%s',(amount,app,day,entry.provider))
        await conn.execute('INSERT INTO provider_chain_attempts(id,app_id,provider,model,day,reserved_microusd) VALUES(%s,%s,%s,%s,%s,%s)',(ident,app,entry.provider,entry.model,day,amount))
        return ident


async def finish(pool,claims,ident,entry,*,tokens=None,status='UNKNOWN',cooldown=False):
    async with pool.connection() as conn,conn.transaction():
        await scope(conn,claims)
        row=await (await conn.execute('SELECT * FROM provider_chain_attempts WHERE id=%s FOR UPDATE',(ident,))).fetchone()
        if row['status']!='RESERVED':
            return
        estimate=math.ceil(tokens*entry.microusd_per_token) if tokens is not None and tokens>0 else None
        charged=estimate if estimate is not None else row['reserved_microusd']
        await conn.execute('UPDATE provider_chain_usage SET reserved_microusd=reserved_microusd+%s,cooldown_until=CASE WHEN %s THEN now()+(%s*interval \'1 second\') ELSE cooldown_until END WHERE app_id=%s AND day=%s AND provider=%s',
            (charged-row['reserved_microusd'],cooldown,entry.cooldown_seconds,row['app_id'],row['day'],entry.provider))
        await conn.execute('UPDATE provider_chain_attempts SET status=%s,total_tokens=%s,estimated_microusd=%s,finished_at=now() WHERE id=%s',(status,tokens,estimate,ident))


async def execute(gateway,task,policy,data,schema,validator,claims=None):
    from .gateway import FallbackProvider, Gateway, NVIDIAProvider
    claims=claims or identity.get() or model_identity.get()
    if not gateway.pool or not claims:
        raise RunFailure('MODEL_BUDGET_STORAGE_REQUIRED')
    context_bytes=len(policy.encode())+len(json.dumps(data,default=str).encode())
    if context_bytes>int(os.environ.get('AI_MODEL_MAX_CONTEXT_BYTES','100000')):
        raise RunFailure('CONTEXT_BUDGET_EXCEEDED')
    for entry in configuration():
        if entry.provider not in {'nvidia',*FallbackProvider.ENDPOINTS}:
            raise RunFailure('INVALID_PROVIDER_CHAIN')
        key=os.environ.get(entry.key_env,'').strip()
        if not key:
            # No silent substitute for an unconfigured entry.
            raise RunFailure('CHAIN_PROVIDER_NOT_CONFIGURED')
        reserve_cost=math.ceil((context_bytes+8192)*entry.microusd_per_token)
        ident=await reserve(gateway.pool,claims,entry,reserve_cost)
        if ident is None:
            continue
        provider=NVIDIAProvider(key) if entry.provider=='nvidia' else FallbackProvider(entry.provider,key)
        child=Gateway(provider,client=gateway.client,max_attempts=1,allow_fallback=False,allow_chain=False)
        child.fast_model=child.reasoning_model=entry.model
        child.pinned_model=entry.model
        child.chain_attempt=True
        try:
            result,tokens,name,model=await child.structured(task,policy,data,schema,validator)
        except RunFailure as exc:
            await finish(gateway.pool,claims,ident,entry,status=exc.code,cooldown=exc.code=='PROVIDER_BACKPRESSURE')
            if exc.code=='PROVIDER_BACKPRESSURE':
                continue
            raise
        except BaseException:
            # Cancellation/crash retains RESERVED cost. Never blindly replay it.
            raise
        await finish(gateway.pool,claims,ident,entry,tokens=tokens,status='COMPLETED')
        return result,tokens,name,model
    raise RunFailure('MODEL_CHAIN_EXHAUSTED')
