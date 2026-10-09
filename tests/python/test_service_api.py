"""Public task API contracts with real storage and synthetic tools."""
import asyncio
from uuid import uuid4

from fastapi.testclient import TestClient
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from test_durable_tasks import FakeSite, database

from agent_platform.app import create_app
from agent_platform.service_identity import provision
from agent_platform.tasks import Registry


def test_app_credentials_scopes_rotation_and_event_replay(monkeypatch):
    db = database()
    monkeypatch.setenv('AI_PLATFORM_DATABASE_URL', db)
    monkeypatch.setenv('AI_BROWSER_TASKS', '1')
    tenant = 'service-' + uuid4().hex

    async def setup():
        async with AsyncConnectionPool(db, open=False, kwargs={'row_factory': dict_row}) as pool:
            common = {'tenant': tenant, 'organisation': 'Fixture org', 'app_name': 'Fixture app'}
            primary = await provision(pool, **common, scopes=['tasks:read', 'tasks:write', 'tasks:approve'])
            async with pool.connection() as conn:
                await conn.execute('INSERT INTO platform_tool_grants(app_id,tool) VALUES(%s,%s)',
                                   (primary['app_id'], 'send'))
            readonly = await provision(pool, **common, scopes=['tasks:read'], app_id=primary['app_id'])
            writer = await provision(pool, **common, scopes=['tasks:write'], app_id=primary['app_id'])
            other = await provision(pool, **common, scopes=['tasks:read'])
            foreign = await provision(pool, tenant=tenant+'-other', organisation='Other org',
                                      app_name='Other app', scopes=['tasks:read'])
            return primary, readonly, writer, other, foreign

    primary, readonly, writer, other, foreign = asyncio.run(setup())
    def headers(key=primary, user='customer-42'):
        return {'Authorization': 'Bearer ' + key['credential'], 'X-External-User-ID': user}

    async def invalidate(key, expired=False):
        async with AsyncConnectionPool(db, open=False) as pool, pool.connection() as conn:
            column = "expires_at=now()-interval '1 second'" if expired else 'revoked_at=now()'
            await conn.execute('UPDATE platform_service_keys SET ' + column + ' WHERE id=%s', (key['key_id'],))

    send = FakeSite('SEND')
    with TestClient(create_app(signing_key='s'*48, task_registry=Registry({'send': send}))) as client:
        assert client.get('/v1/tasks', headers={'X-User-ID': str(uuid4())}).status_code == 401
        assert client.get('/v1/tasks', headers={'Authorization': headers()['Authorization']}).status_code == 422
        assert client.get('/v1/tasks', headers={**headers(), 'Authorization': headers()['Authorization']+'x'}).status_code == 401
        body = {'request': 'Submit fixture task', 'idempotency_key': uuid4().hex}
        assert client.post('/v1/tasks', headers=headers(readonly), json=body).status_code == 403
        response = client.post('/v1/tasks', headers=headers(), json=body)
        assert response.status_code == 201
        task = response.json()['id']
        assert client.post('/v1/tasks', headers=headers(), json=body).json()['id'] == task
        for key, user in [(other, 'customer-42'), (foreign, 'customer-42'), (primary, 'customer-43')]:
            assert client.get('/v1/tasks/'+task, headers=headers(key, user)).status_code == 404
            assert client.get(f'/v1/tasks/{task}/events', headers=headers(key, user)).status_code == 404
            assert client.get('/v1/tasks?before='+task, headers=headers(key, user)).status_code == 404
        assert client.get('/v1/tasks/'+task, headers=headers(readonly)).status_code == 200
        assert client.put(f'/v1/tasks/{task}/plan', headers=headers(writer), json={
            'steps': [{'tool': 'send', 'arguments': {'item': 'fixture'}}]}).status_code == 200
        row = client.post(f'/v1/tasks/{task}/start', headers=headers(writer)).json()
        approval = {'decision': 'yes', 'action_hash': row['steps'][0]['action_hash']}
        assert client.post(f'/v1/tasks/{task}/approve', headers=headers(writer), json=approval).status_code == 403
        assert client.post(f'/v1/tasks/{task}/approve', headers=headers(), json=approval).status_code == 200
        assert send.calls == 0
        events = client.get(f'/v1/tasks/{task}/events?limit=2', headers=headers()).json()
        remaining = client.get(f'/v1/tasks/{task}/events?after={events["next_after"]}', headers=headers()).json()
        assert {e['id'] for e in events['events']}.isdisjoint(e['id'] for e in remaining['events'])
        approved = next(e for e in remaining['events'] if e['event'] == 'APPROVED')
        assert approved['details']['principal'] == 'service:' + primary['key_id']
        for _ in range(2):
            assert client.post('/v1/tasks', headers=headers(), json={**body, 'idempotency_key': uuid4().hex}).status_code == 201
        page = client.get('/v1/tasks?limit=2', headers=headers()).json()
        tail = client.get('/v1/tasks?limit=2&before='+page['next_cursor'], headers=headers()).json()
        assert len(page['tasks']) == 2 and len(tail['tasks']) == 1
        assert len({t['id'] for t in page['tasks']+tail['tasks']}) == 3
        assert tail['next_cursor'] is None
        assert client.get('/v1/tasks?limit=101', headers=headers()).status_code == 422
        asyncio.run(invalidate(readonly, expired=True))
        assert client.get('/v1/tasks', headers=headers(readonly)).status_code == 401
        asyncio.run(invalidate(primary))
        assert client.get('/v1/tasks', headers=headers()).status_code == 401
        assert client.post(f'/v1/tasks/{task}/cancel', headers=headers(writer)).status_code == 200
        spec = client.app.openapi()
        assert '/v1/tasks/{task_id}/stream' in spec['paths']


def test_stream_replays_after_last_event_id(monkeypatch):
    from types import SimpleNamespace

    from fastapi import FastAPI
    from test_durable_tasks import fixture_store, planned

    from agent_platform.task_api import install_tasks

    monkeypatch.setenv('AI_BROWSER_TASKS', '1')

    async def check():
        async with fixture_store() as (store, tenant, user):
            task = await planned(store, tenant, user, FakeSite())
            history = await store.events(tenant, user, task)
            app = FastAPI()
            app.state.repository = SimpleNamespace(pool=store.pool)
            router = install_tasks(app, lambda _: {'tenant': tenant, 'user': user}, Registry())
            endpoint = next(r.endpoint for r in router.routes if r.path.endswith('/stream'))
            async def connected():
                return False
            request = SimpleNamespace(app=app, method='GET', headers={}, is_disconnected=connected)
            response = await endpoint(task_id=task, request=request, authorization='fixture',
                                      after=0, last_event_id=history[0]['id'])
            frame = await anext(response.body_iterator)
            assert f"id: {history[1]['id']}\n" in frame
            assert 'PLAN_SAVED' in frame and 'CREATED' not in frame
            await response.body_iterator.aclose()
    asyncio.run(check())
