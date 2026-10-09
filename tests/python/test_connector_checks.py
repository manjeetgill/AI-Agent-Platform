"""Real HTTP/Postgres/worker checks with fixture probes; no live retailer claims."""
import asyncio
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from test_durable_tasks import database
from test_task_agents import environment, sql

from agent_platform.automation import dispatch, evaluate_completed
from agent_platform.connector_health import CheckInput, ProbeResult, evaluate
from agent_platform.connector_runtime import install
from agent_platform.decision_tool import default_registry
from agent_platform.task_store import TaskStore
from agent_platform.task_worker import tick
from agent_platform.tasks import Action, Strict


class Arguments(Strict):
    source: str


class Probe:
    version, risk, timeout = '1', 'READ', 5
    description = 'Fixture connector with controlled observations.'
    input_model = output_model = Arguments

    def __init__(self):
        self.calls = 0
        self.status = 'WORKING'
        self.error = False
        self.observations = [{'source_url': 'https://retailer.example/product/123',
            'observation_id': 'fixture-only-123', 'observed_at': datetime.now(UTC).isoformat(),
            'price': 499.0, 'currency': 'INR', 'fees_known': False}]

    def prepare(self, arguments):
        value = Arguments.model_validate(arguments, strict=True)
        return Action('fixture.catalog', '1', value.model_dump(), 'Read fixture data', self.risk)

    async def execute(self, action, **context):
        raise AssertionError('Health checks must use the explicit probe method')

    async def health_check(self, action, *, authorized, **context):
        assert await authorized()
        self.calls += 1
        if self.error:
            raise ValueError('secret-that-must-not-be-persisted')
        return {'status': self.status, 'reason_code': 'fixture_status',
                'observations': self.observations if self.status == 'WORKING' else []}

    async def reconcile(self, action, **context):
        return None


@contextmanager
def setup(monkeypatch):
    monkeypatch.setenv('AI_AUTOMATIONS', '1')
    registry, probe = default_registry(), Probe()
    registry.adapters['fixture.catalog'] = probe
    with environment(monkeypatch, registry) as env:
        client, headers, key, _ = env
        for tool in ('runtime.connector_check', 'fixture.catalog'):
            sql('INSERT INTO platform_tool_grants(app_id,tool) VALUES(%s,%s)', (key['app_id'], tool))
        try:
            yield client, headers, key, registry, probe
        finally:
            for table in ('connector_check_reports', 'connector_check_state', 'connector_check_usage',
                          'automation_notifications', 'automation_runs', 'automations', 'automation_events', 'automation_usage', 'platform_app_policies'):
                sql(f'DELETE FROM {table} WHERE app_id=%s', (key['app_id'],))


def create(client, headers, source='fixture'):
    body = {'idempotency_key': uuid4().hex, 'blueprint': {'name': 'Verify fixture connector',
        'description': 'Read-only verification', 'steps': [{'tool': 'runtime.connector_check', 'version': '1',
        'arguments': {'tool': 'fixture.catalog', 'version': '1', 'arguments': {'source': source}, 'require_price': True}}]}}
    agent = client.post('/v1/task-agents', headers=headers, json=body)
    assert agent.status_code == 201, agent.text
    return agent.json()['id']


def run(client, headers, agent):
    result = client.post('/v1/task-agents/'+agent+'/runs', headers=headers, json={'idempotency_key': uuid4().hex})
    assert result.status_code == 202, result.text
    return result.json()['id']


def drive(registry, callback=None):
    async def work():
        async with AsyncConnectionPool(database(), open=False, kwargs={'row_factory': dict_row}) as pool:
            install(registry, pool)
            previous = [(a,a.pool) for a in registry.adapters.values() if hasattr(a,'pool')]
            for adapter,_ in previous:
                adapter.pool=pool
            try:
                return await callback(pool) if callback else await tick(TaskStore(pool), registry)
            finally:
                for adapter,original in previous:
                    adapter.pool=original
    return asyncio.run(work())


def result(client, headers, task):
    row = client.get('/v1/tasks/'+task, headers=headers).json()
    assert row['status'] == 'DONE', row
    return row['steps'][0]['result']


