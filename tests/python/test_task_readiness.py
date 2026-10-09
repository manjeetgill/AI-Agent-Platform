"""Readiness must be current, scoped, actionable and free of provider side effects."""
import asyncio
from contextlib import contextmanager
from uuid import uuid4

import pytest
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from test_connector_checks import setup
from test_task_agents import database, sql

from agent_platform.worker_readiness import heartbeat, reporting

PATH = '/v1/integrations/task-readiness'


@contextmanager
def live_worker(registry, renderer=False):
    identifier = uuid4()
    async def write():
        async with AsyncConnectionPool(database(), open=False, kwargs={'row_factory': dict_row}) as pool:
            await heartbeat(pool, identifier, registry, renderer)
    asyncio.run(write())
    try:
        yield identifier
    finally:
        sql('DELETE FROM platform_worker_heartbeats WHERE instance_id=%s', (identifier,))


def get(client, headers, tool='decision.compare', **params):
    response = client.get(PATH, headers=headers, params={'required_tool': tool, 'planning': False, **params})
    assert response.status_code == 200, response.text
    return response.json()


def checks(value):
    return {c['code']: c for c in value['checks']}


def test_no_worker_stale_worker_version_mismatch_and_recovery(monkeypatch):
    with setup(monkeypatch) as (client, headers, _, registry, probe):
        result = get(client, headers)
        assert not result['ready'] and checks(result)['task_worker']['status'] == 'BLOCKED'
        with live_worker(registry) as identifier:
            assert get(client, headers)['ready']
            sql("UPDATE platform_worker_heartbeats SET last_seen_at=now()-interval '46 seconds' WHERE instance_id=%s", (identifier,))
            assert not get(client, headers)['ready']
            sql("UPDATE platform_worker_heartbeats SET last_seen_at=now(),tool_versions='{}' WHERE instance_id=%s", (identifier,))
            assert not get(client, headers)['ready']
        with live_worker(registry), live_worker(registry) as mismatched:
            sql("UPDATE platform_worker_heartbeats SET tool_versions='{}' WHERE instance_id=%s", (mismatched,))
            assert not get(client, headers)['ready']
        assert probe.calls == 0


def test_planning_configuration_quota_and_runtime_flags(monkeypatch):
    monkeypatch.delenv('NVIDIA_API_KEY', raising=False)
    with setup(monkeypatch) as (client, headers, key, registry, _), live_worker(registry):
        assert get(client, headers)['ready']  # Saved plans need no planning model.
        value = get(client, headers, planning=True)
        assert checks(value)['planning_model']['status'] == 'BLOCKED'
        monkeypatch.setenv('NVIDIA_API_KEY', 'private-fixture-key')
        monkeypatch.setenv('AI_PLATFORM_NVIDIA_FAST_MODEL', 'fixture-model')
        value = get(client, headers, planning=True)
        assert value['ready'] and not value['model']['live_verified']
        assert 'private-fixture-key' not in str(value)
        assert not sql('SELECT id FROM platform_model_calls WHERE app_id=%s', (key['app_id'],))
        sql('INSERT INTO app_planning_usage(app_id,calls) VALUES(%s,500)', (key['app_id'],))
        assert checks(get(client, headers, planning=True))['planning_quota']['status'] == 'BLOCKED'
        assert get(client, headers)['ready']
        monkeypatch.setenv('AI_TASK_AGENTS', '0')
        assert checks(get(client, headers))['task_runtime']['status'] == 'BLOCKED'


def test_missing_grant_missing_adapter_and_scopes_are_distinct(monkeypatch):
    with setup(monkeypatch) as (client, headers, key, registry, _), live_worker(registry):
        value = get(client, headers, tool='ungranted.tool')
        assert value['missing_tool_grants'] == ['ungranted.tool']
        sql('INSERT INTO platform_tool_grants(app_id,tool) VALUES(%s,%s)', (key['app_id'], 'missing.adapter'))
        value = get(client, headers, tool='missing.adapter')
        assert value['missing_adapters'] == ['missing.adapter'] and not value['missing_tool_grants']
        sql("UPDATE platform_service_keys SET scopes=ARRAY['tasks:read'] WHERE id=%s", (key['key_id'],))
        value = get(client, headers)
        assert value['missing_scopes'] == ['agents:run'] and not value['ready']


