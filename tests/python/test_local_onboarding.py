"""Operator onboarding and actual diagnostic worker execution with isolated applications."""
import json
from pathlib import Path
from uuid import uuid4

from test_local_monitor import INTENT
from test_local_monitor import configure as monitor_configure
from test_task_agents import environment, sql, work
from test_task_readiness import live_worker

from agent_platform import local_onboarding
from agent_platform.connection_test import TOOL
from agent_platform.decision_tool import default_registry
from agent_platform.local_onboarding import DEFAULT_SCOPES


def body(name='Fixture app'):
    return {'request_id': str(uuid4()), 'name': name, 'external_user': 'same-user',
            'tools': [TOOL], 'scopes': DEFAULT_SCOPES, 'days': 7}


def setup(tmp_path, monkeypatch, key):
    monkeypatch.setenv('AI_LOCAL_ONBOARDING', '1')
    monkeypatch.setenv('AI_AUTOMATIONS', '1')
    monitor_configure(tmp_path, monkeypatch, [('existing', key)])


def save(client, data):
    response = client.post('/monitor/setup/apps', headers=INTENT, json=data)
    assert response.status_code == 201, response.text
    result = response.json()
    private = json.loads(Path(result['credential_file']).read_text())
    assert private['credential'] not in response.text
    return result, private


def headers(private):
    return {'Authorization': 'Bearer '+private['credential'], 'X-External-User-ID': private['external_user']}


def cleanup(apps):
    for app in apps:
        for table in ('chore_jobs', 'chore_events', 'chore_approvals', 'chore_steps'):
            sql(f'DELETE FROM {table} WHERE task_id IN (SELECT id FROM chore_tasks WHERE source_app_id=%s)', (app,))
        sql('DELETE FROM chore_tasks WHERE source_app_id=%s', (app,))
        for table in ('platform_tool_grants', 'platform_external_users', 'platform_service_keys'):
            sql(f'DELETE FROM {table} WHERE app_id=%s', (app,))
        tenant = sql('DELETE FROM platform_apps WHERE id=%s RETURNING tenant_id', (app,))
        if tenant:
            sql('DELETE FROM platform_organisations WHERE id=%s', (tenant[0]['tenant_id'],))


def test_two_apps_complete_worker_tests_without_sharing_identity(tmp_path, monkeypatch):
    with environment(monkeypatch) as (client, old_headers, key, _):
        setup(tmp_path, monkeypatch, key)
        apps = []
        try:
            a, pa = save(client, body('App A')); apps.append(a['app_id'])
            b, pb = save(client, body('App B')); apps.append(b['app_id'])
            assert a['app_id'] != b['app_id']
            assert Path(a['credential_file']).stat().st_mode & 0o777 == 0o600
            assert a['policy']['automation_runs_per_day'] == 0
            assert a['policy']['user_planning_calls_per_day'] == 5
            assert sql('SELECT tool FROM platform_tool_grants WHERE app_id=%s', (key['app_id'],)) == [{'tool':'decision.compare'}]
            assert len(client.get('/monitor/connections', headers=INTENT).json()['connections']) == 3
            test_id = str(uuid4())
            run = client.post('/v1/integrations/connection-test', headers=headers(pa), json={'test_id':test_id})
            assert run.status_code == 202, run.text
            task = run.json()['task_id']
            repeat = client.post('/v1/integrations/connection-test', headers=headers(pa), json={'test_id':test_id})
            assert repeat.json()['task_id'] == task
            assert work()
            result = client.get('/v1/tasks/'+task, headers=headers(pa)).json()
            assert result['status'] == 'DONE', result
            assert result['steps'][0]['result']['status'] == 'PASSED'
            assert result['steps'][0]['result']['provider_calls'] == 0
            assert client.get('/v1/tasks/'+task, headers=headers(pb)).status_code == 404
            assert client.get('/v1/tasks/'+task, headers=old_headers).status_code == 404
            other = client.post('/monitor/setup/'+b['connection']+'/test', headers=INTENT, json={'test_id':test_id})
            assert other.status_code == 202 and other.json()['task_id'] != task
            assert work()
            assert client.get('/v1/tasks/'+other.json()['task_id'], headers=headers(pb)).json()['status'] == 'DONE'
            with live_worker(default_registry()):
                report = client.get('/monitor/setup/'+a['connection']+'/check?planning=false', headers=INTENT)
                assert report.status_code == 200, report.text
                assert report.json()['ready'] and report.json()['worker_test_available']
                assert report.json()['callbacks']['live_verified'] is False
                assert report.json()['callbacks']['status'] == 'CONFIGURATION_REQUIRED'
                monkeypatch.setenv('FIXTURE_CALLBACK_SECRET', 'x'*48)
                monkeypatch.setenv('AI_EVENT_WEBHOOK_DESTINATIONS', json.dumps({b['app_id']:{'private-other-app':{'url':'https://example.com/callback','secret_env':'FIXTURE_CALLBACK_SECRET'}}}))
                report = client.get('/monitor/setup/'+a['connection']+'/check?planning=false', headers=INTENT)
                assert report.json()['callbacks']['configured_destinations'] == []
                monkeypatch.setenv('AI_EVENT_WEBHOOK_DESTINATIONS', json.dumps({a['app_id']:{'own':{'url':'https://example.com/callback','secret_env':'FIXTURE_CALLBACK_SECRET'}}}))
                report = client.get('/monitor/setup/'+a['connection']+'/check?planning=false', headers=INTENT)
                assert report.json()['callbacks']['status'] == 'SUBSCRIPTION_REQUIRED'
                assert 'https://example.com' not in report.text and 'x'*48 not in report.text
            assert not sql('SELECT * FROM platform_model_calls WHERE app_id=%s', (a['app_id'],))
        finally:
            cleanup(apps)


