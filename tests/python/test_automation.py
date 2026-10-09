import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from test_durable_tasks import database
from test_task_agents import INPUT, draft_and_save, environment, sql, work

from agent_platform.automation import Condition, Trigger, condition_matches, dispatch, evaluate_completed
from agent_platform.decision_tool import default_registry


def test_conditions_fail_closed_and_require_recent_evidence():
    now = datetime.now(UTC)
    condition = Condition(path='result.items.*.price', operator='lt', value=60000,
                          freshness_path='observed_at', max_age_seconds=3600)
    def evidence(price, timestamp):
        return {'result': {'items': [{'price': price, 'observed_at': timestamp}]}}
    assert condition_matches(condition, evidence(50000, now.isoformat()), now)
    assert not condition_matches(condition, evidence('50000', now.isoformat()), now)
    assert not condition_matches(condition, evidence(True, now.isoformat()), now)
    assert not condition_matches(condition, evidence(float('nan'), now.isoformat()), now)
    assert not condition_matches(condition, evidence(50000, (now-timedelta(days=1)).isoformat()), now)
    assert not condition_matches(condition, evidence(50000, 'invalid'), now)
    assert not condition_matches(condition, {})
    assert condition_matches(Condition(path='price', operator='eq', value=1), {'price': 1})
    assert not condition_matches(Condition(path='price', operator='eq', value=1), {'price': True})
    with pytest.raises(ValueError):
        Condition(path='result.__class__()', operator='exists')
    with pytest.raises(ValueError):
        Condition(path='price', operator='lt', value=True)
    with pytest.raises(ValueError):
        Trigger(type='interval')


def drive(fn):
    async def run():
        async with AsyncConnectionPool(database(), open=False, kwargs={'row_factory': dict_row}) as pool:
            return await fn(pool)
    return asyncio.run(run())


@pytest.fixture
def setup(monkeypatch):
    monkeypatch.setenv('AI_AUTOMATIONS', '1')
    with environment(monkeypatch) as env:
        try:
            yield env
        finally:
            app_id = env[2]['app_id']
            for table in ('automation_deliveries', 'automation_webhooks', 'automation_notifications', 'automation_runs', 'automations', 'automation_events',
                          'automation_preferences', 'automation_usage'):
                sql(f'DELETE FROM {table} WHERE app_id=%s', (app_id,))


def create_monitor(client, headers, agent, trigger=None):
    body = {'definition': {'agent_id': agent, 'name': 'Observe scores',
            'trigger': trigger or {'type': 'interval', 'interval_seconds': 60},
            'condition': {'path': 'result.ranking.*.score', 'operator': 'gte', 'value': 8},
            'notification': {'title': 'Condition met'}, 'max_runs': 3},
            'idempotency_key': uuid4().hex}
    row = client.post('/v1/orchestration/automations', headers=headers, json=body)
    assert row.status_code == 201, row.text
    return row.json(), body


def approve(client, headers, row):
    response = client.post(f'/v1/orchestration/automations/{row["id"]}/approve', headers=headers,
                           json={'decision': 'yes', 'action_hash': row['definition_hash']})
    assert response.status_code == 200, response.text


def test_approved_schedule_worker_result_and_exactly_once_notification(setup):
    client, headers, _key, _ = setup
    agent = draft_and_save(client, headers)
    row, body = create_monitor(client, headers, agent)
    assert not drive(lambda pool: dispatch(pool, default_registry()))  # Draft never runs.
    assert client.post('/v1/orchestration/automations', headers=headers, json=body).json()['id'] == row['id']
    wrong = client.post(f'/v1/orchestration/automations/{row["id"]}/approve', headers=headers,
                        json={'decision': 'yes', 'action_hash': '0'*64})
    assert wrong.status_code == 409
    approve(client, headers, row)
    async def race(pool):
        return await asyncio.gather(dispatch(pool, default_registry()), dispatch(pool, default_registry()))
    drive(race)
    assert len(sql('SELECT * FROM automation_runs WHERE automation_id=%s', (row['id'],))) == 1
    assert work()
    assert drive(evaluate_completed)
    assert not drive(evaluate_completed)
    inbox = client.get('/v1/orchestration/notifications', headers=headers).json()['notifications']
    assert len(inbox) == 1
    # Next occurrence remains under the same condition: crossing mode emits no second alert.
    sql("UPDATE automations SET next_run_at=now() WHERE id=%s", (row['id'],))
    assert drive(lambda pool: dispatch(pool, default_registry()))
    assert work()
    assert drive(evaluate_completed)
    assert len(client.get('/v1/orchestration/notifications', headers=headers).json()['notifications']) == 1
    # A new DB connection sees the exact persisted checkpoint, including both runs.
    assert sql('SELECT run_count,last_matched FROM automations WHERE id=%s', (row['id'],))[0] == {'run_count': 2, 'last_matched': True}
    other = {**headers, 'X-External-User-ID': 'other-user'}
    assert client.get('/v1/orchestration/notifications', headers=other).json()['notifications'] == []
    assert client.get(f'/v1/orchestration/automations/{row["id"]}/runs', headers=other).status_code == 404


