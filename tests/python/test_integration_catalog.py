"""App discovery and delegation must not grant or activate capabilities implicitly."""
from typing import get_args

import pytest
from pydantic import ValidationError
from test_task_agents import environment, sql

from agent_platform.business_workspaces import FEATURE_SCOPES, Feature, KeyRequest
from agent_platform.integration_catalog import catalogue
from agent_platform.service_identity import SCOPES


def test_all_features_have_valid_permission_contracts_without_implicit_access():
    assert set(get_args(Feature)) == set(FEATURE_SCOPES) == {c['id'] for c in catalogue([])}
    for item in catalogue([]):
        assert not item['permissions_granted']
        assert set(item['missing_scopes']) == FEATURE_SCOPES[item['id']] <= SCOPES
        assert item['api_root'].startswith('/v1/')
    assert all(item['permissions_granted'] for item in catalogue(SCOPES))
    KeyRequest(features=list(FEATURE_SCOPES))
    with pytest.raises(ValidationError):
        KeyRequest(features=['invented_feature'])


def test_discovery_uses_caller_permissions_and_never_calls_model(monkeypatch):
    with environment(monkeypatch) as (client, headers, key, planner):
        path = '/v1/integrations/capabilities'
        assert client.get(path).status_code == 401
        report = client.get(path, headers=headers)
        assert report.status_code == 200, report.text
        assert report.json()['app_id'] == key['app_id']
        features = {c['id']: c for c in report.json()['capabilities']}
        assert features['agent_tasks']['permissions_granted']
        assert not features['email']['permissions_granted']
        assert features['email']['missing_scopes'] == ['notifications:read', 'notifications:send']
        assert key['credential'] not in report.text
        assert planner.calls == 0


def test_new_business_features_cannot_exceed_parent_or_inherit_its_tools(monkeypatch):
    with environment(monkeypatch) as (client, headers, key, _):
        sql('UPDATE platform_service_keys SET scopes=%s WHERE id=%s',
            (['channels:read', 'channels:write', 'notifications:read', 'notifications:send'], key['key_id']))
        created = client.post('/v1/businesses', headers=headers,
                              json={'name': 'Messaging business', 'external_id': 'messaging'})
        assert created.status_code == 201, created.text
        child = created.json()
        try:
            route = '/v1/businesses/' + child['id'] + '/credentials'
            issued = client.post(route, headers=headers, json={'features': ['email', 'sms']})
            assert issued.status_code == 201, issued.text
            child_headers = {'Authorization': 'Bearer '+issued.json()['credential'],
                             'X-External-User-ID': 'same-user'}
            denied = client.post(route, headers=headers, json={'features': ['agent_tasks']})
            assert denied.status_code == 403
            # Failed delegation must not revoke the previously valid key.
            report = client.get('/v1/integrations/capabilities', headers=child_headers)
            assert report.status_code == 200 and report.json()['app_id'] == child['app_id']
            assert not sql('SELECT tool FROM platform_tool_grants WHERE app_id=%s', (child['app_id'],))
            assert client.get('/v1/whatsapp/channels', headers=child_headers).status_code == 403
            inventory = client.get('/v1/integrations/readiness', headers=child_headers)
            assert inventory.status_code == 200, inventory.text
            assert all(c['status'] == 'ACCESS_REQUIRED' for c in inventory.json()['components'].values())
            assert inventory.json()['trial_policy'] is None
        finally:
            for table in ('platform_service_keys', 'platform_external_users'):
                sql(f'DELETE FROM {table} WHERE app_id=%s', (child['app_id'],))
            sql('DELETE FROM platform_apps WHERE id=%s', (child['app_id'],))
