"""Owner-scoped orchestration APIs. Scope is derived only from authenticated apps."""
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from psycopg.types.json import Jsonb
from pydantic import Field

from .automation import (
    CreateAutomation,
    EventInput,
    Lifecycle,
    PreferenceInput,
    RegisterAgent,
    bounded,
    emit,
    owned_agent,
    scope,
)
from .flags import enabled
from .service_identity import authenticate
from .task_agents import digest, validate
from .task_api import credential_contract
from .tasks import ApprovalInput, Strict, TaskError


class Control(Strict):
    status: Lifecycle


class PreferenceNamespace(Strict):
    namespace: str = Field(pattern=r'^[a-zA-Z][a-zA-Z0-9_.-]{0,99}$')


class WebhookInput(Strict):
    destination: str = Field(pattern=r'^[a-zA-Z][a-zA-Z0-9_-]{0,79}$')
    event_type: str = Field(pattern=r'^[A-Za-z][A-Za-z0-9_.-]{0,99}$')
    event_version: int = Field(default=1, ge=1, le=100)
    decision: str = Field(pattern=r'^yes$')


def install_automation(app, registry):
    router = APIRouter(prefix='/v1/orchestration', dependencies=[Depends(credential_contract)])

    async def context(request, authorization, permission):
        if not enabled('AI_AUTOMATIONS'):
            raise HTTPException(503, 'Orchestration is disabled')
        pool = getattr(request.app.state.repository, 'pool', None)
        if pool is None:
            raise HTTPException(503, 'Durable storage unavailable')
        claims = await authenticate(pool, authorization, request.headers.get('X-External-User-ID'), permission)
        return pool, claims

    async def owned(conn, claims, automation_id):
        row = await (await conn.execute('''SELECT * FROM automations WHERE id=%s AND app_id=%s
            AND owner_id=%s FOR UPDATE''', (automation_id, claims['app_id'], claims['user']))).fetchone()
        if not row:
            raise HTTPException(404, 'Automation not found')
        return row

    @router.post('/agents', status_code=201)
    async def register(body: RegisterAgent, request: Request, authorization: str | None = Header(default=None)):
        pool, claims = await context(request, authorization, 'agents:write')
        manifest = body.manifest.model_dump(mode='json')
        bounded(manifest, 16000)
        fingerprint = digest({'manifest': manifest, 'status': body.status})
        async with pool.connection() as conn, conn.transaction():
            await scope(conn, claims)
            grants = {r['tool'] for r in await (await conn.execute(
                'SELECT tool FROM platform_tool_grants WHERE app_id=%s', (claims['app_id'],))).fetchall()}
            try:
                validate(body.manifest.blueprint, registry, grants)
            except (TaskError, ValueError):
                raise HTTPException(422, 'Unavailable tool, invalid arguments or changed version') from None
            row = await (await conn.execute('''INSERT INTO task_agents
                (id,tenant_id,owner_id,app_id,blueprint,request_key,definition_hash,manifest,version,status)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT(app_id,owner_id,request_key) DO NOTHING RETURNING *''',
                (uuid4(), claims['tenant'], claims['user'], claims['app_id'], Jsonb(manifest['blueprint']),
                 body.idempotency_key, fingerprint, Jsonb(manifest), body.manifest.version, body.status))).fetchone()
            if not row:
                row = await (await conn.execute('SELECT * FROM task_agents WHERE app_id=%s AND owner_id=%s AND request_key=%s',
                    (claims['app_id'], claims['user'], body.idempotency_key))).fetchone()
                if row['definition_hash'] != fingerprint:
                    raise HTTPException(409, 'Idempotency key belongs to a different manifest')
            return row

    @router.patch('/agents/{agent_id}')
    async def agent_control(agent_id: UUID, body: Control, request: Request,
                            authorization: str | None = Header(default=None)):
        pool, claims = await context(request, authorization, 'agents:write')
        async with pool.connection() as conn, conn.transaction():
            await scope(conn, claims)
            row = await owned_agent(conn, claims, agent_id, lock=True)
            if row['status'] == 'ARCHIVED':
                raise HTTPException(409, 'Archived manifests are immutable; register a new version')
            await conn.execute('UPDATE task_agents SET status=%s,updated_at=now() WHERE id=%s', (body.status, agent_id))
            await emit(conn, claims, 'AgentStatusChanged', {'agent_id': str(agent_id), 'status': body.status},
                       key=str(uuid4()), correlation=str(agent_id))
            return {'id': agent_id, 'status': body.status}

    @router.post('/automations', status_code=201)
    async def create(body: CreateAutomation, request: Request, authorization: str | None = Header(default=None)):
        pool, claims = await context(request, authorization, 'agents:run')
        if 'tasks:write' not in claims['scopes']:
            raise HTTPException(403, 'tasks:write required')
        definition = body.definition.model_dump(mode='json')
        bounded(definition)
        fingerprint = digest(definition)
        async with pool.connection() as conn, conn.transaction():
            await scope(conn, claims)
            # Serialize count checks for this app to enforce the active-resource cap.
            await conn.execute('SELECT id FROM platform_apps WHERE id=%s FOR UPDATE', (claims['app_id'],))
            prior = await (await conn.execute('''SELECT * FROM automations WHERE app_id=%s AND owner_id=%s
                AND request_key=%s''', (claims['app_id'], claims['user'], body.idempotency_key))).fetchone()
            if prior:
                if prior['definition_hash'] != fingerprint:
                    raise HTTPException(409, 'Idempotency key belongs to another automation')
                return prior
            await owned_agent(conn, claims, body.definition.agent_id)
            count = await (await conn.execute('SELECT count(*) AS n FROM automations WHERE app_id=%s AND owner_id=%s AND status<>%s',
                                            (claims['app_id'], claims['user'], 'ARCHIVED'))).fetchone()
            if count['n'] >= 100:
                raise HTTPException(429, 'Archive unused automations before adding more')
            sequence = await (await conn.execute('SELECT coalesce(max(sequence),0) AS n FROM automation_events')).fetchone()
            return await (await conn.execute('''INSERT INTO automations
                (id,tenant_id,app_id,owner_id,agent_id,definition,definition_hash,request_key,principal,next_run_at,last_event_sequence)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,coalesce(%s,now()),%s) RETURNING *''',
                (uuid4(), claims['tenant'], claims['app_id'], claims['user'], body.definition.agent_id,
                 Jsonb(definition), fingerprint, body.idempotency_key, claims['principal'],
                 body.definition.trigger.start_at, sequence['n']))).fetchone()

    @router.get('/automations')
    async def automations(request: Request, authorization: str | None = Header(default=None),
                          limit: int = Query(default=50, ge=1, le=100), after: UUID | None = None):
        pool, claims = await context(request, authorization, 'tasks:read')
        async with pool.connection() as conn, conn.transaction():
            await scope(conn, claims)
            rows = await (await conn.execute('''SELECT * FROM automations WHERE app_id=%s AND owner_id=%s
                AND (%s::uuid IS NULL OR id>%s) ORDER BY id LIMIT %s''',
                (claims['app_id'], claims['user'], after, after, limit+1))).fetchall()
            return {'automations': rows[:limit], 'next_cursor': str(rows[limit-1]['id']) if len(rows)>limit else None}

    @router.post('/automations/{automation_id}/approve')
    async def approve(automation_id: UUID, body: ApprovalInput, request: Request,
                      authorization: str | None = Header(default=None)):
        pool, claims = await context(request, authorization, 'tasks:approve')
        async with pool.connection() as conn, conn.transaction():
            await scope(conn, claims)
            row = await owned(conn, claims, automation_id)
            if row['status'] not in {'DRAFT', 'PAUSED'} or row['definition_hash'] != body.action_hash:
                raise HTTPException(409, 'Approval does not match an approvable definition')
            # Changing arguments requires a new immutable definition and new approval.
            await conn.execute("UPDATE automations SET status='ACTIVE',approved_at=now(),updated_at=now() WHERE id=%s", (automation_id,))
            await emit(conn, claims, 'AutomationApproved', {'automation_id': str(automation_id),
                       'action_hash': body.action_hash, 'principal': claims['principal']},
                       key=str(uuid4()), correlation=str(automation_id))
            return {'id': automation_id, 'status': 'ACTIVE'}

    @router.patch('/automations/{automation_id}')
    async def control(automation_id: UUID, body: Control, request: Request,
                      authorization: str | None = Header(default=None)):
        pool, claims = await context(request, authorization, 'tasks:write')
        if body.status not in {'PAUSED', 'DISABLED', 'ARCHIVED'}:
            raise HTTPException(422, 'Activation requires exact definition approval')
        async with pool.connection() as conn, conn.transaction():
            await scope(conn, claims)
            row = await owned(conn, claims, automation_id)
            if row['status'] == 'ARCHIVED':
                raise HTTPException(409, 'Automation is archived')
            await conn.execute('UPDATE automations SET status=%s,approved_at=NULL,updated_at=now() WHERE id=%s',
                               (body.status, automation_id))
            await emit(conn, claims, 'AutomationStatusChanged', {'automation_id': str(automation_id), 'status': body.status},
                       key=str(uuid4()), correlation=str(automation_id))
            return {'id': automation_id, 'status': body.status}

    @router.get('/automations/{automation_id}/runs')
    async def runs(automation_id: UUID, request: Request, authorization: str | None = Header(default=None),
                   limit: int = Query(default=50, ge=1, le=100)):
        pool, claims = await context(request, authorization, 'tasks:read')
        async with pool.connection() as conn, conn.transaction():
            await scope(conn, claims)
            await owned(conn, claims, automation_id)
            rows = await (await conn.execute('''SELECT r.*,t.status FROM automation_runs r JOIN chore_tasks t ON t.id=r.task_id
                WHERE automation_id=%s ORDER BY r.created_at DESC LIMIT %s''', (automation_id, limit))).fetchall()
            return {'runs': rows}

    @router.post('/events', status_code=201)
    async def event(body: EventInput, request: Request, authorization: str | None = Header(default=None)):
        pool, claims = await context(request, authorization, 'tasks:write')
        async with pool.connection() as conn, conn.transaction():
            await scope(conn, claims)
            # No client-provided tenant/source is accepted. Bearer authenticates producer.
            return await emit(conn, claims, body.event_type, body.payload, key=body.idempotency_key,
                              source='app:'+claims['app_id'], correlation=body.correlation_id, version=body.version)

    @router.post('/webhooks', status_code=201)
    async def webhook(body: WebhookInput, request: Request, authorization: str | None = Header(default=None)):
        pool, claims = await context(request, authorization, 'tasks:approve')
        from .event_webhooks import destination
        try:
            destination(claims['app_id'], body.destination)
        except ValueError:
            raise HTTPException(422, 'Operator must configure this app destination and signing secret') from None
        async with pool.connection() as conn, conn.transaction():
            await scope(conn, claims)
            sequence = (await (await conn.execute('SELECT coalesce(max(sequence),0) AS n FROM automation_events')).fetchone())['n']
            return await (await conn.execute('''INSERT INTO automation_webhooks
                (id,tenant_id,app_id,owner_id,destination,event_type,event_version,last_sequence)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(app_id,owner_id,destination,event_type,event_version)
                DO UPDATE SET enabled=true RETURNING id,destination,event_type,event_version,enabled''',
                (uuid4(), claims['tenant'], claims['app_id'], claims['user'], body.destination,
                 body.event_type, body.event_version, sequence))).fetchone()

    @router.delete('/webhooks/{webhook_id}')
    async def disable_webhook(webhook_id: UUID, request: Request, authorization: str | None = Header(default=None)):
        pool, claims = await context(request, authorization, 'tasks:approve')
        async with pool.connection() as conn, conn.transaction():
            await scope(conn, claims)
            row = await (await conn.execute('''UPDATE automation_webhooks SET enabled=false WHERE id=%s
                AND app_id=%s AND owner_id=%s RETURNING id''', (webhook_id, claims['app_id'], claims['user']))).fetchone()
            if not row:
                raise HTTPException(404, 'Webhook not found')
            return row

    @router.get('/deliveries')
    async def deliveries(request: Request, authorization: str | None = Header(default=None),
                         limit: int = Query(default=50, ge=1, le=100)):
        pool, claims = await context(request, authorization, 'tasks:read')
        async with pool.connection() as conn, conn.transaction():
            await scope(conn, claims)
            return {'deliveries': await (await conn.execute('''SELECT id,event_id,webhook_id,status,attempts,
                next_at,delivered_at,http_status FROM automation_deliveries WHERE app_id=%s AND owner_id=%s
                ORDER BY next_at DESC LIMIT %s''', (claims['app_id'], claims['user'], limit))).fetchall()}

    @router.get('/events')
    async def events(request: Request, authorization: str | None = Header(default=None), after: int = 0,
                     limit: int = Query(default=50, ge=1, le=100)):
        pool, claims = await context(request, authorization, 'tasks:read')
        async with pool.connection() as conn, conn.transaction():
            await scope(conn, claims)
            rows = await (await conn.execute('''SELECT * FROM automation_events WHERE app_id=%s AND owner_id=%s
                AND sequence>%s ORDER BY sequence LIMIT %s''', (claims['app_id'], claims['user'], after, limit))).fetchall()
            return {'events': rows, 'next_after': rows[-1]['sequence'] if rows else after}

    @router.put('/preferences/{namespace}')
    async def preferences(namespace: str, body: PreferenceInput, request: Request,
                          authorization: str | None = Header(default=None)):
        if not namespace or len(namespace)>100:
            raise HTTPException(422, 'Invalid preference namespace')
        pool, claims = await context(request, authorization, 'tasks:write')
        bounded(body.value)
        async with pool.connection() as conn, conn.transaction():
            await scope(conn, claims)
            if body.expected_version == 0:
                row = await (await conn.execute('''INSERT INTO automation_preferences(tenant_id,app_id,owner_id,namespace,value)
                    VALUES(%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING RETURNING *''',
                    (claims['tenant'], claims['app_id'], claims['user'], namespace, Jsonb(body.value)))).fetchone()
            else:
                row = await (await conn.execute('''UPDATE automation_preferences SET value=%s,version=version+1,updated_at=now()
                    WHERE app_id=%s AND owner_id=%s AND namespace=%s AND version=%s RETURNING *''',
                    (Jsonb(body.value), claims['app_id'], claims['user'], namespace, body.expected_version))).fetchone()
            if not row:
                raise HTTPException(409, 'Preference changed; reload its version')
            return row

    @router.get('/preferences/{namespace}')
    async def get_preferences(namespace: str, request: Request, authorization: str | None = Header(default=None)):
        pool, claims = await context(request, authorization, 'tasks:read')
        async with pool.connection() as conn, conn.transaction():
            await scope(conn, claims)
            row = await (await conn.execute('SELECT * FROM automation_preferences WHERE app_id=%s AND owner_id=%s AND namespace=%s',
                                           (claims['app_id'], claims['user'], namespace))).fetchone()
            return row or {'namespace': namespace, 'value': {}, 'version': 0}

    @router.get('/notifications')
    async def notifications(request: Request, authorization: str | None = Header(default=None),
                            limit: int = Query(default=50, ge=1, le=100), after: UUID | None = None):
        pool, claims = await context(request, authorization, 'tasks:read')
        async with pool.connection() as conn, conn.transaction():
            await scope(conn, claims)
            rows = await (await conn.execute('''SELECT * FROM automation_notifications WHERE app_id=%s AND owner_id=%s
                AND (%s::uuid IS NULL OR id>%s) ORDER BY id LIMIT %s''',
                (claims['app_id'], claims['user'], after, after, limit+1))).fetchall()
            return {'notifications': rows[:limit], 'next_cursor': str(rows[limit-1]['id']) if len(rows)>limit else None}

    @router.get('/usage')
    async def usage(request: Request, authorization: str | None = Header(default=None)):
        pool, claims = await context(request, authorization, 'tasks:read')
        from .app_catalogue import policy
        async with pool.connection() as conn, conn.transaction():
            await scope(conn, claims)
            row = await (await conn.execute('''SELECT count(*) AS model_calls,coalesce(sum(total_tokens),0) AS reported_tokens,
                count(*) FILTER(WHERE total_tokens IS NULL) AS calls_with_unknown_usage,
                coalesce(sum(latency_ms),0) AS latency_ms FROM platform_model_calls
                WHERE app_id=%s AND owner_id=%s AND created_at>=CURRENT_DATE''',
                (claims['app_id'], claims['user']))).fetchone()
            reserved = await (await conn.execute('SELECT calls FROM agent_planning_usage WHERE app_id=%s AND owner_id=%s AND day=CURRENT_DATE',
                (claims['app_id'], claims['user']))).fetchone()
            scheduled = await (await conn.execute('SELECT runs FROM automation_usage WHERE app_id=%s AND day=CURRENT_DATE',
                                                  (claims['app_id'],))).fetchone()
            return {**row, 'planning_requests_reserved': reserved['calls'] if reserved else 0,
                    'app_automation_runs': scheduled['runs'] if scheduled else 0,
                    'limits': (await policy(conn, claims['app_id'])).model_dump(),
                    'estimated_cost': None, 'cost_basis': 'Provider billing rates are not configured; unknown is not zero.'}

    @router.get('/usage/models')
    async def model_usage(request: Request, days: int = Query(30, ge=1, le=366),
                          authorization: str | None = Header(default=None)):
        pool, claims = await context(request, authorization, 'tasks:read')
        async with pool.connection() as conn, conn.transaction():
            await scope(conn, claims)
            rows = await (await conn.execute('''SELECT (created_at AT TIME ZONE 'UTC')::date AS day,
                provider,model,count(*) AS calls,
                count(*) FILTER(WHERE status='FAILED') AS failed_calls,
                sum(total_tokens) AS reported_tokens,
                count(*) FILTER(WHERE total_tokens IS NULL) AS calls_with_unknown_usage,
                sum(estimated_cost) AS recorded_estimated_cost,
                count(*) FILTER(WHERE estimated_cost IS NULL) AS calls_with_unknown_cost,
                sum(latency_ms) AS latency_ms FROM platform_model_calls
                WHERE app_id=%s AND owner_id=%s AND created_at>=now()-%s*interval '1 day'
                GROUP BY 1,provider,model ORDER BY 1 DESC,provider,model LIMIT 1001''',
                (claims['app_id'], claims['user'], days))).fetchall()
        return {'source':'platform_model_calls (migration 039)', 'days':days, 'timezone':'UTC',
                'rows':rows[:1000], 'truncated':len(rows)>1000,
                'cost_currency':None,
                'cost_basis':'Stored estimates only. Currency is not recorded in this schema; these are not provider invoices. Missing usage or costs remain null.',
                'coverage':'Instrumented planning and adaptive-runtime calls only; not all speech, email or provider account spending.'}

    @router.post('/notifications/{notification_id}/read')
    async def mark_read(notification_id: UUID, request: Request, authorization: str | None = Header(default=None)):
        pool, claims = await context(request, authorization, 'tasks:write')
        async with pool.connection() as conn, conn.transaction():
            await scope(conn, claims)
            row = await (await conn.execute('''UPDATE automation_notifications SET read_at=coalesce(read_at,now())
                WHERE id=%s AND app_id=%s AND owner_id=%s RETURNING id,read_at''',
                (notification_id, claims['app_id'], claims['user']))).fetchone()
            if not row:
                raise HTTPException(404, 'Notification not found')
            return row

    app.include_router(router)
