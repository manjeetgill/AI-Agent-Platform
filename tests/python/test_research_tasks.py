"""Durable research integration: ownership, version pins and no duplicate provider calls."""
import asyncio
from contextlib import contextmanager
from uuid import uuid4

from fastapi.testclient import TestClient
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from test_durable_tasks import database
from test_task_agents import sql

from agent_platform.app import create_app
from agent_platform.decision_tool import default_registry
from agent_platform.models import AgentDraft, Report
from agent_platform.repository import Repository
from agent_platform.service_identity import provision
from agent_platform.task_store import TaskStore
from agent_platform.task_worker import tick


class ResearchRuntime:
    def __init__(self, user):
        self.user, self.calls, self.fail = user, 0, False

    async def retrieve(self, tenant, user, agent, run, query):
        assert tenant == 'nraialgo' and user == self.user
        return {'facts': [{'label': 'fixture', 'value': 1}], 'freshness': 'Synthetic'}

    async def generate(self, definition, evidence, preferences):
        self.calls += 1
        if self.fail:
            raise TimeoutError('Provider may have received the request')
        return Report(summary='Synthetic verified report.', supporting=[], conflicting=[], missing=[],
                      risks=[], data_freshness='Synthetic'), 100, 'fixture', 'fixture'


@contextmanager
def environment(monkeypatch):
    db, user = database(), str(uuid4())
    monkeypatch.setenv('AI_PLATFORM_DATABASE_URL', db)
    monkeypatch.setenv('AI_DURABLE_TASKS', '1')
    monkeypatch.setenv('AI_TASK_AGENTS', '1')
    async def setup():
        async with AsyncConnectionPool(db, open=False, kwargs={'row_factory': dict_row}) as pool:
            key = await provision(pool, tenant='nraialgo', organisation='Fixture', app_name='Test',
                scopes=['agents:read', 'agents:write', 'agents:run', 'tasks:read', 'tasks:write'])
            async with pool.connection() as conn:
                await conn.execute('INSERT INTO platform_tool_grants(app_id,tool) VALUES(%s,%s)', (key['app_id'], 'research.explain'))
            agent = await Repository(pool).create_agent('nraialgo', user, AgentDraft(name='Test researcher',
                instructions='Explain synthetic evidence', allowed_tools=['research_evidence']))
            return key, agent
    key, agent = asyncio.run(setup())
    monkeypatch.setenv('AI_PLATFORM_RESEARCH_APP_ID', key['app_id'])
    runtime, registry = ResearchRuntime(user), default_registry()
    headers = {'Authorization': 'Bearer '+key['credential'], 'X-External-User-ID': user}
    app = create_app(signing_key='s'*48, runtime=runtime, task_registry=registry)
    with TestClient(app) as client:
        try:
            yield client, headers, key, agent, runtime, registry
        finally:
            for table in ('chore_jobs', 'chore_events', 'chore_approvals', 'chore_steps'):
                sql(f'DELETE FROM {table} WHERE task_id IN (SELECT id FROM chore_tasks WHERE source_app_id=%s)', (key['app_id'],))
            sql('DELETE FROM chore_tasks WHERE source_app_id=%s', (key['app_id'],))
            sql('DELETE FROM task_agents WHERE app_id=%s', (key['app_id'],))
            for table in ('platform_tool_grants', 'agent_planning_usage', 'app_planning_usage', 'platform_external_users', 'platform_service_keys'):
                sql(f'DELETE FROM {table} WHERE app_id=%s', (key['app_id'],))
            sql('DELETE FROM platform_apps WHERE id=%s', (key['app_id'],))
            sql('DELETE FROM agent_memory WHERE owner_id=%s', (user,))
            sql('DELETE FROM agent_runs WHERE owner_id=%s', (user,))
            sql('DELETE FROM agent_definitions WHERE owner_id=%s', (user,))


def invoke(client, headers, agent):
    blueprint = {'name': 'Research fixture', 'description': 'Synthetic only', 'steps': [
        {'tool': 'research.explain', 'version': '1', 'arguments': {'snapshot_id': str(uuid4()), 'agent_id': agent, 'agent_version': 1}}]}
    saved = client.post('/v1/task-agents', headers=headers, json={'blueprint': blueprint, 'idempotency_key': uuid4().hex})
    assert saved.status_code == 201, saved.text
    key = uuid4().hex
    response = client.post(f"/v1/task-agents/{saved.json()['id']}/runs", headers=headers, json={'idempotency_key': key})
    assert response.status_code == 202, response.text
    return response.json(), saved.json()['id'], key