def test_schedule_runs_probe_and_returns_observation_history(monkeypatch):
    with setup(monkeypatch) as (client, headers, _key, registry, probe):
        agent = create(client, headers)
        response = client.post('/v1/orchestration/automations', headers=headers, json={
            'idempotency_key': uuid4().hex, 'definition': {'agent_id': agent, 'name': 'Connector monitoring',
            'trigger': {'type': 'interval', 'interval_seconds': 60}, 'max_runs': 2,
            'condition': {'path': 'result.usable_evidence', 'operator': 'eq', 'value': True},
            'notification': {'title': 'Evidence available'}}})
        assert response.status_code == 201, response.text
        schedule = response.json()
        assert not drive(registry, lambda pool: dispatch(pool, registry))
        assert client.post('/v1/orchestration/automations/'+schedule['id']+'/approve', headers=headers,
            json={'decision': 'yes', 'action_hash': schedule['definition_hash']}).status_code == 200
        assert drive(registry, lambda pool: dispatch(pool, registry))
        assert drive(registry)
        task = str(sql('SELECT task_id FROM automation_runs WHERE automation_id=%s', (schedule['id'],))[0]['task_id'])
        report = result(client, headers, task)
        assert report['status'] == 'WORKING' and report['usable_evidence']
        assert report['evidence'][0]['unknown_fees'] and report['evidence'][0]['price'] == 499.0
        assert probe.calls == 1
        history = client.get('/v1/connectors/reports', headers=headers).json()
        assert len(history['reports']) == 1 and history['reports'][0]['result'] == report
        assert not client.get('/v1/connectors/reports?after='+str(history['next_after']), headers=headers).json()['reports']
        assert drive(registry, evaluate_completed)
        assert len(client.get('/v1/orchestration/notifications', headers=headers).json()['notifications']) == 1
        catalog = client.get('/v1/connectors', headers=headers).json()
        assert catalog['check_tool_granted'] and next(c for c in catalog['connectors'] if c['tool']=='fixture.catalog')['health_check']
        other = {**headers, 'X-External-User-ID': 'different-owner'}
        assert client.get('/v1/connectors/reports', headers=other).json()['reports'] == []
        assert client.get('/v1/connectors', headers=other).json()['states'] == []
        assert client.post('/v1/connectors/fixture.catalog/default/resume', headers=other, json={'report_id': report['report_id']}).status_code == 404


@pytest.mark.parametrize('status', ['BLOCKED', 'LOGIN_REQUIRED', 'CONFIGURATION_REQUIRED'])
def test_blocked_sources_stop_automatic_requests_until_explicit_resume(monkeypatch, status):
    with setup(monkeypatch) as (client, headers, key, registry, probe):
        probe.status = status
        agent = create(client, headers)
        first = run(client, headers, agent); drive(registry)
        report = result(client, headers, first)
        assert report['needs_attention'] and not report['usable_evidence']
        second = run(client, headers, agent); drive(registry)
        assert result(client, headers, second)['reason_code'] == 'owner_action_required'
        assert probe.calls == 1
        assert client.post('/v1/connectors/fixture.catalog/default/resume', headers=headers, json={'report_id': str(uuid4())}).status_code == 409
        assert client.post('/v1/connectors/fixture.catalog/default/resume', headers=headers, json={'report_id': report['report_id']}).status_code == 200
        third = run(client, headers, agent); drive(registry)
        assert result(client, headers, third)['reason_code'] == 'connector_cooldown'
        sql('UPDATE connector_check_state SET next_check_at=now() WHERE app_id=%s', (key['app_id'],))
        probe.status = 'WORKING'
        fourth = run(client, headers, agent); drive(registry)
        assert result(client, headers, fourth)['status'] == 'WORKING' and probe.calls == 2


def test_errors_are_redacted_and_app_quota_blocks_network(monkeypatch):
    with setup(monkeypatch) as (client, headers, key, registry, probe):
        probe.error = True
        agent = create(client, headers)
        first = run(client, headers, agent); drive(registry)
        report = result(client, headers, first)
        assert report['status'] == 'BROKEN' and 'secret-that' not in str(report)
        sql('UPDATE connector_check_state SET next_check_at=now() WHERE app_id=%s', (key['app_id'],))
        sql('INSERT INTO platform_app_policies(app_id,connector_checks_per_hour) VALUES(%s,1)', (key['app_id'],))
        second = run(client, headers, agent); drive(registry)
        assert result(client, headers, second)['reason_code'] == 'app_hourly_limit'
        assert probe.calls == 1
        sql('DELETE FROM platform_tool_grants WHERE app_id=%s AND tool=%s', (key['app_id'], 'fixture.catalog'))
        assert client.post('/v1/task-agents/'+agent+'/runs', headers=headers, json={'idempotency_key': uuid4().hex}).status_code == 422