def test_replay_after_private_index_write_failure_recovers_same_key(tmp_path, monkeypatch):
    with environment(monkeypatch) as (client, _, key, _):
        setup(tmp_path, monkeypatch, key)
        original = local_onboarding.atomic
        data = body()
        def fail_index(path, value):
            if path == tmp_path/'monitor.json':
                raise OSError('fixture save failure')
            original(path, value)
        monkeypatch.setattr(local_onboarding, 'atomic', fail_index)
        assert client.post('/monitor/setup/apps', headers=INTENT, json=data).status_code == 503
        journal = json.loads((tmp_path/'applications'/f"{data['request_id']}.json").read_text())
        try:
            monkeypatch.setattr(local_onboarding, 'atomic', original)
            result, saved = save(client, data)
            assert saved['credential'] == journal['credential']
            assert len(sql('SELECT id FROM platform_service_keys WHERE app_id=%s', (saved['app_id'],))) == 1
            assert save(client, data)[0] == result
            assert client.post('/monitor/setup/apps', headers=INTENT, json={**data,'name':'Different'}).status_code == 409
            sql('UPDATE platform_service_keys SET revoked_at=now() WHERE id=%s', (saved['key_id'],))
            assert client.post('/monitor/setup/apps', headers=INTENT, json=data).status_code == 409
        finally:
            cleanup([journal['app_id']])


def test_private_file_failure_rolls_back_app_key_grants_and_policy(tmp_path, monkeypatch):
    with environment(monkeypatch) as (client, _, key, _):
        setup(tmp_path, monkeypatch, key)
        def fail(*args):
            raise OSError('fixture file failure')
        monkeypatch.setattr(local_onboarding, 'atomic', fail)
        data = body()
        assert client.post('/monitor/setup/apps', headers=INTENT, json=data).status_code == 503
        assert not sql('SELECT id FROM platform_apps WHERE tenant_id=%s', ('local-app-'+data['request_id'],))
        assert not sql('SELECT id FROM platform_organisations WHERE id=%s', ('local-app-'+data['request_id'],))


def test_setup_guards_and_worker_test_permissions(tmp_path, monkeypatch):
    with environment(monkeypatch) as (client, original_headers, key, _):
        setup(tmp_path, monkeypatch, key)
        data = body()
        assert client.get('/monitor/onboard').status_code == 200
        assert client.get('/monitor/setup/catalog', headers=INTENT).status_code == 200
        assert client.post('/monitor/setup/apps', json=data).status_code == 403
        assert client.post('/monitor/setup/apps', headers={**INTENT,'Origin':'https://foreign.example'}, json=data).status_code == 403
        assert client.post('/monitor/setup/apps', headers=INTENT, json={**data,'scopes':['admin:all']}).status_code == 422
        assert client.post('/monitor/setup/apps', headers=INTENT, json={**data,'tools':['not.installed']}).status_code == 422
        assert client.post('/v1/integrations/connection-test', headers=original_headers, json={'test_id':str(uuid4())}).status_code == 403
        a, saved = save(client, {**data,'scopes':['tasks:read']})
        try:
            assert client.post('/v1/integrations/connection-test', headers=headers(saved), json={'test_id':str(uuid4())}).status_code == 403
            assert client.post('/monitor/setup/'+a['connection']+'/test', headers=INTENT, json={'test_id':str(uuid4())}).status_code == 403
            monkeypatch.setenv('AI_LOCAL_ONBOARDING', '0')
            assert client.get('/monitor/onboard').status_code == 404
            assert client.get('/monitor/setup/catalog', headers=INTENT).status_code == 404
        finally:
            cleanup([a['app_id']])


def test_worker_diagnostic_daily_limit_and_revoked_grant(tmp_path, monkeypatch):
    with environment(monkeypatch) as (client, _, key, _):
        setup(tmp_path, monkeypatch, key)
        a, saved = save(client, body())
        try:
            h = headers(saved)
            for _ in range(20):
                r = client.post('/v1/tasks', headers=h, json={'request':'Fixture diagnostic count', 'idempotency_key':'connection-test:'+str(uuid4())})
                assert r.status_code == 201
            assert client.post('/v1/integrations/connection-test', headers=h, json={'test_id':str(uuid4())}).status_code == 429
            sql('DELETE FROM platform_tool_grants WHERE app_id=%s', (a['app_id'],))
            assert client.post('/v1/integrations/connection-test', headers=h, json={'test_id':str(uuid4())}).status_code == 403
        finally:
            cleanup([a['app_id']])


def test_diagnostic_conflict_is_atomic_and_idempotency_cannot_return_another_task(tmp_path, monkeypatch):
    with environment(monkeypatch) as (client, _, key, _):
        setup(tmp_path, monkeypatch, key)
        app, saved = save(client, body())
        try:
            h = headers(saved)
            first = str(uuid4())
            r = client.post('/v1/integrations/connection-test', headers=h, json={'test_id':first})
            assert r.status_code == 202
            second = str(uuid4())
            assert client.post('/v1/integrations/connection-test', headers=h, json={'test_id':second}).status_code == 409
            assert not sql('SELECT id FROM chore_tasks WHERE source_app_id=%s AND request_key=%s', (app['app_id'],'connection-test:'+second))
            assert work()
            fake = str(uuid4())
            client.post('/v1/tasks', headers=h, json={'request':'Different request', 'idempotency_key':'connection-test:'+fake})
            assert client.post('/v1/integrations/connection-test', headers=h, json={'test_id':fake}).status_code == 409
        finally:
            cleanup([app['app_id']])
