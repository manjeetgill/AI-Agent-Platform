"""Paired Chrome runner for a single public search-preparation adapter. Never submits or pays."""
import asyncio
import hashlib
import hmac
import secrets
from uuid import UUID, uuid4

from fastapi import Header, HTTPException, Request
from psycopg.types.json import Jsonb
from pydantic import Field

from .service_identity import authenticate
from .task_store import TaskStore
from .tasks import Action, Reconciliation, Strict, TaskError


class SearchInput(Strict):
    query: str = Field(min_length=1, max_length=150, pattern=r'^[^\x00-\x1f]+$')


class SearchResult(Strict):
    prepared: bool
    handoff: str = Field(max_length=200)


class BrowserSearch:
    version, risk, timeout = '1', 'SEND', 25
    description = ('Prepare a public Wikipedia search in the paired Chrome browser. Fills only the search '
                   'box and never submits it. User supplies query and explicitly approves. No secrets or payments.')
    input_model, output_model = SearchInput, SearchResult

    def __init__(self, pool):
        self.pool = pool

    def prepare(self, arguments):
        value = SearchInput.model_validate(arguments, strict=True)
        return Action('browser.wikipedia_search', '1', value.model_dump(),
            f'Open wikipedia.org in your paired Chrome and type "{value.query}" into its search box. You submit it yourself.', 'SEND')

    async def execute(self, action, **context):
        if not await context['authorized']():
            raise TaskError('Browser authority revoked')
        async with self.pool.connection() as conn:
            await conn.execute('''INSERT INTO browser_commands(id,task_id,attempt_id,arguments)
                VALUES(%s,%s,%s,%s) ON CONFLICT(attempt_id) DO NOTHING''',
                (uuid4(), context['task_id'], context['attempt_id'], Jsonb(action.arguments)))
        while await context['authorized']():
            async with self.pool.connection() as conn:
                command = await (await conn.execute('SELECT * FROM browser_commands WHERE attempt_id=%s',
                                                    (context['attempt_id'],))).fetchone()
            if command['state'] == 'DONE':
                if not command['result']['prepared']:
                    raise TaskError('Browser preparation was interrupted')
                return command['result']
            await asyncio.sleep(.5)
        raise TaskError('Browser execution was interrupted; check actual outcome')

    async def reconcile(self, action, **context):
        async with self.pool.connection() as conn:
            commands = await (await conn.execute('SELECT * FROM browser_commands WHERE task_id=%s ORDER BY created_at DESC',
                                                 (context['task_id'],))).fetchall()
        for command in commands:
            if command['arguments'] != action.arguments:
                continue
            if command['state'] == 'DONE' and command['result']['prepared']:
                return Reconciliation(True, command['result'])
            if command['state'] == 'STARTED' or command['state'] == 'DONE':
                return None
        # Poll requires a live lease and atomically marks STARTED. Pending expired commands cannot run.
        return Reconciliation(False, {'browser_dispatch': 'never-started'})