def work(client, registry):
    return client.portal.call(tick, TaskStore(client.app.state.repository.pool), registry)


def test_automatic_research_report_idempotency_and_owner(monkeypatch):
    with environment(monkeypatch) as (client, headers, _key, agent, runtime, registry):
        task, saved, request_key = invoke(client, headers, agent)
        assert task['status'] == 'READY' and task['steps'][0]['risk'] == 'READ'
        assert work(client, registry)
        done = client.get('/v1/tasks/'+task['id'], headers=headers).json()
        assert done['status'] == 'DONE'
        run = done['steps'][0]['result']['run_id']
        assert sql('SELECT status,tokens FROM agent_runs WHERE id=%s', (run,))[0] == {'status': 'COMPLETED', 'tokens': 100}
        replay = client.post(f'/v1/task-agents/{saved}/runs', headers=headers, json={'idempotency_key': request_key})
        assert replay.json()['id'] == task['id'] and runtime.calls == 1
        assert client.get('/v1/tasks/'+task['id'], headers={**headers, 'X-External-User-ID': str(uuid4())}).status_code == 404


def test_provider_timeout_never_resubmits(monkeypatch):
    with environment(monkeypatch) as (client, headers, _key, agent, runtime, registry):
        runtime.fail = True
        task, _, _ = invoke(client, headers, agent)
        for _ in range(3):
            sql("UPDATE chore_jobs SET available_at=now() WHERE task_id=%s", (task['id'],))
            work(client, registry)
        done = client.get('/v1/tasks/'+task['id'], headers=headers).json()
        assert done['status'] == 'FAILED' and runtime.calls == 1


def test_changed_version_and_revoked_grant_do_not_send(monkeypatch):
    with environment(monkeypatch) as (client, headers, key, agent, runtime, registry):
        task, _, _ = invoke(client, headers, agent)
        sql("UPDATE agent_definitions SET status='disabled' WHERE id=%s", (agent,))
        work(client, registry)
        assert runtime.calls == 0
        sql('DELETE FROM platform_tool_grants WHERE app_id=%s', (key['app_id'],))
        sql('UPDATE chore_jobs SET available_at=now() WHERE task_id=%s', (task['id'],))
        work(client, registry)
        assert client.get('/v1/tasks/'+task['id'], headers=headers).json()['status'] == 'PAUSED'


def test_crash_after_report_commit_recovers_without_another_model_call(monkeypatch):
    with environment(monkeypatch) as (client, headers, _key, agent, runtime, registry):
        task, _, _ = invoke(client, headers, agent)
        class LostFinish(TaskStore):
            async def finish(self, *args, **kwargs):
                return False  # Simulated process loss after provider/report commit, before task commit.
        client.portal.call(tick, LostFinish(client.app.state.repository.pool), registry)
        assert runtime.calls == 1
        sql("UPDATE chore_tasks SET lease_until=now()-interval '1 second' WHERE id=%s", (task['id'],))
        work(client, registry)  # Recovery schedules a status check.
        sql('UPDATE chore_jobs SET available_at=now() WHERE task_id=%s', (task['id'],))
        work(client, registry)
        assert client.get('/v1/tasks/'+task['id'], headers=headers).json()['status'] == 'DONE'
        assert runtime.calls == 1


def test_durable_gateway_never_repeats_server_error_or_invalid_output(monkeypatch):
    import httpx
    import pytest

    from agent_platform.gateway import Gateway, NVIDIAProvider
    from agent_platform.runtime_errors import RunFailure
    monkeypatch.setenv('AI_PLATFORM_NVIDIA_FAST_MODEL', 'fixture')
    async def scenario(status, body):
        calls = []
        def respond(request):
            calls.append(request)
            return httpx.Response(status, json=body)
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            gateway = Gateway(NVIDIAProvider('synthetic-key'), client=client, max_attempts=1)
            with pytest.raises(RunFailure):
                await gateway.structured('research', 'test', {}, Report, lambda value: value)
        assert len(calls) == 1
    asyncio.run(scenario(503, {}))
    asyncio.run(scenario(200, {'choices': [{'message': {'content': 'invalid'}}]}))
