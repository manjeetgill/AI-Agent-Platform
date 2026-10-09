"""Operator monitor uses pinned per-app credentials; controls reuse the task API."""
import json
from uuid import uuid4

import pytest
from test_task_agents import INPUT, environment, sql

from agent_platform.local_monitor import private_json

INTENT = {'X-Monitor-Intent': 'user-action'}


def configure(tmp_path, monkeypatch, profiles):
    entries = []
    for name, key in profiles:
        client = tmp_path / (name + '.json')
        client.write_text(json.dumps({'url': 'http://127.0.0.1:4060', 'app_id': key['app_id'],
                                     'credential': key['credential'], 'external_user': 'fixture-user'}))
        client.chmod(0o600)
        entries.append({'id': name, 'name': name.title(), 'client_file': str(client)})
    config = tmp_path / 'monitor.json'
    config.write_text(json.dumps({'connections': entries}))
    config.chmod(0o600)
    monkeypatch.setenv('AI_LOCAL_MONITOR', '1')
    monkeypatch.setenv('AI_LOCAL_MONITOR_CONNECTIONS', str(config))


def create(client, headers, name='Fixture monitor task'):
    r = client.post('/v1/tasks', headers=headers, json={'request': name, 'idempotency_key': uuid4().hex})
    assert r.status_code == 201, r.text
    return r.json()['id']


def test_monitor_is_local_and_credentials_never_reach_browser(tmp_path, monkeypatch):
    with environment(monkeypatch) as (client, _, key, _):
        configure(tmp_path, monkeypatch, [('first', key)])
        page = client.get('/monitor')
        assert page.status_code == 200 and "frame-ancestors 'none'" in page.headers['content-security-policy']
        assert client.get('/monitor/connections').status_code == 403
        response = client.get('/monitor/connections', headers=INTENT)
        assert response.json() == {'connections': [{'id': 'first', 'name': 'First'}]}
        assert key['credential'] not in response.text and str(tmp_path) not in response.text
        assert client.get('/monitor/connections', headers={**INTENT, 'Origin': 'https://evil.example'}).status_code == 403
        assert client.get('/monitor/connections', headers={**INTENT, 'Sec-Fetch-Site': 'cross-site'}).status_code == 403
        assert client.get('/monitor', headers={'Host': 'public.example'}).status_code == 403
        assert client.get('/monitor/api/first/knowledge/documents', headers=INTENT).status_code == 404
        assert client.post('/monitor/api/first/tasks/anything/approve', headers=INTENT).status_code == 404
        monkeypatch.setenv('AI_LOCAL_MONITOR', '0')
        assert client.get('/monitor').status_code == 404


def test_each_connection_sees_only_its_app_and_owner(tmp_path, monkeypatch):
    with environment(monkeypatch) as (client, ha, a, _), environment(monkeypatch) as (_, hb, b, _):
        configure(tmp_path, monkeypatch, [('first', a), ('second', b)])
        ta, tb = create(client, ha, 'Application A private task'), create(client, hb, 'Application B private task')
        foreign = create(client, {**ha, 'X-External-User-ID': 'another-user'}, 'Another owner private task')
        for profile, own, denied in [('first', ta, tb), ('second', tb, ta)]:
            r = client.get('/monitor/api/' + profile + '/tasks', headers=INTENT)
            assert r.status_code == 200, r.text
            assert [t['id'] for t in r.json()['tasks']] == [own]
            assert client.get('/monitor/api/' + profile + '/tasks/' + denied, headers=INTENT).status_code == 404
            assert client.post('/monitor/api/' + profile + '/tasks/' + denied + '/cancel', headers=INTENT).status_code == 404
            assert client.get('/monitor/api/' + profile + '/tasks/' + foreign, headers=INTENT).status_code == 404
            assert client.get('/monitor/api/' + profile + '/tasks/summary', headers=INTENT).json()['counts'] == {'DRAFT': 1}
        r = client.get('/monitor/api/first/tasks', headers={**INTENT, **hb, 'X-External-User-ID': 'another-user'})
        assert [t['id'] for t in r.json()['tasks']] == [ta]