def test_archiving_schedule_revokes_queued_execution(setup):
    client, headers, _key, _ = setup
    agent = draft_and_save(client, headers)
    row, _ = create_monitor(client, headers, agent)
    approve(client, headers, row)
    assert drive(lambda pool: dispatch(pool, default_registry()))
    task = sql('SELECT task_id FROM automation_runs WHERE automation_id=%s', (row['id'],))[0]['task_id']
    assert client.patch(f'/v1/orchestration/automations/{row["id"]}', headers=headers,
                        json={'status': 'ARCHIVED'}).status_code == 200
    assert not work()
    assert sql('SELECT status FROM chore_tasks WHERE id=%s', (task,))[0]['status'] == 'CANCELLED'
    assert drive(evaluate_completed)
    assert client.get('/v1/orchestration/notifications', headers=headers).json()['notifications'] == []


def test_event_deduplication_preferences_and_lifecycle(setup):
    client, headers, _key, _ = setup
    agent = draft_and_save(client, headers)
    row, _ = create_monitor(client, headers, agent, {'type': 'event', 'event_type': 'ObservationChanged'})
    approve(client, headers, row)
    body = {'event_type': 'ObservationChanged', 'correlation_id': 'test',
            'idempotency_key': uuid4().hex, 'payload': {'changed': True}}
    first = client.post('/v1/orchestration/events', headers=headers, json=body)
    assert first.status_code == 201, first.text
    assert client.post('/v1/orchestration/events', headers=headers, json=body).json()['id'] == first.json()['id']
    assert client.post('/v1/orchestration/events', headers=headers, json={**body, 'payload': {}}).status_code == 409
    sql("UPDATE automations SET next_run_at=now() WHERE id=%s", (row['id'],))
    assert drive(lambda pool: dispatch(pool, default_registry()))
    # Pause after dispatch; worker must not execute a revoked agent.
    assert client.patch(f'/v1/orchestration/agents/{agent}', headers=headers, json={'status': 'PAUSED'}).status_code == 200
    assert not work()
    assert client.post(f'/v1/task-agents/{agent}/runs', headers=headers, json={'idempotency_key': uuid4().hex}).status_code == 409
    pref = {'value': {'retailers': ['example'], 'price_threshold': 60000}, 'expected_version': 0}
    assert client.put('/v1/orchestration/preferences/shopping', headers=headers, json=pref).status_code == 200
    assert client.put('/v1/orchestration/preferences/shopping', headers=headers, json=pref).status_code == 409
    assert client.get('/v1/orchestration/preferences/shopping', headers=headers).json()['version'] == 1
    other = {**headers, 'X-External-User-ID': 'other-user'}
    assert client.get('/v1/orchestration/preferences/shopping', headers=other).json()['value'] == {}
    assert client.get('/v1/orchestration/events', headers=other).json()['events'] == []


def test_revoked_grants_expired_principal_and_quota_stop_dispatch(setup):
    client, headers, key, _ = setup
    agent = draft_and_save(client, headers)
    row, _ = create_monitor(client, headers, agent)
    approve(client, headers, row)
    sql('INSERT INTO platform_app_policies(app_id,automation_runs_per_day) VALUES(%s,0)', (key['app_id'],))
    try:
        assert drive(lambda pool: dispatch(pool, default_registry()))
        assert sql('SELECT run_count FROM automations WHERE id=%s', (row['id'],))[0]['run_count'] == 0
        sql('UPDATE platform_app_policies SET automation_runs_per_day=10 WHERE app_id=%s', (key['app_id'],))
        sql('DELETE FROM platform_tool_grants WHERE app_id=%s', (key['app_id'],))
        sql('UPDATE automations SET next_run_at=now() WHERE id=%s', (row['id'],))
        assert drive(lambda pool: dispatch(pool, default_registry()))
        assert sql('SELECT status FROM automations WHERE id=%s', (row['id'],))[0]['status'] == 'PAUSED'
        assert not sql('SELECT * FROM automation_usage WHERE app_id=%s', (key['app_id'],))
    finally:
        sql('DELETE FROM platform_app_policies WHERE app_id=%s', (key['app_id'],))


