import asyncio
import base64
import json
from contextlib import contextmanager
from datetime import UTC, datetime
from uuid import uuid4

import httpx
import pytest
from cryptography.exceptions import InvalidTag
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from test_connector_checks import setup as connector_setup
from test_durable_tasks import database
from test_task_agents import sql

from agent_platform.connector_runtime import install
from agent_platform.feed_config import Profile, public_address
from agent_platform.feed_crypto import unseal
from agent_platform.feed_http import FeedFailure, fetch
from agent_platform.feed_runtime import FeedRead
from agent_platform.task_store import TaskStore
from agent_platform.task_worker import tick

SECRET = 'fixture-private-feed-key-only'
PROFILE = {'name': 'Fixture API', 'url': 'https://feed.example/catalog', 'auth': 'bearer',
    'fields': {'source_url': '/url', 'observation_id': '/id', 'observed_at': '/time', 'price': '/price', 'currency': '/currency'},
    'require_price': True, 'alternatives': ['Use the provider approved export feed.']}


def item(number=1):
    return {'url': f'https://retailer.example/p/{number}', 'id': f'observation-{number}',
            'time': datetime.now(UTC).isoformat(), 'price': 100.0, 'currency': 'INR'}


@contextmanager
def setup(monkeypatch):
    with connector_setup(monkeypatch) as (client, headers, key, registry, _):
        monkeypatch.setenv('AI_FEED_PROFILES', json.dumps({key['app_id']: {'fixture': PROFILE}}))
        monkeypatch.setenv('AI_FEED_ENCRYPTION_KEY_ID', 'test')
        monkeypatch.setenv('AI_FEED_ENCRYPTION_KEYS', json.dumps({'test': base64.b64encode(b'x'*32).decode()}))
        sql('INSERT INTO platform_tool_grants(app_id,tool) VALUES(%s,%s)', (key['app_id'], 'feed.read'))
        try:
            yield client, headers, key, registry
        finally:
            sql('DELETE FROM feed_connections WHERE app_id=%s', (key['app_id'],))
            sql('DELETE FROM feed_request_usage WHERE app_id=%s', (key['app_id'],))


def connect(client, headers):
    profiles = client.get('/v1/connectors/feeds/profiles', headers=headers)
    assert profiles.status_code == 200, profiles.text
    profile = profiles.json()['profiles'][0]
    created = client.post('/v1/connectors/feeds', headers=headers, json={'profile': profile['id'], 'profile_hash': profile['profile_hash']})
    assert created.status_code == 201, created.text
    return created.json()


def save(client, headers, row):
    response = client.put('/v1/connectors/feeds/'+row['id']+'/credentials', headers=headers,
        json={'expected_revision': row['revision'], 'profile_hash': row['profile_hash'], 'secret': SECRET})
    assert response.status_code == 200, response.text
    assert SECRET not in response.text
    return response.json()


def work(registry, handler):
    async def run():
        async with AsyncConnectionPool(database(), open=False, kwargs={'row_factory': dict_row}) as pool:
            install(registry, pool)
            registry.adapters['feed.read'] = FeedRead(pool, registry, transport=httpx.MockTransport(handler), resolver=lambda host: '8.8.8.8')
            return await tick(TaskStore(pool), registry)
    return asyncio.run(run())


def test_private_setup_queues_worker_and_delivers_verified_feed(monkeypatch):
    with setup(monkeypatch) as (client, headers, key, registry):
        row = connect(client, headers)
        assert row['state'] == 'AUTHORIZATION_REQUIRED'
        saved = save(client, headers, row)
        dbrow = sql('SELECT * FROM feed_connections WHERE id=%s', (row['id'],))[0]
        assert SECRET not in dbrow['credential_cipher'] and unseal(dbrow['credential_cipher'], dbrow) == SECRET
        with pytest.raises(InvalidTag):
            unseal(dbrow['credential_cipher'], {**dbrow, 'owner_id': uuid4()})
        calls = []
        def provider(request):
            calls.append(request)
            assert request.url.host == '8.8.8.8' and request.headers['host'] == 'feed.example'
            assert request.headers['authorization'] == 'Bearer '+SECRET
            assert request.extensions['sni_hostname'] == 'feed.example'
            assert SECRET not in str(request.url)
            return httpx.Response(200, json={'items': [item()]})
        assert not calls
        assert work(registry, provider)
        task = client.get('/v1/tasks/'+saved['task_id'], headers=headers).json()
        assert task['status'] == 'DONE', task
        result = task['steps'][0]['result']
        assert result['usable_evidence'] and result['evidence'][0]['unknown_fees']
        assert SECRET not in json.dumps(task)
        listing = client.get('/v1/connectors/feeds', headers=headers)
        assert listing.json()['connections'][0]['state'] == 'READY' and SECRET not in listing.text
        assert sql('SELECT requests FROM feed_request_usage WHERE app_id=%s', (key['app_id'],))[0]['requests'] == 1
        other = {**headers, 'X-External-User-ID': 'other'}
        assert not client.get('/v1/connectors/feeds', headers=other).json()['connections']
        assert client.delete('/v1/connectors/feeds/'+row['id'], headers=other).status_code == 404
        assert client.delete('/v1/connectors/feeds/'+row['id'], headers=headers).status_code == 200
        assert sql('SELECT credential_cipher FROM feed_connections WHERE id=%s', (row['id'],))[0]['credential_cipher'] is None


