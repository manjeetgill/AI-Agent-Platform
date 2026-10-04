"""Opt-in integration test against an isolated migrated platform database."""
import asyncio
import os
import time
from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from agent_platform.app import create_app
from agent_platform.catalog import BUILTINS
from agent_platform.migrate import schema_ready
from agent_platform.models import AgentDraft, Finding, Report
from agent_platform.repository import Repository
from agent_platform.tokens import issue

REAL_ASYNC_CONNECT = psycopg.AsyncConnection.connect
REAL_SYNC_CONNECT = psycopg.Connection.connect


def test_versioning_idempotency_and_isolation_in_postgres(monkeypatch):
    url = os.environ.get('AGENT_TEST_DATABASE_URL')
    if not url:
        pytest.skip('Set AGENT_TEST_DATABASE_URL to a disposable migrated platform database')
    monkeypatch.setattr(psycopg.AsyncConnection, 'connect', REAL_ASYNC_CONNECT)

    async def exercise():
        tenant, owner, stranger = 'test-' + uuid4().hex, uuid4(), uuid4()
        pool = AsyncConnectionPool(url, min_size=1, max_size=3, open=False,
                                   kwargs={'row_factory': dict_row})
        await pool.open()
        await pool.wait(timeout=10)
        repository = Repository(pool)
        try:
            async with pool.connection() as connection:
                assert await schema_ready(connection)
            await repository.ensure_builtins(tenant, owner, BUILTINS)
            own = await repository.list_agents(tenant, owner)
            assert {item['name'] for item in own} == {item['name'] for item in BUILTINS.values()}
            assert await repository.list_agents(tenant, stranger) == []
            first = AgentDraft(name='My research', instructions='Review saved evidence',
                               allowed_tools=['scanner_evidence'])
            agent_id = await repository.create_agent(tenant, owner, first)
            assert (await repository.get_agent(tenant, stranger, agent_id)) is None
            second = AgentDraft(name='My research v2', instructions='Challenge saved evidence',
                                allowed_tools=['scanner_evidence'])
            assert await repository.update_agent(tenant, stranger, agent_id, second) is None
            assert await repository.update_agent(tenant, owner, agent_id, second) == 2
            key = 'fixture-' + uuid4().hex
            payload = {'tool': 'scanner_evidence', 'run_id': str(uuid4()), 'instrument': 'NSE:ABC'}
            run_id, created = await repository.create_run(tenant, owner, agent_id, 2, payload, key)
            assert created
            same_id, created = await repository.create_run(tenant, owner, agent_id, 2, payload, key)
            assert not created and same_id == run_id
            assert await repository.transition(run_id, 'CREATED', 'RUNNING')
            assert await repository.transition(run_id, 'RUNNING', 'COMPLETED', report={'summary': 'fixture'})
            assert not await repository.transition(run_id, 'RUNNING', 'FAILED')
            assert (await repository.get_run(tenant, owner, run_id))['agent_version'] == 2
            assert await repository.get_run(tenant, stranger, run_id) is None
            stale_id, created = await repository.create_run(tenant, owner, agent_id, 2, payload,
                                                            'fixture-stale-' + uuid4().hex)
            assert created and await repository.transition(stale_id, 'CREATED', 'RUNNING')
            async with pool.connection() as connection:
                await connection.execute("UPDATE agent_runs SET lease_until=now()-interval '1 second' WHERE id=%s",
                                         (stale_id,))
            assert await repository.reap_stale(tenant, stranger) == 0
            assert await repository.reap_stale(tenant, owner) == 1
            assert (await repository.get_run(tenant, owner, stale_id))['status'] == 'TIMEOUT'
            assert (await repository.usage(tenant, owner))['runs_24h'] >= 2
            assert (await repository.usage(tenant, stranger))['runs_24h'] == 0
            competing = await asyncio.gather(
                repository.create_run(tenant, owner, agent_id, 2, payload, 'fixture-a-' + uuid4().hex),
                repository.create_run(tenant, owner, agent_id, 2, payload, 'fixture-b-' + uuid4().hex))
            assert sum(created for _, created in competing) == 1
            assert sum(run is None for run, _ in competing) == 1
            preference = await repository.add_preference(tenant, owner, 'Prefer 15-minute research')
            assert preference
            assert not await repository.search_memory(tenant, stranger, 'research')
            assert await repository.search_memory(tenant, owner, 'research')
            assert not await repository.remove_preference(tenant, stranger, preference)
            assert await repository.remove_preference(tenant, owner, preference)
            workflow_key = 'wf-' + uuid4().hex
            workflow_id, created = await repository.create_workflow(tenant, owner, 'signal_review', workflow_key)
            assert created
            same_workflow, created = await repository.create_workflow(tenant, owner, 'signal_review', workflow_key)
            assert not created and same_workflow == workflow_id
            assert await repository.get_workflow(tenant, stranger, workflow_id) is None
            assert await repository.workflow_transition(workflow_id, 'CREATED', 'RUNNING')
            await repository.workflow_step(workflow_id, run_id)
            assert await repository.workflow_transition(workflow_id, 'RUNNING', 'COMPLETED')
            assert (await repository.get_workflow(tenant, owner, workflow_id))['step_run_ids'] == [run_id]
        finally:
            async with pool.connection() as connection:
                await connection.execute('DELETE FROM agent_memory WHERE tenant_id=%s', (tenant,))
                await connection.execute('DELETE FROM agent_workflow_runs WHERE tenant_id=%s', (tenant,))
                await connection.execute('DELETE FROM agent_runs WHERE tenant_id=%s', (tenant,))
                await connection.execute('DELETE FROM agent_definitions WHERE tenant_id=%s', (tenant,))
            await pool.close()

    asyncio.run(exercise())


