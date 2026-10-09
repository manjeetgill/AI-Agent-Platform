"""A provider-free diagnostic executed by the normal durable task worker."""
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from psycopg.errors import UniqueViolation
from psycopg.types.json import Jsonb
from pydantic import Field

from .flags import enabled
from .service_identity import authenticate
from .task_api import credential_contract
from .task_store import TaskStore
from .tasks import Action, Strict, TaskError

TOOL = 'platform.connection_test'


class Input(Strict):
    test_id: UUID


class Output(Strict):
    test_id: str = Field(pattern=r'^[a-f0-9-]{36}$')
    worker_received_at: str
    status: Literal['PASSED']
    provider_calls: Literal[0]


class ConnectionTest:
    version, risk, timeout = '1', 'READ', 5
    description = 'Verify platform worker execution without calling a model or external service.'
    input_model, output_model = Input, Output

    def prepare(self, arguments):
        value = Input.model_validate(arguments)
        return Action(TOOL, self.version, value.model_dump(mode='json'), 'Verify platform worker connection', self.risk)

    async def execute(self, action, **context):
        if not await context['authorized']():
            raise TaskError('Execution authority revoked')
        return Output(test_id=action.arguments['test_id'], worker_received_at=datetime.now(UTC).isoformat(),
                      status='PASSED', provider_calls=0).model_dump(mode='json')

    async def reconcile(self, action, **context):
        return None


async def enqueue(pool, claims, body):
    """Commit diagnostic task, immutable action and queue together; retries return it."""
    store = TaskStore(pool, app_id=claims['app_id'], principal=claims['principal'])
    key = 'connection-test:' + str(body.test_id)
    action = ConnectionTest().prepare(body.model_dump(mode='json'))
    async with pool.connection() as conn, conn.transaction():
        # Serialize only this app/user's diagnostic requests, including idempotent replays.
        await conn.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',
                           ('connection-test:'+claims['app_id']+':'+claims['user'],))
        if not await store.granted(conn, claims['app_id'], TOOL):
            raise HTTPException(403, 'Operator must grant platform.connection_test to this application')
        existing = await (await conn.execute('''SELECT id,status FROM chore_tasks WHERE tenant_id=%s
            AND owner_id=%s AND request_key=%s''', (claims['tenant'], claims['user'], key))).fetchone()
        if existing:
            step = await (await conn.execute('SELECT tool,action_hash FROM chore_steps WHERE task_id=%s AND position=0',
                                             (existing['id'],))).fetchone()
            if not step or step['tool'] != TOOL or step['action_hash'] != action.digest:
                raise HTTPException(409, 'Test ID belongs to a different saved task')
            return existing
        count = (await (await conn.execute('''SELECT count(*) AS n FROM chore_tasks WHERE tenant_id=%s
            AND owner_id=%s AND request_key LIKE 'connection-test:%%' AND created_at>=CURRENT_DATE''',
            (claims['tenant'], claims['user']))).fetchone())['n']
        if count >= 20:
            raise HTTPException(429, 'Daily worker-test limit reached (20 per application user)')
        task = uuid4()
        await conn.execute('''INSERT INTO chore_tasks(id,tenant_id,owner_id,source_app_id,request,request_key,revision)
            VALUES(%s,%s,%s,%s,%s,%s,1)''', (task, claims['tenant'], claims['user'], claims['app_id'],
            'Platform connection test (no provider calls)', key))
        await conn.execute('''INSERT INTO chore_steps(task_id,position,tool,tool_version,arguments,action_hash,summary,risk)
            VALUES(%s,0,%s,%s,%s,%s,%s,'READ')''',
            (task, TOOL, action.version, Jsonb(action.arguments), action.digest, action.summary))
        await store.event(conn, task, 'CONNECTION_TEST_CREATED', {'test_id': str(body.test_id)})
        await store.next(conn, task, 0)
        return {'id': task, 'status': 'READY'}


def install(app):
    router = APIRouter(prefix='/v1/integrations', dependencies=[Depends(credential_contract)])

    @router.post('/connection-test', status_code=202)
    async def test(body: Input, request: Request):
        if not enabled('AI_DURABLE_TASKS'):
            raise HTTPException(503, 'Durable tasks are disabled')
        claims = await authenticate(app.state.repository.pool, request.headers.get('authorization'),
                                    request.headers.get('X-External-User-ID'), 'tasks:write')
        if not {'tasks:read', 'agents:run'} <= set(claims['scopes']):
            raise HTTPException(403, 'Worker tests require tasks:read, tasks:write and agents:run')
        try:
            task = await enqueue(app.state.repository.pool, claims, body)
        except UniqueViolation:
            raise HTTPException(409, 'Another task is active for this user; wait for it to finish.') from None
        return {'task_id': str(task['id']), 'status': task['status'], 'poll_url': '/v1/tasks/'+str(task['id']),
                'verifies': 'Worker round trip only; model access, business tools and callbacks need separate live tests.'}

    app.include_router(router)