def test_rejected_credential_refresh_clears_block_and_retests(monkeypatch):
    with setup(monkeypatch) as (client, headers, key, registry):
        row = connect(client, headers); save(client, headers, row)
        assert work(registry, lambda request: httpx.Response(401))
        row = client.get('/v1/connectors/feeds', headers=headers).json()['connections'][0]
        assert row['state'] == 'AUTHORIZATION_REQUIRED'
        assert sql('SELECT needs_attention FROM connector_check_state WHERE app_id=%s', (key['app_id'],))[0]['needs_attention']
        # A repeated test must keep the authorization instruction, not spin in
        # TESTING or tell the user that waiting alone will fix a rejected key.
        repeated = client.post('/v1/connectors/feeds/'+row['id']+'/test', headers=headers,
            json={'expected_revision':row['revision'], 'idempotency_key':uuid4().hex})
        assert repeated.status_code == 202
        sql('UPDATE chore_jobs SET available_at=now() WHERE task_id=%s', (repeated.json()['task_id'],))
        calls = []
        work(registry, lambda request: calls.append(request))
        after = client.get('/v1/connectors/feeds', headers=headers).json()['connections'][0]
        assert not calls and after['state'] == 'AUTHORIZATION_REQUIRED'
        assert after['recovery_options'][0]['action'] == 'authorize'
        saved = save(client, headers, row)
        assert not sql('SELECT needs_attention FROM connector_check_state WHERE app_id=%s', (key['app_id'],))[0]['needs_attention']
        sql('UPDATE connector_check_state SET next_check_at=now() WHERE app_id=%s', (key['app_id'],))
        sql('UPDATE chore_jobs SET available_at=now() WHERE task_id=%s', (saved['task_id'],))
        work(registry, lambda request: httpx.Response(200, json={'items': [item()]}))
        assert client.get('/v1/connectors/feeds', headers=headers).json()['connections'][0]['state'] == 'READY'


def test_credentials_never_echo_on_invalid_input_and_changed_profile_stops_queued_work(monkeypatch):
    with setup(monkeypatch) as (client, headers, key, registry):
        row = connect(client, headers)
        invalid = client.put('/v1/connectors/feeds/'+row['id']+'/credentials', headers=headers,
            json={'secret': SECRET, 'expected_revision': 'bad'})
        assert invalid.status_code == 422 and SECRET not in invalid.text
        save(client, headers, row)
        monkeypatch.setenv('AI_FEED_PROFILES', json.dumps({key['app_id']: {'fixture': {**PROFILE, 'url': 'https://new.example/catalog'}}}))
        calls = []
        work(registry, lambda request: calls.append(request))
        assert not calls
        assert client.get('/v1/connectors/feeds', headers=headers).json()['connections'][0]['state'] == 'CONFIGURATION_REQUIRED'


def test_zero_request_quota_performs_no_http(monkeypatch):
    with setup(monkeypatch) as (client, headers, key, registry):
        row = connect(client, headers); save(client, headers, row)
        sql('INSERT INTO platform_app_policies(app_id,feed_requests_per_hour) VALUES(%s,0)', (key['app_id'],))
        calls = []
        work(registry, lambda request: calls.append(request))
        assert not calls
        assert client.get('/v1/connectors/feeds', headers=headers).json()['connections'][0]['state'] == 'RATE_LIMITED'


