"""App request -> validated reusable plan -> atomic durable task invocation."""
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from psycopg.errors import UniqueViolation
from psycopg.types.json import Jsonb
from pydantic import ValidationError

from .flags import enabled
from .runtime_errors import RunFailure
from .service_identity import authenticate
from .task_agents import Blueprint, Idea, InvokeAgent, SaveAgent, catalog, digest, propose, validate
from .task_api import credential_contract
from .task_store import TaskStore
from .tasks import TaskError


def install_task_agents(app, registry):
    router = APIRouter(prefix='/v1/task-agents', dependencies=[Depends(credential_contract)])

    async def context(request, authorization, scope):
        if not enabled('AI_TASK_AGENTS') or not (enabled('AI_DURABLE_TASKS') or enabled('AI_BROWSER_TASKS')):
            raise HTTPException(503, 'Task agents are disabled')
        pool = getattr(request.app.state.repository, 'pool', None)
        if pool is None:
            raise HTTPException(503, 'Durable storage unavailable')
        if not authorization or not authorization.startswith('Bearer apk_'):
            raise HTTPException(401, 'Application service credential required')
        claims = await authenticate(pool, authorization, request.headers.get('X-External-User-ID'), scope)
        return pool, claims

    async def grants(conn, claims):
        return {r['tool'] for r in await (await conn.execute(
            'SELECT tool FROM platform_tool_grants WHERE app_id=%s', (claims['app_id'],))).fetchall()}

    async def owned(conn, claims, agent_id):
        row = await (await conn.execute('''SELECT * FROM task_agents WHERE id=%s AND tenant_id=%s
            AND app_id=%s AND owner_id=%s''',
            (agent_id, claims['tenant'], claims['app_id'], claims['user']))).fetchone()
        if row is None:
            raise HTTPException(404, 'Agent not found')
        return row

    def checked(blueprint, permitted, arguments=None):
        try:
            return validate(blueprint, registry, permitted, arguments)
        except (TaskError, ValidationError, ValueError):
            raise HTTPException(422, 'Invalid arguments, unavailable tool, or changed tool version') from None

    @router.get('/tools')
    async def tools(request: Request, authorization: str | None = Header(default=None)):
        pool, claims = await context(request, authorization, 'agents:read')
        async with pool.connection() as conn:
            return {'tools': catalog(registry, await grants(conn, claims))}

    @router.post('/interpret')
    async def interpret(body: Idea, request: Request, authorization: str | None = Header(default=None)):
        pool, claims = await context(request, authorization, 'agents:write')
        async with pool.connection() as conn, conn.transaction():
            permitted = await grants(conn, claims)
            if not catalog(registry, permitted):
                raise HTTPException(409, 'No typed tools are granted to this app')
            # Reserve before the provider call; failures count too. Both limits commit atomically.
            for table, columns, values, limit in [
                ('app_planning_usage', 'app_id', [claims['app_id']], 500),
                ('agent_planning_usage', 'app_id,owner_id', [claims['app_id'], claims['user']], 50),
            ]:
                placeholders = ','.join(['%s'] * len(values))
                row = await (await conn.execute(f'''INSERT INTO {table}({columns},calls)
                    VALUES({placeholders},1) ON CONFLICT({columns},day) DO UPDATE SET calls={table}.calls+1
                    WHERE {table}.calls<%s RETURNING calls''', (*values, limit))).fetchone()
                if not row:
                    raise HTTPException(429, 'Daily planning limit reached')
        try:
            return await propose(request.app.state.runtime.gateway, body.request, registry, permitted)
        except (TaskError, ValidationError, ValueError):
            raise HTTPException(422, 'Planner could not produce a valid plan for the permitted tools') from None
        except (RunFailure, TimeoutError):
            raise HTTPException(503, 'Planner unavailable; no agent was created') from None

    @router.post('', status_code=201)
    async def save(body: SaveAgent, request: Request, authorization: str | None = Header(default=None)):
        pool, claims = await context(request, authorization, 'agents:write')
        async with pool.connection() as conn, conn.transaction():
            checked(body.blueprint, await grants(conn, claims))
            value = body.blueprint.model_dump()
            fingerprint = digest(value)
            row = await (await conn.execute('''INSERT INTO task_agents
                (id,tenant_id,owner_id,app_id,blueprint,request_key,definition_hash)
                VALUES(%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(app_id,owner_id,request_key) DO NOTHING RETURNING *''',
                (uuid4(), claims['tenant'], claims['user'], claims['app_id'], Jsonb(value),
                 body.idempotency_key, fingerprint))).fetchone()
            if row is None:
                row = await (await conn.execute('''SELECT * FROM task_agents
                    WHERE app_id=%s AND owner_id=%s AND request_key=%s''',
                    (claims['app_id'], claims['user'], body.idempotency_key))).fetchone()
                if row['definition_hash'] != fingerprint:
                    raise HTTPException(409, 'Idempotency key belongs to another agent definition')
            return row

    @router.get('')
    async def listing(request: Request, authorization: str | None = Header(default=None),
                      limit: int = Query(default=50, ge=1, le=100), before: UUID | None = None):
        pool, claims = await context(request, authorization, 'agents:read')
        async with pool.connection() as conn:
            boundary, params = '', [claims['app_id'], claims['user'], claims['tenant']]
            if before:
                row = await owned(conn, claims, before)
                boundary = ' AND (created_at,id)<(%s,%s)'
                params.extend([row['created_at'], row['id']])
            rows = await (await conn.execute('''SELECT * FROM task_agents
                WHERE app_id=%s AND owner_id=%s AND tenant_id=%s''' + boundary +
                ' ORDER BY created_at DESC,id DESC LIMIT %s', (*params, limit+1))).fetchall()
            return {'agents': rows[:limit], 'next_cursor': str(rows[limit-1]['id']) if len(rows)>limit else None}

    @router.get('/{agent_id}')
    async def detail(agent_id: UUID, request: Request, authorization: str | None = Header(default=None)):
        pool, claims = await context(request, authorization, 'agents:read')
        async with pool.connection() as conn:
            return await owned(conn, claims, agent_id)

    @router.post('/{agent_id}/runs', status_code=202)
    async def invoke(agent_id: UUID, body: InvokeAgent, request: Request,
                     authorization: str | None = Header(default=None)):
        pool, claims = await context(request, authorization, 'agents:run')
        store = TaskStore(pool, principal=claims['principal'])
        try:
            async with pool.connection() as conn, conn.transaction():
                definition = await owned(conn, claims, agent_id)
                blueprint = Blueprint.model_validate(definition['blueprint'])
                # Fingerprint the caller's exact invocation, including explicit vs saved arguments.
                fingerprint = digest({'agent_id': str(agent_id), 'version': definition['version'],
                                      'arguments': body.arguments})
                key = f'agent:{agent_id}:{body.idempotency_key}'
                previous = await (await conn.execute('''SELECT * FROM chore_tasks
                    WHERE tenant_id=%s AND owner_id=%s AND request_key=%s''',
                    (claims['tenant'], claims['user'], key))).fetchone()
                if previous:
                    if previous['invocation_hash'] != fingerprint:
                        raise HTTPException(409, 'Idempotency key belongs to another invocation')
                    task_id = previous['id']
                else:
                    actions = checked(blueprint, await grants(conn, claims), body.arguments)
                    task_id = uuid4()
                    inserted = await (await conn.execute('''INSERT INTO chore_tasks
                        (id,tenant_id,owner_id,request,request_key,source_agent_id,source_app_id,invocation_hash,revision)
                        VALUES(%s,%s,%s,%s,%s,%s,%s,%s,1)
                        ON CONFLICT(tenant_id,owner_id,request_key) DO NOTHING RETURNING id''',
                        (task_id, claims['tenant'], claims['user'], 'Run agent: '+blueprint.name, key,
                         agent_id, claims['app_id'], fingerprint))).fetchone()
                    if inserted is None:
                        previous = await (await conn.execute('''SELECT * FROM chore_tasks
                            WHERE tenant_id=%s AND owner_id=%s AND request_key=%s''',
                            (claims['tenant'], claims['user'], key))).fetchone()
                        if previous['invocation_hash'] != fingerprint:
                            raise HTTPException(409, 'Idempotency key belongs to another invocation')
                        task_id = previous['id']
                    else:
                        for position, action in enumerate(actions):
                            await conn.execute('''INSERT INTO chore_steps(task_id,position,tool,tool_version,
                                arguments,action_hash,summary,risk) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)''',
                                (task_id, position, action.tool, action.version, Jsonb(action.arguments),
                                 action.digest, action.summary, action.risk))
                        await store.event(conn, task_id, 'AGENT_INVOKED',
                                          {'agent_id': str(agent_id), 'version': definition['version']})
                        await store.next(conn, task_id, 0)
            return await store.get(claims['tenant'], claims['user'], task_id)
        except UniqueViolation:
            raise HTTPException(409, 'Another task is active for this user') from None

    app.include_router(router)