def test_two_step_workflow_runs_and_isolates_reports(monkeypatch):
    url = os.environ.get('AGENT_TEST_DATABASE_URL')
    if not url:
        pytest.skip('Set AGENT_TEST_DATABASE_URL to a disposable migrated platform database')
    monkeypatch.setattr(psycopg.AsyncConnection, 'connect', REAL_ASYNC_CONNECT)
    monkeypatch.setattr(psycopg.Connection, 'connect', REAL_SYNC_CONNECT)
    monkeypatch.setenv('AI_PLATFORM_DATABASE_URL', url)
    monkeypatch.setenv('AI_WORKFLOWS', '1')
    monkeypatch.setenv('AI_MEMORY', '0')
    key = 'w' * 48
    owner, stranger = str(uuid4()), str(uuid4())

    class Runtime:
        async def retrieve(self, tenant, user, agent_id, run_id, query):
            assert user == owner and query.tool == 'scanner_evidence'
            return {'events': [{'kind': 'breakout', 'source_id': '0'}]}

        async def generate(self, definition, evidence, preferences):
            report = Report(summary=definition['name'], data_freshness='Saved scan', supporting=[
                Finding(category='FACT', text='Breakout', source_ids=['0'])])
            return report, 12, 'fixture', 'fixture-model'

    def headers(user):
        token = issue(key, issuer='nraialgo', audience='agent-platform', tenant='nraialgo',
                      user=user, scope='agents:access')
        return {'Authorization': 'Bearer ' + token}

    app = create_app(runtime=Runtime(), signing_key=key)
    try:
        with TestClient(app) as client:
            response = client.post('/v1/agents/workflows', headers=headers(owner), json={
                'kind': 'signal_review', 'evidence': {'tool': 'scanner_evidence',
                    'run_id': str(uuid4()), 'instrument': 'NSE:ABC'},
                'idempotency_key': 'workflow-' + uuid4().hex})
            assert response.status_code == 200, response.text
            workflow_id = response.json()['id']
            for _ in range(40):
                current = client.get('/v1/agents/workflows/' + workflow_id, headers=headers(owner)).json()
                if current['status'] in {'COMPLETED', 'FAILED', 'TIMEOUT'}:
                    break
                time.sleep(0.05)
            assert current['status'] == 'COMPLETED', current
            assert len(current['step_run_ids']) == 2
            for run_id in current['step_run_ids']:
                assert client.get('/v1/agents/runs/' + run_id, headers=headers(owner)).json()['status'] == 'COMPLETED'
                assert client.get('/v1/agents/runs/' + run_id, headers=headers(stranger)).status_code == 404
            assert client.get('/v1/agents/workflows/' + workflow_id, headers=headers(stranger)).status_code == 404
    finally:
        with psycopg.Connection.connect(url) as connection:
            connection.execute('DELETE FROM agent_memory WHERE owner_id=%s', (owner,))
            connection.execute('DELETE FROM agent_workflow_runs WHERE owner_id=%s', (owner,))
            connection.execute('DELETE FROM agent_runs WHERE owner_id=%s', (owner,))
            connection.execute('DELETE FROM agent_definitions WHERE owner_id=%s', (owner,))