def test_restart_after_report_commit_reuses_evidence_without_new_probe(monkeypatch):
    with setup(monkeypatch) as (client, headers, _key, registry, probe):
        task = run(client, headers, create(client, headers))
        async def interrupt_after_probe(pool):
            store = TaskStore(pool)
            claimed = await store.claim()
            adapter, action = registry.resolve(claimed['step'])
            async def authorized():
                return await store.authorized(claimed['id'], claimed['lease_token'])
            return await adapter.execute(action, task_id=task, authorized=authorized)
        persisted = drive(registry, interrupt_after_probe)
        sql("UPDATE chore_tasks SET lease_until=now()-interval '1 second' WHERE id=%s", (task,))
        drive(registry)
        sql('UPDATE chore_jobs SET available_at=now() WHERE task_id=%s', (task,))
        drive(registry)
        recovered = result(client, headers, task)
        assert recovered['execution'] == 'CACHED' and recovered['report_id'] == persisted['report_id']
        assert probe.calls == 1


def test_evidence_rejects_stale_future_missing_price_and_private_links():
    now = datetime.now(UTC)
    inputs = CheckInput(tool='fixture.catalog', version='1', require_price=True)
    base = {'source_url': 'https://retailer.example/product/1', 'observation_id': 'id',
            'observed_at': now.isoformat(), 'price': 100.0, 'currency': 'INR'}
    for change, issue in [({'observed_at': (now-timedelta(days=1)).isoformat()}, 'stale'),
                          ({'observed_at': (now+timedelta(hours=1)).isoformat()}, 'future_timestamp'),
                          ({'price': None}, 'missing_price_or_currency')]:
        evidence, usable = evaluate(ProbeResult(status='WORKING', reason_code='ok', observations=[{**base, **change}]), inputs, now=now)
        assert not usable and issue in evidence[0]['issues']
    assert not evaluate(ProbeResult(status='WORKING', reason_code='ok'), inputs, now=now)[1]
    for link in ['https://user:password@example.com/p', 'https://example.com/p?token=secret', 'file:///secret']:
        with pytest.raises(ValueError):
            ProbeResult(status='WORKING', reason_code='ok', observations=[{**base, 'source_url': link}])
    with pytest.raises(ValueError):
        ProbeResult(status='WORKING', reason_code='ok', observations=[{**base, 'price': float('nan')}])


def test_concurrent_probe_cap_and_runtime_grant_revocation(monkeypatch):
    with setup(monkeypatch) as (client, headers, key, registry, probe):
        agent = create(client, headers)
        task = run(client, headers, agent)
        owner = sql('SELECT tenant_id,owner_id FROM chore_tasks WHERE id=%s', (task,))[0]
        for number in range(3):
            sql('''INSERT INTO connector_check_state(tenant_id,app_id,owner_id,tool,lease_until)
                VALUES(%s,%s,%s,%s,now()+interval '60 seconds')''',
                (owner['tenant_id'], key['app_id'], owner['owner_id'], 'fixture.busy'+str(number)))
        drive(registry)
        assert result(client, headers, task)['reason_code'] == 'app_concurrency_limit' and probe.calls == 0
        sql('UPDATE connector_check_state SET lease_until=NULL WHERE app_id=%s', (key['app_id'],))
        next_task = run(client, headers, agent)
        sql('DELETE FROM platform_tool_grants WHERE app_id=%s AND tool=%s', (key['app_id'], 'fixture.catalog'))
        drive(registry)
        assert probe.calls == 0
        assert client.get('/v1/tasks/'+next_task, headers=headers).json()['status'] != 'DONE'


