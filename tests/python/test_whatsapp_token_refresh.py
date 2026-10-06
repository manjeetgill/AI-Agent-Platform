"""Refresh credentials without disclosing or replacing other saved secrets."""
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent_platform.console_app import local
from agent_platform.whatsapp_console import install_connection_form


def test_refresh_is_private_scoped_and_preserves_other_credentials(monkeypatch, tmp_path):
    path = tmp_path/'connections.json'
    original = {'app_id': 'owner', 'access_token': 'old-fixture-token', 'app_secret': 'private-app-secret',
                'verify_token': 'private-verify-token', 'graph_version': 'v26.0', 'waba_id': '123456789'}
    rows = {'123456': original, '999999': {**original, 'app_id': 'other'},
            '888888': {**original, 'provider': 'chakra'}}
    path.write_text(json.dumps(rows)); path.chmod(0o600)
    monkeypatch.setenv('AI_WHATSAPP_CONNECTIONS_FILE', str(path))
    monkeypatch.setenv('AI_WHATSAPP_CONNECTIONS', '{}')
    queries = []

    class Pool:
        @asynccontextmanager
        async def connection(self):
            yield self

        async def execute(self, query, args):
            queries.append((query, args))

    app = FastAPI(); app.state.console_app_id = 'owner'
    app.state.repository = SimpleNamespace(pool=Pool())
    install_connection_form(app, local)
    with TestClient(app) as client:
        headers = {'X-Console-Intent': 'user-action', 'Host': '127.0.0.1:4061'}
        response = client.get('/console/whatsapp/saved-connections', headers=headers)
        assert response.json() == {'connections': [{'phone_id': '123456', 'graph_version': 'v26.0'}]}
        endpoint = '/console/whatsapp/refresh-token'
        body = {'phone_id': '123456', 'access_token': 'replacement-fixture-token'}
        assert client.post(endpoint, json=body, headers={'Host': '127.0.0.1:4061'}).status_code == 403
        assert client.post(endpoint, json=body, headers={**headers, 'Origin': 'https://outside.example'}).status_code == 403
        assert client.post(endpoint, json={**body, 'phone_id': '999999'}, headers=headers).status_code == 404
        assert client.post(endpoint, json={**body, 'phone_id': '888888'}, headers=headers).status_code == 409
        invalid = client.post(endpoint, json={**body, 'phone_id': 'bad'}, headers=headers)
        assert invalid.status_code == 422 and body['access_token'] not in invalid.text
        assert json.loads(path.read_text()) == rows
        response = client.post(endpoint, json=body, headers=headers)
        assert response.status_code == 200 and body['access_token'] not in response.text
        expected = {**rows, '123456': {**original, 'access_token': body['access_token']}}
        assert json.loads(path.read_text()) == expected
        assert path.stat().st_mode & 0o077 == 0
        assert len(queries) == 1 and queries[0][1] == ('123456', 'owner')
        assert 'enabled=false' in queries[0][0] and 'webhook_verified_at' not in queries[0][0]