def install_browser(app, registry):
    async def device(request, authorization):
        # Extension origins are allowed here, authenticated by the scoped, expiring pairing secret.
        if request.client.host not in {'127.0.0.1', '::1', 'testclient'}:
            raise HTTPException(403)
        try:
            device_id, secret = (authorization or '').removeprefix('Bearer ').split('.')
            device_id = UUID(device_id)
        except ValueError:
            raise HTTPException(401, 'Pair Chrome again') from None
        from .db_scope import connection_identity
        await connection_identity(app.state.repository.pool, 'browser', device_id)
        async with app.state.repository.pool.connection() as conn:
            row = await (await conn.execute('SELECT * FROM browser_devices WHERE id=%s AND revoked_at IS NULL AND expires_at>now()', (device_id,))).fetchone()
        if not row or not hmac.compare_digest(row['secret_hash'], hashlib.sha256(secret.encode()).hexdigest()):
            raise HTTPException(401, 'Pair Chrome again')
        return row

    @app.post('/console/browser/pair')
    async def pair(request: Request):
        from .console_app import local
        local(request, mutation=True)
        pool = app.state.repository.pool
        claims = await authenticate(pool, 'Bearer '+app.state.console_credential, 'operator', 'tasks:read')
        identifier, secret = uuid4(), secrets.token_urlsafe(32)
        async with pool.connection() as conn, conn.transaction():
            await conn.execute('UPDATE browser_devices SET revoked_at=now() WHERE app_id=%s AND owner_id=%s',
                               (claims['app_id'], claims['user']))
            await conn.execute('INSERT INTO browser_devices(id,app_id,owner_id,secret_hash) VALUES(%s,%s,%s,%s)',
                (identifier, claims['app_id'], claims['user'], hashlib.sha256(secret.encode()).hexdigest()))
            await conn.execute('INSERT INTO platform_tool_grants(app_id,tool) VALUES(%s,%s) ON CONFLICT DO NOTHING',
                               (claims['app_id'], 'browser.wikipedia_search'))
        return {'pairing_code': f'{identifier}.{secret}', 'expires_in_hours': 24}

    @app.post('/v1/browser/poll')
    async def poll(request: Request, authorization: str | None = Header(default=None)):
        owner = await device(request, authorization)
        async with app.state.repository.pool.connection() as conn, conn.transaction():
            command = await (await conn.execute('''SELECT c.*,t.lease_until FROM browser_commands c
                JOIN chore_tasks t ON t.id=c.task_id
                WHERE c.state='PENDING' AND t.source_app_id=%s AND t.owner_id=%s
                AND t.status='RUNNING' AND t.lease_token=c.attempt_id AND t.lease_until>now()+interval '5 seconds'
                AND EXISTS(SELECT 1 FROM platform_tool_grants g WHERE g.app_id=t.source_app_id AND g.tool='browser.wikipedia_search')
                ORDER BY c.created_at FOR UPDATE OF c,t SKIP LOCKED LIMIT 1''', (owner['app_id'], owner['owner_id']))).fetchone()
            if not command:
                return {'command': None}
            await conn.execute("UPDATE browser_commands SET state='STARTED',device_id=%s WHERE id=%s", (owner['id'], command['id']))
            return {'command': {'id': str(command['id']), 'task_id': str(command['task_id']),
                'operation': 'wikipedia.prepare', 'query': command['arguments']['query'],
                'expires_at': command['lease_until'].isoformat()}}

    @app.post('/v1/browser/{command_id}/authorize')
    async def authorize(command_id: UUID, request: Request, authorization: str | None = Header(default=None)):
        owner = await device(request, authorization)
        async with app.state.repository.pool.connection() as conn:
            command = await (await conn.execute("SELECT * FROM browser_commands WHERE id=%s AND device_id=%s AND state='STARTED'", (command_id, owner['id']))).fetchone()
        if not command or not await TaskStore(app.state.repository.pool).authorized(command['task_id'], command['attempt_id']):
            raise HTTPException(409, 'Execution authority expired or revoked')
        return {'authorized': True}

    @app.post('/v1/browser/{command_id}/result')
    async def result(command_id: UUID, body: SearchResult, request: Request,
                     authorization: str | None = Header(default=None)):
        owner = await device(request, authorization)
        # A late observation is stored for reconciliation; it cannot commit task state directly.
        async with app.state.repository.pool.connection() as conn:
            changed = await (await conn.execute("""UPDATE browser_commands SET state='DONE',result=%s
                WHERE id=%s AND device_id=%s AND state='STARTED' RETURNING id""",
                (Jsonb(body.model_dump()), command_id, owner['id']))).fetchone()
        if not changed:
            raise HTTPException(409, 'Result already recorded or command unavailable')
        return {'recorded': True}

    @app.post('/v1/browser/stop')
    async def stop(request: Request, authorization: str | None = Header(default=None)):
        owner = await device(request, authorization)
        store = TaskStore(app.state.repository.pool, principal='browser:'+str(owner['id']))
        async with app.state.repository.pool.connection() as conn:
            rows = await (await conn.execute("""SELECT id FROM chore_tasks WHERE source_app_id=%s AND owner_id=%s
                AND status NOT IN ('DRAFT','DONE','FAILED','CANCELLED')""", (owner['app_id'], owner['owner_id']))).fetchall()
        for row in rows:
            try:
                # Tenant is obtained through the device's authenticated app, not from the request.
                async with app.state.repository.pool.connection() as conn:
                    tenant = await (await conn.execute('SELECT tenant_id FROM platform_apps WHERE id=%s', (owner['app_id'],))).fetchone()
                await store.control(tenant['tenant_id'], owner['owner_id'], row['id'], 'pause')
            except TaskError:
                pass
        return {'paused': True}
