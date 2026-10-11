from worker_pool import WorkerPool

"""Two unrelated apps share an engine without sharing users, catalogues or tool authority."""
import asyncio
from types import SimpleNamespace

import pytest
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from test_task_agents import INPUT, database, environment

from agent_platform.app_catalogue import AppManifest, configure
from agent_platform.decision_tool import DecisionComparison, default_registry
from agent_platform.tasks import TaskError
from agent_platform.tool_plugins import load_plugins


def setup(key, name='Supplier comparison', quota=500, version=1):
    manifest = AppManifest.model_validate({'app_id': key['app_id'], 'tools': ['decision.compare'],
        'policy': {'planning_calls_per_day': quota, 'max_plan_steps': 1}, 'templates': [{
            'slug': 'compare', 'version': version, 'blueprint': {'name': name, 'description': 'App-owned sample',
                'steps': [{'tool': 'decision.compare', 'version': '1', 'arguments': INPUT}]}}]})
    async def apply():
        async with AsyncConnectionPool(database(), open=False, kwargs={'row_factory': dict_row}) as pool:
            await configure(pool, default_registry(), manifest)
    asyncio.run(apply())
    return manifest


def test_two_apps_catalogue_instantiation_and_isolation(monkeypatch):
    with environment(monkeypatch) as (a, ha, ka, _), environment(monkeypatch) as (b, hb, kb, _):
        setup(ka, 'Supplier comparison')
        setup(kb, 'Venue comparison')
        ca = a.get('/v1/task-agents/catalogue', headers=ha).json()['templates']
        cb = b.get('/v1/task-agents/catalogue', headers=hb).json()['templates']
        assert ca[0]['blueprint']['name'] == 'Supplier comparison'
        assert cb[0]['blueprint']['name'] == 'Venue comparison'
        assert ca[0]['available'] and ca[0]['id'] != cb[0]['id']
        path = '/v1/task-agents/catalogue/'+ca[0]['id']+'/instantiate'
        assert b.post(path, headers=hb, json={'idempotency_key': 'instantiate-1'}).status_code == 404
        body = {'idempotency_key': 'instantiate-1', 'arguments': [INPUT]}
        saved = a.post(path, headers=ha, json=body)
        assert saved.status_code == 201, saved.text
        assert a.post(path, headers=ha, json=body).json()['id'] == saved.json()['id']
        assert b.get('/v1/task-agents/'+saved.json()['id'], headers=hb).status_code == 404
        run = a.post('/v1/task-agents/'+saved.json()['id']+'/runs', headers=ha, json={'idempotency_key': 'invoke-1'})
        assert run.status_code == 202, run.text
        assert b.get('/v1/tasks/'+run.json()['id'], headers=hb).status_code == 404
        with pytest.raises(TaskError, match='immutable'):
            setup(ka, 'Changed without version bump')
        assert a.get('/v1/task-agents/catalogue', headers=ha).json()['templates'][0]['blueprint']['name'] == 'Supplier comparison'
        setup(ka, 'Supplier comparison v2', version=2)
        assert a.get('/v1/task-agents/catalogue', headers=ha).json()['templates'][0]['version'] == 2


def test_app_limits_enforced_and_revocation_changes_availability(monkeypatch):
    with environment(monkeypatch) as (client, headers, key, _):
        manifest = setup(key, quota=1)
        request = {'request': 'Compare the options using the supplied scores.'}
        assert client.post('/v1/task-agents/interpret', headers=headers, json=request).status_code == 200
        assert client.post('/v1/task-agents/interpret', headers=headers, json=request).status_code == 429
        blueprint = manifest.templates[0].blueprint.model_dump()
        blueprint['steps'] *= 2
        assert client.post('/v1/task-agents', headers=headers, json={'blueprint': blueprint, 'idempotency_key': 'too-many-steps'}).status_code == 422
        from test_task_agents import sql
        sql('DELETE FROM platform_tool_grants WHERE app_id=%s', (key['app_id'],))
        template = client.get('/v1/task-agents/catalogue', headers=headers).json()['templates'][0]
        assert not template['available']
        assert client.post('/v1/task-agents/catalogue/'+template['id']+'/instantiate', headers=headers,
                           json={'idempotency_key': 'revoked-template'}).status_code == 422


def test_plugins_are_explicit_and_cannot_override_tools(monkeypatch):
    from agent_platform import tool_plugins
    calls = []
    def factory():
        calls.append(True)
        return {'decision.compare': DecisionComparison()}
    monkeypatch.setattr(tool_plugins, 'entry_points', lambda **kw: [SimpleNamespace(name='business', load=lambda: factory)])
    monkeypatch.delenv('AI_PLATFORM_TOOL_PLUGINS', raising=False)
    default_registry()
    assert not calls
    monkeypatch.setenv('AI_PLATFORM_TOOL_PLUGINS', 'business')
    with pytest.raises(TaskError, match='overwrite'):
        default_registry()
    monkeypatch.setenv('AI_PLATFORM_TOOL_PLUGINS', 'missing')
    with pytest.raises(TaskError, match='missing'):
        load_plugins(SimpleNamespace(adapters={}))


def test_business_package_runs_without_engine_changes(monkeypatch):
    import sys
    from pathlib import Path

    from agent_platform import tool_plugins
    from agent_platform.task_store import TaskStore
    from agent_platform.task_worker import tick
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'examples/shipping_plugin'))
    try:
        from shipping_example import tools
        monkeypatch.setattr(tool_plugins, 'entry_points', lambda **kw: [SimpleNamespace(name='shipping-example', load=lambda: tools)])
        monkeypatch.setenv('AI_PLATFORM_TOOL_PLUGINS', 'shipping-example')
        registry = default_registry()
        with environment(monkeypatch, registry=registry) as (client, headers, key, _):
            manifest = AppManifest.model_validate({'app_id': key['app_id'], 'tools': ['shipping.estimate'],
                'templates': [{'slug': 'shipping-estimate', 'version': 1, 'blueprint': {
                    'name': 'Shipping estimate', 'description': 'Use supplied rates', 'steps': [{
                        'tool': 'shipping.estimate', 'version': '1', 'arguments': {
                            'units': 3, 'rate_minor_units': 125, 'currency': 'INR'}}]}}]})
            async def apply():
                async with AsyncConnectionPool(database(), open=False, kwargs={'row_factory': dict_row}) as pool:
                    await configure(pool, registry, manifest)
            asyncio.run(apply())
            catalog = client.get('/v1/task-agents/catalogue', headers=headers).json()
            template = catalog['templates'][0]
            agent = client.post('/v1/task-agents/catalogue/'+template['id']+'/instantiate', headers=headers,
                                json={'idempotency_key': 'shipping-template'}).json()
            run = client.post('/v1/task-agents/'+agent['id']+'/runs', headers=headers,
                              json={'idempotency_key': 'shipping-run'})
            assert run.status_code == 202, run.text
            client.portal.call(tick, TaskStore(WorkerPool()), registry)
            task = client.get('/v1/tasks/'+run.json()['id'], headers=headers).json()
            assert task['status'] == 'DONE'
            assert task['steps'][0]['result']['total_minor_units'] == 375
            assert [t['tool'] for t in client.get('/v1/task-agents/tools', headers=headers).json()['tools']] == ['shipping.estimate']
    finally:
        sys.path.pop(0)