def test_credentials_expiry_revocation_and_auth_contract(monkeypatch):
    with setup(monkeypatch) as (client, headers, key, registry, _), live_worker(registry):
        assert client.get(PATH).status_code == 401
        assert client.get(PATH, headers={**headers, 'X-External-User-ID': ''}).status_code == 422
        sql("UPDATE platform_service_keys SET expires_at=now()+interval '1 day' WHERE id=%s", (key['key_id'],))
        assert checks(get(client, headers))['credential']['status'] == 'WARNING'
        sql('UPDATE platform_service_keys SET revoked_at=now() WHERE id=%s', (key['key_id'],))
        assert client.get(PATH, headers=headers).status_code == 401


def test_connector_owner_and_application_isolation_and_no_global_block(monkeypatch):
    with setup(monkeypatch) as (client, headers, key, registry, _), live_worker(registry):
        get(client, headers)
        owner = sql('SELECT owner_id FROM platform_external_users WHERE app_id=%s', (key['app_id'],))[0]['owner_id']
        tenant = sql('SELECT tenant_id FROM platform_apps WHERE id=%s', (key['app_id'],))[0]['tenant_id']
        sql('''INSERT INTO connector_check_state(tenant_id,app_id,owner_id,tool,source,needs_attention)
            VALUES(%s,%s,%s,'fixture.catalog','private-source',true)''', (tenant, key['app_id'], owner))
        assert get(client, headers)['ready']
        value = get(client, headers, tool='fixture.catalog')
        assert value['ready'] and checks(value)['connectors']['status'] == 'WARNING'
        value = get(client, headers, tool='fixture.catalog', source='private-source')
        assert not value['ready'] and checks(value)['connectors']['status'] == 'BLOCKED'
        other = get(client, {**headers, 'X-External-User-ID': 'other'}, tool='fixture.catalog')
        assert not other['connectors'] and 'private-source' not in str(other)
        with setup(monkeypatch) as (second, second_headers, _, _, _):
            assert not get(second, second_headers, tool='fixture.catalog')['connectors']


def test_renderer_readiness_and_bounded_parameters(monkeypatch):
    monkeypatch.setenv('AI_RETAILER_BROWSER', '1')
    with setup(monkeypatch) as (client, headers, key, registry, _):
        sql("INSERT INTO platform_tool_grants(app_id,tool) VALUES(%s,'retailer.observe')", (key['app_id'],))
        with live_worker(registry):
            assert checks(get(client, headers, tool='retailer.observe'))['server_renderer']['status'] == 'BLOCKED'
        with live_worker(registry, renderer=True):
            assert get(client, headers, tool='retailer.observe')['ready']
        assert client.get(PATH, headers=headers, params={'source': 'foo'}).status_code == 422
        assert client.get(PATH, headers=headers, params={'required_tool': 'x' * 101}).status_code == 422
        assert client.get(PATH, headers=headers, params=[('required_tool', 'x')] * 21).status_code == 422


def test_reporting_removes_heartbeat_on_cancellation(monkeypatch):
    with setup(monkeypatch) as (_, _, _, registry, _):
        async def exercise():
            async with AsyncConnectionPool(database(), open=False, kwargs={'row_factory': dict_row}) as pool:
                with pytest.raises(asyncio.CancelledError):
                    async with reporting(pool, registry):
                        async with pool.connection() as conn:
                            assert (await (await conn.execute('SELECT count(*) AS n FROM platform_worker_heartbeats')).fetchone())['n'] == 1
                        raise asyncio.CancelledError()
                async with pool.connection() as conn:
                    assert (await (await conn.execute('SELECT count(*) AS n FROM platform_worker_heartbeats')).fetchone())['n'] == 0
        asyncio.run(exercise())


def test_feed_readiness_requires_current_profile_and_authorization(monkeypatch):
    from test_feed_connections import SECRET, connect, save
    from test_feed_connections import setup as feed_setup
    with feed_setup(monkeypatch) as (client, headers, _key, registry), live_worker(registry):
        row = connect(client, headers)
        assert checks(get(client, headers, tool='feed.read'))['feed_connections']['status'] == 'BLOCKED'
        save(client, headers, row)
        # Saved fixture verification, without contacting a real provider.
        sql("UPDATE feed_connections SET state='READY' WHERE id=%s", (row['id'],))
        value = get(client, headers, tool='feed.read')
        assert checks(value)['feed_connections']['status'] == 'WARNING'
        assert value['feeds'][0]['credentials_configured'] and SECRET not in str(value)
        assert not get(client, {**headers, 'X-External-User-ID': 'other'}, tool='feed.read')['feeds']
        monkeypatch.setenv('AI_FEED_PROFILES', '{}')
        value = get(client, headers, tool='feed.read')
        assert checks(value)['feed_connections']['status'] == 'BLOCKED'
        assert not value['feeds'][0]['configuration_current']