def test_report_isolation_between_applications(monkeypatch):
    with setup(monkeypatch) as (client, headers, _key, registry, probe):
        task = run(client, headers, create(client, headers)); drive(registry)
        first = result(client, headers, task)
        with setup(monkeypatch) as (client2, headers2, _key2, registry2, probe2):
            assert client2.get('/v1/connectors/reports', headers=headers2).json()['reports'] == []
            assert client2.post('/v1/connectors/fixture.catalog/default/resume', headers=headers2,
                                json={'report_id': first['report_id']}).status_code == 404
            task2 = run(client2, headers2, create(client2, headers2)); drive(registry2)
            assert result(client2, headers2, task2)['report_id'] != first['report_id']
            assert probe.calls == probe2.calls == 1
            assert len(client.get('/v1/connectors/reports', headers=headers).json()['reports']) == 1
            assert len(client2.get('/v1/connectors/reports', headers=headers2).json()['reports']) == 1


def test_rate_limit_retry_after_and_send_probe_rejection(monkeypatch):
    with setup(monkeypatch) as (client, headers, _key, registry, probe):
        async def limited(action, **context):
            probe.calls += 1
            return {'status': 'RATE_LIMITED', 'reason_code': 'provider_rate_limit', 'retry_after_seconds': 1800}
        probe.health_check = limited
        task = run(client, headers, create(client, headers)); drive(registry)
        report = result(client, headers, task)
        assert (datetime.fromisoformat(report['next_check_at'])-datetime.fromisoformat(report['checked_at'])).total_seconds() == 1800
        assert not report['needs_attention'] and not report['usable_evidence']
        probe.risk = 'SEND'
        from agent_platform.connector_runtime import ConnectorCheck
        with pytest.raises(ValueError):
            ConnectorCheck(None, registry).prepare({'tool': 'fixture.catalog', 'version': '1', 'arguments': {'source': 'fixture'}})


def test_connector_rls_hides_other_owner_and_tenant(monkeypatch):

    from agent_platform.automation import scope
    with setup(monkeypatch) as (client, headers, key, registry, _probe):
        task = run(client, headers, create(client, headers)); drive(registry)
        row = sql('SELECT tenant_id,owner_id FROM chore_tasks WHERE id=%s', (task,))[0]
        async def verify(pool):
            async with pool.connection() as conn, conn.transaction():
                try:
                    own = {'tenant': row['tenant_id'], 'app_id': key['app_id'], 'user': str(row['owner_id'])}
                    await scope(conn, own)
                    assert len(await (await conn.execute('SELECT * FROM connector_check_reports')).fetchall()) == 1
                    for changed in ({**own, 'user': str(uuid4())}, {**own, 'tenant': 'different-tenant'}, {**own, 'app_id': str(uuid4())}):
                        await scope(conn, changed)
                        assert not await (await conn.execute('SELECT * FROM connector_check_reports')).fetchall()
                        assert not await (await conn.execute('SELECT * FROM connector_check_state')).fetchall()
                finally:
                    await scope(conn,worker=True)
        drive(registry, verify)


def test_blocking_one_source_does_not_block_another(monkeypatch):
    with setup(monkeypatch) as (client, headers, _key, registry, probe):
        probe.health_scope = lambda action: action.arguments['source']
        probe.status = 'BLOCKED'
        first = run(client, headers, create(client, headers, source='retailer-one')); drive(registry)
        assert result(client, headers, first)['needs_attention']
        probe.status = 'WORKING'
        second = run(client, headers, create(client, headers, source='retailer-two')); drive(registry)
        assert result(client, headers, second)['usable_evidence'] and probe.calls == 2
        third = run(client, headers, create(client, headers, source='retailer-one')); drive(registry)
        assert result(client, headers, third)['reason_code'] == 'owner_action_required' and probe.calls == 2