def test_controls_pause_resume_cancel_and_uncertain_outcomes(tmp_path, monkeypatch):
    with environment(monkeypatch) as (client, headers, key, _):
        configure(tmp_path, monkeypatch, [('first', key)])
        task = create(client, headers)
        assert client.put('/v1/tasks/' + task + '/plan', headers=headers, json={
            'steps': [{'tool': 'decision.compare', 'arguments': INPUT}]}).status_code == 200
        assert client.post('/v1/tasks/' + task + '/start', headers=headers).status_code == 200
        base = '/monitor/api/first/tasks/' + task
        assert client.post(base + '/pause').status_code == 403
        assert client.post(base + '/pause', headers=INTENT).json()['status'] == 'PAUSED'
        assert client.post(base + '/resume', headers=INTENT).json()['status'] == 'READY'
        sql("UPDATE chore_tasks SET status='OUTCOME_UNKNOWN' WHERE id=%s", (task,))
        sql("UPDATE chore_steps SET risk='SEND',outcome_pending=true WHERE task_id=%s", (task,))
        assert client.post(base + '/resume', headers=INTENT).status_code == 409
        assert client.post(base + '/reconcile', headers=INTENT).json()['status'] == 'READY'
        assert sql('SELECT mode FROM chore_jobs WHERE task_id=%s', (task,))[0]['mode'] == 'RECONCILE'
        sql("UPDATE chore_tasks SET status='OUTCOME_UNKNOWN' WHERE id=%s", (task,))
        assert client.post(base + '/cancel', headers=INTENT).json()['status'] == 'OUTCOME_UNKNOWN'


def test_status_filters_pagination_progress_and_key_revocation(tmp_path, monkeypatch):
    with environment(monkeypatch) as (client, headers, key, _):
        configure(tmp_path, monkeypatch, [('first', key)])
        first, second = create(client, headers), create(client, headers)
        sql("UPDATE chore_tasks SET status='DONE' WHERE id=%s", (first,))
        base = '/monitor/api/first/tasks'
        page = client.get(base, headers=INTENT, params={'status': 'DONE', 'limit': 1}).json()
        assert [t['id'] for t in page['tasks']] == [first] and page['next_cursor'] is None
        assert page['tasks'][0]['total_steps'] == 0
        page = client.get(base, headers=INTENT, params={'limit': 1}).json()
        assert page['tasks'][0]['id'] == second and page['next_cursor'] == second
        assert client.get(base, headers=INTENT, params={'before': second}).json()['tasks'][0]['id'] == first
        sql("UPDATE platform_service_keys SET scopes=ARRAY['tasks:read'] WHERE id=%s", (key['key_id'],))
        assert not client.get(base + '/' + second, headers=INTENT).json()['monitor_can_control']
        assert client.post(base + '/' + second + '/cancel', headers=INTENT).status_code == 403
        sql('UPDATE platform_service_keys SET revoked_at=now() WHERE id=%s', (key['key_id'],))
        assert client.get(base, headers=INTENT).status_code == 401


def test_monitor_refuses_public_endpoints_and_world_readable_files(tmp_path, monkeypatch):
    with environment(monkeypatch) as (client, _, key, _):
        configure(tmp_path, monkeypatch, [('first', key)])
        path = tmp_path / 'first.json'
        data = json.loads(path.read_text())
        data['url'] = 'https://foreign.example'
        path.write_text(json.dumps(data))
        response = client.get('/monitor/api/first/tasks', headers=INTENT)
        assert response.status_code == 503 and 'foreign.example' not in response.text
        path.chmod(0o644)
        with pytest.raises(ValueError):
            private_json(path)
        path.chmod(0o600)
        data['url'] = 'http://127.0.0.1:4060'
        data['app_id'] = str(uuid4())
        path.write_text(json.dumps(data))
        assert client.get('/monitor/api/first/tasks', headers=INTENT).status_code == 403
