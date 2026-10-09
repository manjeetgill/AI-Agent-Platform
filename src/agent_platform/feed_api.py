"""Private credential setup and queued tests; no upstream work runs in web requests."""
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from psycopg.errors import UniqueViolation
from psycopg.types.json import Jsonb
from pydantic import Field, SecretStr

from .automation import emit, scope
from .connector_recovery import recovery_options
from .feed_config import profiles
from .feed_crypto import keyring, seal
from .flags import enabled
from .service_identity import authenticate
from .task_api import credential_contract
from .task_store import TaskStore
from .tasks import Strict


class Create(Strict):
    profile: str = Field(pattern=r'^[a-z][a-z0-9_-]{0,59}$')
    profile_hash: str = Field(pattern=r'^[a-f0-9]{64}$')


class Credential(Strict):
    expected_revision: int = Field(ge=1)
    profile_hash: str = Field(pattern=r'^[a-f0-9]{64}$')
    secret: SecretStr | None = None


class Test(Strict):
    expected_revision: int = Field(ge=1)
    idempotency_key: str = Field(pattern=r'^[a-zA-Z0-9_.-]{8,100}$')


async def body(request, schema):
    data = bytearray()
    async for part in request.stream():
        data.extend(part)
        if len(data)>10000:
            raise HTTPException(413, 'Connection form is too large')
    try:
        return schema.model_validate_json(data)
    except ValueError:
        # Default validation errors may echo secret input. Return only a fixed code.
        raise HTTPException(422, 'Invalid connection form; check required fields') from None


def public(row):
    keys = ('id', 'profile', 'profile_hash', 'revision', 'state', 'reason_code', 'disabled', 'test_task_id', 'updated_at')
    return {**{key: row[key] for key in keys}, 'credential_saved': bool(row['credential_cipher'])}