def test_manifest_contract_and_tenant_reference(setup):
    client, headers, _key, _ = setup
    body = {'manifest': {'blueprint': {'name': 'Manifest agent', 'description': 'Fixture only',
            'steps': [{'tool': 'decision.compare', 'version': '1', 'arguments': INPUT}]}},
            'idempotency_key': uuid4().hex, 'status': 'DRAFT'}
    from agent_platform.decision_tool import DecisionComparison
    body['manifest']['blueprint']['steps'][0]['version'] = DecisionComparison.version
    response = client.post('/v1/orchestration/agents', headers=headers, json=body)
    assert response.status_code == 201, response.text
    assert response.json()['manifest']['runtime'] == 'durable_plan'
    agent = response.json()['id']
    assert client.post(f'/v1/task-agents/{agent}/runs', headers=headers, json={'idempotency_key': uuid4().hex}).status_code == 409
    other = {**headers, 'X-External-User-ID': 'other-user'}
    assert client.patch(f'/v1/orchestration/agents/{agent}', headers=other, json={'status': 'ACTIVE'}).status_code == 404


def test_database_rls_hides_other_owners_without_application_where_clause(setup):
    client, headers, key, _ = setup
    pref = {'value': {'private': 'only first user'}, 'expected_version': 0}
    assert client.put('/v1/orchestration/preferences/private', headers=headers, json=pref).status_code == 200
    other = {**headers, 'X-External-User-ID': 'other-user'}
    client.get('/v1/orchestration/preferences/private', headers=other)
    owners = sql('SELECT external_id,owner_id FROM platform_external_users WHERE app_id=%s', (key['app_id'],))
    first = next(r['owner_id'] for r in owners if r['external_id'] == 'fixture-user')
    second = next(r['owner_id'] for r in owners if r['external_id'] == 'other-user')

    from agent_platform.automation import scope
    async def verify():
        async with AsyncConnectionPool(database(), open=False, kwargs={'row_factory': dict_row}) as pool:  # noqa: SIM117
            async with pool.connection() as conn, conn.transaction():
                tenant = (await (await conn.execute('SELECT tenant_id FROM platform_apps WHERE id=%s', (key['app_id'],))).fetchone())['tenant_id']
                try:
                    await scope(conn, {'tenant': tenant, 'app_id': key['app_id'], 'user': str(first)})
                    assert len(await (await conn.execute('SELECT * FROM automation_preferences')).fetchall()) == 1
                    await scope(conn, {'tenant': tenant, 'app_id': key['app_id'], 'user': str(second)})
                    assert await (await conn.execute('SELECT * FROM automation_preferences')).fetchall() == []
                    await scope(conn, {'tenant': 'wrong-tenant', 'app_id': key['app_id'], 'user': str(first)})
                    assert await (await conn.execute('SELECT * FROM automation_preferences')).fetchall() == []
                finally:
                    await scope(conn,worker=True)
    asyncio.run(verify())


def test_signed_callback_retry_and_event_replay(setup, monkeypatch):
    import json

    import httpx

    from agent_platform.event_webhooks import deliver, fanout, verify
    client, headers, key, _ = setup
    secret = 'fixture-only-signing-key-' + 's'*32
    monkeypatch.setenv('FIXTURE_WEBHOOK_SECRET', secret)
    monkeypatch.setenv('AI_EVENT_WEBHOOK_DESTINATIONS', json.dumps({key['app_id']: {
        'fixture': {'url': 'https://receiver.example/events', 'secret_env': 'FIXTURE_WEBHOOK_SECRET'}}}))
    response = client.post('/v1/orchestration/webhooks', headers=headers,
        json={'destination': 'fixture', 'event_type': 'ObservationChanged', 'decision': 'yes'})
    assert response.status_code == 201, response.text
    client.post('/v1/orchestration/events', headers=headers, json={'event_type': 'ObservationChanged',
        'correlation_id': 'test', 'idempotency_key': uuid4().hex, 'payload': {'observed': True}})
    assert drive(fanout)
    assert not drive(fanout)
    attempts = []
    def receiver(request):
        assert verify(secret, request.headers['X-Agent-Timestamp'], request.content, request.headers['X-Agent-Signature'])
        assert not verify(secret, '1', request.content, request.headers['X-Agent-Signature'])
        attempts.append(request.headers['Idempotency-Key'])
        return httpx.Response(503 if len(attempts) == 1 else 200)
    transport = httpx.MockTransport(receiver)
    assert drive(lambda pool: deliver(pool, transport=transport))
    sql('UPDATE automation_deliveries SET next_at=now() WHERE app_id=%s', (key['app_id'],))
    assert drive(lambda pool: deliver(pool, transport=transport))
    assert attempts[0] == attempts[1]
    result = client.get('/v1/orchestration/deliveries', headers=headers).json()['deliveries']
    assert len(result) == 1 and result[0]['status'] == 'DELIVERED' and result[0]['attempts'] == 2
    assert secret not in str(result)