def transport_run(profile, handler, *, quota=100):
    attempts = []
    async def run():
        async def reserve():
            attempts.append(1)
            return len(attempts)<=quota
        async def authorized():
            return True
        result = await fetch(Profile.model_validate(profile), SECRET, reserve=reserve, authorized=authorized,
                             transport=httpx.MockTransport(handler), resolver=lambda host: '8.8.8.8')
        return result, len(attempts)
    return asyncio.run(run())


def test_cursor_pagination_retry_and_request_accounting():
    seen = []
    def provider(request):
        seen.append(str(request.url))
        if len(seen)==1:
            return httpx.Response(503)
        next_page = request.url.params.get('cursor') == 'two'
        return httpx.Response(200, json={'items': [item(2 if next_page else 1)], 'next_cursor': None if next_page else 'two'})
    result, attempts = transport_run({**PROFILE, 'pagination': 'cursor', 'page_parameter': 'cursor', 'max_pages': 3}, provider)
    assert len(result.observations)==2 and attempts==3 and result.reason_code=='feed_read'


@pytest.mark.parametrize('code,status', [(401,'LOGIN_REQUIRED'), (403,'BLOCKED'), (302,'BLOCKED'), (429,'RATE_LIMITED')])
def test_denied_access_never_retries_or_follows_redirects(code, status):
    calls = []
    def provider(request):
        calls.append(request)
        return httpx.Response(code, headers={'location':'https://evil.example/', 'retry-after':'120'})
    with pytest.raises(FeedFailure) as error:
        transport_run(PROFILE, provider)
    assert error.value.result.status == status and len(calls)==1


def test_limits_bad_evidence_and_reflected_credentials():
    with pytest.raises(FeedFailure) as error:
        transport_run(PROFILE, lambda request: httpx.Response(200, headers={'content-type':'application/json'}, content=b'x'*262145))
    assert error.value.result.reason_code == 'response_too_large'
    with pytest.raises(FeedFailure) as error:
        transport_run(PROFILE, lambda request: httpx.Response(200, json={'items':[], 'debug':SECRET}))
    assert error.value.result.reason_code == 'credential_in_response'
    escaped = ''.join('\\u' + format(ord(c), '04x') for c in SECRET)
    with pytest.raises(FeedFailure):
        transport_run(PROFILE, lambda request: httpx.Response(200, headers={'content-type':'application/json'},
            content=('{"items":[],"debug":"'+escaped+'"}').encode()))
    result, attempts = transport_run({**PROFILE, 'pagination':'page', 'next_pointer':'/has_more', 'max_pages':2},
                                    lambda request: httpx.Response(200, json={'items':[item()], 'has_more':True}))
    assert attempts==2 and result.reason_code=='page_limit_reached'
    with pytest.raises(FeedFailure):
        transport_run({**PROFILE, 'pagination':'cursor', 'max_pages':3},
                      lambda request: httpx.Response(200, json={'items':[], 'next_cursor':'repeated'}))


def test_public_destination_and_profile_validation(monkeypatch):
    import socket
    for address in ['127.0.0.1', '169.254.169.254', '10.0.0.1', '::1', '224.0.0.1']:
        monkeypatch.setattr(socket, 'getaddrinfo', lambda *args, address=address, **kwargs: [(2,1,6,'',(address,443))])
        with pytest.raises(ValueError):
            public_address('provider.example')
    for url in ['http://provider.example', 'https://user:pass@provider.example', 'https://provider.example?token=secret']:
        with pytest.raises(ValueError):
            Profile.model_validate({**PROFILE, 'url':url})


