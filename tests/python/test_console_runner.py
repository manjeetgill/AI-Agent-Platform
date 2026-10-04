"""Local console access and browser protocol fencing; no actual browser or merchant involved."""
import asyncio
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from test_durable_tasks import database

from agent_platform.app import create_app
from agent_platform.console_app import create_console
from agent_platform.task_store import TaskStore
from agent_platform.weather_tool import WeatherForecast


def test_console_origin_checks_and_browser_command_fencing(monkeypatch, request):
    monkeypatch.setenv('AI_PLATFORM_DATABASE_URL', database())
    monkeypatch.setenv('AI_DURABLE_TASKS', '1')
    monkeypatch.setenv('AI_TASK_AGENTS', '1')
    from agent_platform.decision_tool import default_registry
    registry = default_registry()
    app = create_console(registry=registry, app=create_app(signing_key='c'*48, task_registry=registry))
    headers = {'X-Console-Intent': 'user-action'}
    async def claim():
        async with AsyncConnectionPool(database(), open=False, kwargs={'row_factory': dict_row}) as pool:
            return await TaskStore(pool).claim()
    async def insert(claimed):
        from psycopg.types.json import Jsonb
        async with AsyncConnectionPool(database(), open=False) as pool, pool.connection() as conn:
            row=await (await conn.execute('''INSERT INTO browser_commands(id,task_id,attempt_id,arguments)
                VALUES(%s,%s,%s,%s) RETURNING id''',
                (uuid4(), claimed['id'], claimed['lease_token'], Jsonb({'query':'test search'})))).fetchone()
            return str(row[0])
    with TestClient(app) as client:
        assert client.get('/').status_code == 200
        assert client.get('/', headers={'Host':'attacker.example'}).status_code == 403
        assert client.get('/console/api/tasks').status_code == 403
        assert client.get('/console/api/tasks', headers={**headers,'Origin':'https://attacker.example'}).status_code == 403
        assert client.get('/console/api/tasks', headers=headers).status_code == 200
        pair=client.post('/console/browser/pair', headers=headers).json()['pairing_code']
        device={'Authorization':'Bearer '+pair}
        assert client.post('/v1/browser/poll').status_code == 401
        definition={'name':'Browser fixture', 'description':'Approved search preparation', 'steps':[
            {'tool':'browser.wikipedia_search','version':'1','arguments':{'query':'test search'}}]}
        agent=client.post('/console/api/task-agents',headers=headers,json={
            'blueprint':definition,'idempotency_key':uuid4().hex}).json()['id']
        task=client.post(f'/console/api/task-agents/{agent}/runs',headers=headers,json={'idempotency_key':uuid4().hex}).json()
        assert task['status']=='WAITING_APPROVAL'
        async def cleanup():
            async with AsyncConnectionPool(database(), open=False) as pool, pool.connection() as conn:
                for table in ('browser_commands','chore_jobs','chore_events','chore_approvals','chore_steps'):
                    await conn.execute(f'DELETE FROM {table} WHERE task_id=%s',(task['id'],))
                await conn.execute('DELETE FROM chore_tasks WHERE id=%s',(task['id'],))
                await conn.execute('DELETE FROM task_agents WHERE id=%s',(agent,))
        request.addfinalizer(lambda: asyncio.run(cleanup()))
        assert client.post('/v1/browser/poll',headers=device).json()['command'] is None
        assert client.post(f'/console/api/tasks/{task["id"]}/approve',headers=headers,json={
            'decision':'yes','action_hash':task['steps'][0]['action_hash']}).status_code==200
        claimed=asyncio.run(claim()); assert str(claimed['id'])==task['id']
        command=asyncio.run(insert(claimed))
        assert client.post('/v1/browser/poll',headers=device).json()['command']['id']==command
        assert client.post('/v1/browser/poll',headers=device).json()['command'] is None
        assert client.post(f'/v1/browser/{command}/authorize',headers=device).status_code==200
        assert client.post('/v1/browser/stop',headers=device).status_code==200
        assert client.post(f'/v1/browser/{command}/authorize',headers=device).status_code==409
        # Late observed outcome is retained for reconciliation, never a task-state override.
        assert client.post(f'/v1/browser/{command}/result',headers=device,json={
            'prepared':True,'handoff':'Fixture observation'}).status_code==200
        row=client.get(f'/console/api/tasks/{task["id"]}',headers=headers).json()
        assert row['status']=='OUTCOME_UNKNOWN'
        assert client.post(f'/v1/browser/{command}/result',headers=device,json={
            'prepared':True,'handoff':'duplicate'}).status_code==409
        client.post('/console/browser/pair',headers=headers)
        assert client.post('/v1/browser/poll',headers=device).status_code==401


def test_weather_schema_fixed_endpoint_and_no_redirects():
    def response(request):
        assert str(request.url).startswith('https://api.open-meteo.com/v1/forecast?')
        return httpx.Response(200,json={'timezone':'UTC','daily':{
            'time':['2026-10-04'], 'temperature_2m_min':[10], 'temperature_2m_max':[20],
            'precipitation_probability_max':[30]}})
    async def check():
        tool=WeatherForecast(httpx.MockTransport(response))
        async def authorized():return True
        result=await tool.execute(tool.prepare({'latitude':0.0,'longitude':0.0,'days':1}),authorized=authorized)
        assert result['days'][0]['maximum_c']==20
        with pytest.raises(ValueError):tool.prepare({'latitude':91.0,'longitude':0.0})
        with pytest.raises(ValueError):tool.prepare({'latitude':0.0,'longitude':0.0,'url':'http://internal'})
    asyncio.run(check())