def install_feeds(app, registry):
    router = APIRouter(prefix='/v1/connectors/feeds', dependencies=[Depends(credential_contract)])

    async def context(request, authorization, permission):
        if not enabled('AI_AUTOMATIONS') or not enabled('AI_DURABLE_TASKS'):
            raise HTTPException(503, 'Durable connectors are disabled')
        pool = getattr(request.app.state.repository, 'pool', None)
        if pool is None:
            raise HTTPException(503, 'Storage unavailable')
        claims = await authenticate(pool, authorization, request.headers.get('X-External-User-ID'), permission)
        async with pool.connection() as conn:
            if not await (await conn.execute('SELECT 1 FROM platform_tool_grants WHERE app_id=%s AND tool=%s',
                                             (claims['app_id'], 'feed.read'))).fetchone():
                raise HTTPException(403, 'Operator must grant feed.read to this app')
        return pool, claims

    def configured(claims, slug, fingerprint=None):
        try:
            profile = profiles(claims['app_id']).get(slug)
        except ValueError:
            raise HTTPException(503, 'Operator must correct feed profiles') from None
        if not profile:
            raise HTTPException(404, 'No approved profile for this app')
        if fingerprint and profile.fingerprint != fingerprint:
            raise HTTPException(409, 'Profile changed; review its endpoint and setup instructions')
        return profile

    async def owned(conn, claims, ident):
        row = await (await conn.execute('''SELECT * FROM feed_connections WHERE id=%s AND app_id=%s AND owner_id=%s FOR UPDATE''',
                                       (ident, claims['app_id'], claims['user']))).fetchone()
        if not row:
            raise HTTPException(404, 'Connection not found')
        return row

    async def queue(conn, claims, row, key):
        if 'agents:run' not in claims['scopes']:
            raise HTTPException(403, 'agents:run is required to test a connection')
        configured(claims, row['profile'], row['profile_hash'])
        if row['disabled']:
            raise HTTPException(409, 'Reconnect before testing')
        request_key = f"feed:{row['id']}:{row['revision']}:{key}"
        previous = await (await conn.execute('SELECT id FROM chore_tasks WHERE tenant_id=%s AND owner_id=%s AND request_key=%s',
                                            (claims['tenant'], claims['user'], request_key))).fetchone()
        if previous:
            return previous['id']
        action = registry.adapters['feed.read'].prepare({'connection_id': str(row['id']), 'revision': row['revision'], 'profile_hash': row['profile_hash']})
        task = uuid4()
        await conn.execute('''INSERT INTO chore_tasks(id,tenant_id,owner_id,request,request_key,source_app_id,revision)
            VALUES(%s,%s,%s,'Test configured API/feed connection',%s,%s,1)''',
            (task, claims['tenant'], claims['user'], request_key, claims['app_id']))
        await conn.execute('''INSERT INTO chore_steps(task_id,position,tool,tool_version,arguments,action_hash,summary,risk)
            VALUES(%s,0,%s,%s,%s,%s,%s,%s)''', (task, action.tool, action.version, Jsonb(action.arguments), action.digest, action.summary, action.risk))
        store = TaskStore(None, principal=claims['principal'])
        await store.event(conn, task, 'FEED_TEST_QUEUED', {'connection_id': str(row['id']), 'revision': row['revision']})
        await store.queue(conn, task)
        # Preserve provider cooldown when a new credential clears an auth block.
        await conn.execute('''UPDATE chore_jobs SET available_at=greatest(available_at,coalesce((SELECT next_check_at
            FROM connector_check_state WHERE app_id=%s AND owner_id=%s AND tool='feed.read' AND source=%s),now()))
            WHERE task_id=%s''', (claims['app_id'], claims['user'], str(row['id']), task))
        await conn.execute("UPDATE feed_connections SET state='TESTING',reason_code='test_queued',test_task_id=%s,updated_at=now() WHERE id=%s", (task, row['id']))
        return task

    async def pause_schedules(conn, claims, ident):
        # Definitions pin the reviewed credential revision. Rotation must not
        # silently retarget previously approved recurring work.
        rows = await (await conn.execute('''UPDATE automations a SET status='PAUSED',approved_at=NULL,updated_at=now()
            FROM task_agents g WHERE a.agent_id=g.id AND a.app_id=%s AND a.owner_id=%s
            AND a.status IN ('DRAFT','ACTIVE','PAUSED') AND EXISTS (
                SELECT 1 FROM jsonb_array_elements(g.blueprint->'steps') s
                WHERE (s->>'tool'='feed.read' AND s->'arguments'->>'connection_id'=%s)
                OR (s->>'tool'='runtime.connector_check' AND s->'arguments'->>'tool'='feed.read'
                    AND s->'arguments'->'arguments'->>'connection_id'=%s)) RETURNING a.id''',
            (claims['app_id'], claims['user'], str(ident), str(ident)))).fetchall()
        for row in rows:
            await emit(conn, claims, 'AutomationPaused', {'automation_id': str(row['id']), 'reason': 'feed_connection_changed'},
                key='feed-pause:'+str(uuid4()), correlation=str(row['id']))

    @router.get('/profiles')
    async def catalogue(request: Request, authorization: str | None = Header(default=None)):
        _, claims = await context(request, authorization, 'tasks:read')
        try:
            configured_profiles = profiles(claims['app_id'])
        except ValueError:
            raise HTTPException(503, 'Operator must correct feed profiles') from None
        try:
            keyring(); encryption = True
        except ValueError:
            encryption = False
        return {'credential_storage_ready': encryption, 'profiles': [{
            'id': slug, 'name': p.name, 'endpoint': p.url, 'profile_hash': p.fingerprint,
            'authorization_required': p.auth != 'none', 'setup_help': p.setup_help, 'alternatives': p.alternatives,
            'max_pages': p.max_pages, 'max_items': p.max_items} for slug,p in configured_profiles.items()]}

    @router.get('')
    async def listing(request: Request, authorization: str | None = Header(default=None)):
        pool, claims = await context(request, authorization, 'tasks:read')
        async with pool.connection() as conn, conn.transaction():
            await scope(conn, claims)
            rows = await (await conn.execute('SELECT * FROM feed_connections WHERE app_id=%s AND owner_id=%s ORDER BY updated_at DESC LIMIT 50',
                                            (claims['app_id'], claims['user']))).fetchall()
            output = []
            for row in rows:
                value = public(row)
                current = profiles(claims['app_id']).get(row['profile'])
                if not current or current.fingerprint != row['profile_hash']:
                    value.update(state='CONFIGURATION_REQUIRED', reason_code='profile_changed')
                if row['test_task_id']:
                    task = await (await conn.execute('SELECT status FROM chore_tasks WHERE id=%s', (row['test_task_id'],))).fetchone()
                    value['task_status'] = task['status'] if task else None
                    if value['state']=='TESTING' and task and task['status'] in {'FAILED','CANCELLED','OUTCOME_UNKNOWN'}:
                        value.update(state='TEST_FAILED', reason_code='test_did_not_complete')
                status = {'AUTHORIZATION_REQUIRED': 'LOGIN_REQUIRED', 'EVIDENCE_REQUIRED': 'WORKING',
                          'READY': 'WORKING', 'TEST_REQUIRED': 'CONFIGURATION_REQUIRED',
                          'WAITING_RETRY': 'NOT_CHECKED'}.get(value['state'], value['state'])
                value['recovery_options'] = recovery_options(claims['app_id'], 'feed.read', str(row['id']),
                    status, usable=value['state']=='READY')
                value['schedules'] = await (await conn.execute('''SELECT a.id,a.status,a.definition,a.definition_hash,a.run_count,
                    a.next_run_at,g.blueprint->'steps'->0->'arguments' AS connection_arguments
                    FROM automations a JOIN task_agents g ON g.id=a.agent_id
                    WHERE a.app_id=%s AND a.owner_id=%s AND a.status<>'ARCHIVED'
                    AND jsonb_array_length(g.blueprint->'steps')=1
                    AND g.blueprint->'steps'->0->>'tool'='feed.read'
                    AND g.blueprint->'steps'->0->'arguments'->>'connection_id'=%s
                    ORDER BY a.created_at DESC LIMIT 100''',
                    (claims['app_id'], claims['user'], str(row['id'])))).fetchall()
                output.append(value)
            return {'connections': output}

    @router.post('', status_code=201)
    async def create(request: Request, authorization: str | None = Header(default=None)):
        pool, claims = await context(request, authorization, 'tasks:write')
        form = await body(request, Create)
        profile = configured(claims, form.profile, form.profile_hash)
        async with pool.connection() as conn, conn.transaction():
            await scope(conn, claims)
            await conn.execute('SELECT id FROM platform_apps WHERE id=%s FOR UPDATE', (claims['app_id'],))
            count = (await (await conn.execute('SELECT count(*) AS n FROM feed_connections WHERE app_id=%s AND owner_id=%s', (claims['app_id'], claims['user']))).fetchone())['n']
            if count>=50:
                raise HTTPException(429, 'Connection limit reached')
            await conn.execute('''INSERT INTO feed_connections(id,tenant_id,app_id,owner_id,profile,profile_hash,state,reason_code)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(app_id,owner_id,profile) DO NOTHING''',
                (uuid4(), claims['tenant'], claims['app_id'], claims['user'], form.profile, form.profile_hash,
                 'AUTHORIZATION_REQUIRED' if profile.auth!='none' else 'TEST_REQUIRED',
                 'credentials_missing' if profile.auth!='none' else 'test_required'))
            row = await (await conn.execute('SELECT * FROM feed_connections WHERE app_id=%s AND owner_id=%s AND profile=%s',
                                           (claims['app_id'], claims['user'], form.profile))).fetchone()
            return public(row)

    @router.put('/{ident}/credentials')
    async def credentials(ident: UUID, request: Request, authorization: str | None = Header(default=None)):
        pool, claims = await context(request, authorization, 'tasks:write')
        form = await body(request, Credential)
        try:
            async with pool.connection() as conn, conn.transaction():
                await scope(conn, claims)
                row = await owned(conn, claims, ident)
                if row['revision'] != form.expected_revision:
                    raise HTTPException(409, 'Connection changed; reload its status')
                profile = configured(claims, row['profile'], form.profile_hash)
                secret = form.secret.get_secret_value() if form.secret else ''
                if profile.auth != 'none' and (not 8<=len(secret)<=4096 or not secret.isascii() or any(ord(c)<33 or ord(c)>126 for c in secret)):
                    raise HTTPException(422, 'Enter a valid provider API credential')
                try:
                    cipher = seal(secret, row) if profile.auth != 'none' else None
                except ValueError:
                    raise HTTPException(503, 'Operator must configure private credential encryption') from None
                row = await (await conn.execute('''UPDATE feed_connections SET credential_cipher=%s,revision=revision+1,
                    profile_hash=%s,disabled=false,updated_at=now() WHERE id=%s RETURNING *''', (cipher, profile.fingerprint, ident))).fetchone()
                await pause_schedules(conn, claims, ident)
                await conn.execute('''UPDATE connector_check_state SET needs_attention=false WHERE app_id=%s AND owner_id=%s
                    AND tool='feed.read' AND source=%s''', (claims['app_id'], claims['user'], str(ident)))
                task = await queue(conn, claims, row, 'credential-update')
                await emit(conn, claims, 'FeedCredentialUpdated', {'connection_id': str(ident), 'revision': row['revision']},
                           key=f'feed-credential:{ident}:{row["revision"]}', correlation=str(ident))
                return {'id': ident, 'revision': row['revision'], 'state': 'TESTING', 'task_id': task, 'credential_saved': cipher is not None}
        except UniqueViolation:
            raise HTTPException(409, 'Another task is active; finish or pause it, then save again') from None

    @router.post('/{ident}/test', status_code=202)
    async def test(ident: UUID, request: Request, authorization: str | None = Header(default=None)):
        pool, claims = await context(request, authorization, 'tasks:write')
        form = await body(request, Test)
        try:
            async with pool.connection() as conn, conn.transaction():
                await scope(conn, claims)
                row = await owned(conn, claims, ident)
                if row['revision'] != form.expected_revision:
                    raise HTTPException(409, 'Connection changed; reload')
                task = await queue(conn, claims, row, form.idempotency_key)
                return {'task_id': task, 'state': 'TESTING'}
        except UniqueViolation:
            raise HTTPException(409, 'Another task is active for this user') from None

    @router.delete('/{ident}')
    async def disconnect(ident: UUID, request: Request, authorization: str | None = Header(default=None)):
        pool, claims = await context(request, authorization, 'tasks:write')
        async with pool.connection() as conn, conn.transaction():
            await scope(conn, claims)
            await owned(conn, claims, ident)
            await pause_schedules(conn, claims, ident)
            await conn.execute("UPDATE feed_connections SET disabled=true,credential_cipher=NULL,revision=revision+1,state='DISCONNECTED',reason_code='owner_disconnected',updated_at=now() WHERE id=%s", (ident,))
            await emit(conn, claims, 'FeedDisconnected', {'connection_id': str(ident)}, key='feed-disconnect:'+str(uuid4()), correlation=str(ident))
            return {'id': ident, 'state': 'DISCONNECTED'}

    app.include_router(router)