def test_recovery_suggestions_are_operator_scoped_and_survive_block(monkeypatch):
    import json

    from agent_platform.connector_recovery import recovery_options
    with setup(monkeypatch) as (client, headers, key, registry, probe):
        profile = {'name': 'Approved catalogue export', 'url': 'https://feed.example/catalog',
            'fields': {'source_url': '/url', 'observation_id': '/id', 'observed_at': '/time'},
            'replaces': [{'tool': 'fixture.catalog', 'source': 'default'}]}
        monkeypatch.setenv('AI_FEED_PROFILES', json.dumps({key['app_id']: {'catalogue': profile},
            str(uuid4()): {'private-other-app': profile}}))
        probe.status = 'BLOCKED'
        agent = create(client, headers)
        first = run(client, headers, agent); drive(registry)
        report = result(client, headers, first)
        assert report['verification_mode'] == 'unspecified'
        assert [r['action'] for r in report['recovery_options']] == ['contact_provider', 'setup_feed']
        assert report['recovery_options'][1]['profile'] == 'catalogue'
        second = run(client, headers, agent); drive(registry)
        deferred = result(client, headers, second)
        assert deferred['recovery_options'] == report['recovery_options'] and probe.calls == 1
        assert len(recovery_options(key['app_id'], 'fixture.catalog', 'different', 'BLOCKED')) == 1
        assert len(recovery_options(uuid4(), 'fixture.catalog', 'default', 'BLOCKED')) == 1


def test_eight_scheduled_sources_are_independent_and_blocked_source_stays_stopped(monkeypatch):
    with setup(monkeypatch) as (client, headers, key, registry, probe):
        sources = [f'source-{i}' for i in range(8)]
        calls = []
        probe.health_scope = lambda action: action.arguments['source']
        async def health(action, *, authorized, **context):
            assert await authorized()
            source = action.arguments['source']; calls.append(source)
            return {'status': 'BLOCKED' if source == sources[0] else 'WORKING',
                'reason_code': 'fixture_only', 'verification_mode': 'stored_observation',
                'observations': [] if source == sources[0] else probe.observations}
        probe.health_check = health
        for source in sources:
            agent = create(client, headers, source)
            response = client.post('/v1/orchestration/automations', headers=headers, json={
                'idempotency_key': uuid4().hex, 'definition': {'agent_id': agent, 'name': source,
                'trigger': {'type': 'interval', 'interval_seconds': 60}, 'max_runs': 2}})
            assert response.status_code == 201, response.text
            schedule = response.json()
            assert client.post('/v1/orchestration/automations/'+schedule['id']+'/approve', headers=headers,
                json={'decision':'yes', 'action_hash':schedule['definition_hash']}).status_code == 200
        for _ in sources:
            assert drive(registry, lambda pool: dispatch(pool, registry))
            assert drive(registry)
        reports = client.get('/v1/connectors/reports', headers=headers).json()['reports']
        assert len(reports) == 8 and set(calls) == set(sources)
        assert sum(r['result']['usable_evidence'] for r in reports) == 7
        assert all(r['result']['verification_mode'] == 'stored_observation' for r in reports)
        sql('UPDATE connector_check_state SET next_check_at=now() WHERE app_id=%s', (key['app_id'],))
        sql('UPDATE automations SET next_run_at=now() WHERE app_id=%s', (key['app_id'],))
        for _ in sources:
            assert drive(registry, lambda pool: dispatch(pool, registry))
            assert drive(registry)
        assert calls.count(sources[0]) == 1 and len(calls) == 15
        assert len(sql('SELECT * FROM automation_runs WHERE app_id=%s', (key['app_id'],))) == 16


def test_report_history_pages_newest_first_without_duplicates(monkeypatch):
    with setup(monkeypatch) as (client, headers, key, registry, _probe):
        agent = create(client, headers)
        for _ in range(3):
            sql('UPDATE connector_check_state SET next_check_at=now() WHERE app_id=%s', (key['app_id'],))
            run(client, headers, agent)
            drive(registry)
        ascending = client.get('/v1/connectors/reports', headers=headers).json()['reports']
        assert len(ascending) == 3
        newest = client.get('/v1/connectors/reports?order=desc&limit=2', headers=headers).json()
        assert [r['id'] for r in newest['reports']] == [r['id'] for r in ascending[::-1][:2]]
        older = client.get('/v1/connectors/reports?order=desc&limit=2&before='+str(newest['next_before']), headers=headers).json()
        assert [r['id'] for r in older['reports']] == [ascending[0]['id']]
        assert older['next_before'] is None
        other = {**headers, 'X-External-User-ID': 'other-owner'}
        assert client.get('/v1/connectors/reports?order=desc', headers=other).json()['reports'] == []
        assert client.get('/v1/connectors/reports?order=invalid', headers=headers).status_code == 422