def test_feed_console_is_local_csrf_protected_and_shows_setup(monkeypatch):
    from fastapi.testclient import TestClient

    from agent_platform.app import create_app
    from agent_platform.console_app import create_console
    from agent_platform.decision_tool import default_registry
    monkeypatch.setenv('AI_AUTOMATIONS', '1')
    monkeypatch.setenv('AI_DURABLE_TASKS', '1')
    monkeypatch.setenv('AI_TASK_AGENTS', '1')
    monkeypatch.setenv('AI_PLATFORM_DATABASE_URL', database())
    registry = default_registry()
    app = create_console(registry=registry, app=create_app(signing_key='c'*48, task_registry=registry))
    with TestClient(app) as client:
        assert client.get('/connectors').status_code == 200
        assert 'Save privately and test' in client.get('/connectors').text
        assert client.get('/connectors', headers={'Host':'evil.example'}).status_code == 403
        assert client.get('/console/api/connectors/feeds/profiles').status_code == 403
        headers = {'X-Console-Intent':'user-action'}
        monkeypatch.setenv('AI_FEED_PROFILES', json.dumps({str(app.state.console_app_id): {'fixture': PROFILE}}))
        response = client.get('/console/api/connectors/feeds/profiles', headers=headers)
        assert response.status_code == 200 and len(response.json()['profiles'])==1
        assert client.get('/console/api/connectors/feeds/profiles', headers={**headers,'Origin':'https://evil.example'}).status_code == 403
        assert client.get('/console/feeds.js').status_code == 200


def test_page_requests_consume_quota_and_retries_are_bounded():
    calls = []
    def failure(request):
        calls.append(request)
        return httpx.Response(503)
    with pytest.raises(FeedFailure):
        transport_run(PROFILE, failure)
    assert len(calls)==3
    calls.clear()
    def page(request):
        calls.append(request)
        return httpx.Response(200,json={'items':[item()], 'has_more':True})
    with pytest.raises(FeedFailure) as error:
        transport_run({**PROFILE, 'pagination':'page', 'next_pointer':'/has_more', 'max_pages':5}, page, quota=1)
    assert error.value.result.reason_code=='app_request_limit' and len(calls)==1


def test_disabled_or_foreign_connection_never_sends_credentials(monkeypatch):
    with setup(monkeypatch) as (client, headers, _key, registry):
        row=connect(client, headers); save(client, headers, row)
        assert client.delete('/v1/connectors/feeds/'+row['id'], headers=headers).status_code==200
        calls=[]
        work(registry,lambda request:calls.append(request))
        assert not calls
        other={**headers, 'X-External-User-ID':'other-owner'}
        response=client.put('/v1/connectors/feeds/'+row['id']+'/credentials',headers=other,json={
            'expected_revision':3,'profile_hash':row['profile_hash'],'secret':SECRET})
        assert response.status_code==404 and SECRET not in response.text


def test_feed_schedule_listing_rotation_pauses_and_scopes_schedules(monkeypatch):
    with setup(monkeypatch) as (client, headers, key, registry):
        row = connect(client, headers); save(client, headers, row)
        work(registry, lambda request: httpx.Response(200, json={'items':[item()]}))
        row = client.get('/v1/connectors/feeds', headers=headers).json()['connections'][0]
        agent = client.post('/v1/task-agents', headers=headers, json={'idempotency_key': uuid4().hex,
            'blueprint': {'name':'Verify configured feed', 'description':'Test recurring connection checks',
            'steps':[{'tool':'feed.read','version':'1','arguments':{'connection_id':row['id'],
                'revision':row['revision'], 'profile_hash':row['profile_hash']}}]}})
        assert agent.status_code == 201, agent.text
        schedule = client.post('/v1/orchestration/automations', headers=headers, json={
            'idempotency_key':uuid4().hex, 'definition':{'agent_id':agent.json()['id'], 'name':'Hourly feed',
            'trigger':{'type':'interval','interval_seconds':3600}, 'max_runs':24}})
        assert schedule.status_code == 201, schedule.text
        schedule = schedule.json()
        assert client.post('/v1/orchestration/automations/'+schedule['id']+'/approve',headers=headers,
            json={'decision':'yes','action_hash':schedule['definition_hash']}).status_code == 200
        listing = client.get('/v1/connectors/feeds',headers=headers).json()['connections'][0]
        assert listing['schedules'][0]['status']=='ACTIVE'
        assert listing['schedules'][0]['connection_arguments']['revision']==row['revision']
        other = {**headers,'X-External-User-ID':'other-owner'}
        assert client.get('/v1/connectors/feeds',headers=other).json()['connections']==[]
        save(client, headers, row)
        state = sql('SELECT status,approved_at FROM automations WHERE id=%s',(schedule['id'],))[0]
        assert state['status']=='PAUSED' and state['approved_at'] is None
        assert sql("SELECT payload FROM automation_events WHERE app_id=%s AND event_type='AutomationPaused'", (key['app_id'],))[0]['payload']['reason']=='feed_connection_changed'
        assert SECRET not in client.get('/v1/connectors/feeds',headers=headers).text
