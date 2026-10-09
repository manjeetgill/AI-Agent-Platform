"""Enumerate hidden and flag-enabled routes; public exceptions are exact paths."""
import re
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from agent_platform.app import create_app

# These surfaces use signed webhook bodies, expiring invitation/session tokens,
# or intentionally public static content. No broad /public/* wildcard exemption.
PUBLIC = {
    '/health', '/readiness', '/about', '/privacy', '/data-deletion', '/app-icon.png',
    '/voice/call', '/voice/assets/{name}', '/public/voice/invitation',
    '/public/voice/session', '/public/voice/session/{ident}', '/public/voice/session/{ident}/end',
    '/webhooks/whatsapp/{channel_id}', '/webhooks/meta',
    '/connect/whatsapp-assets/{asset}', '/connect/whatsapp/{session_id}',
    '/connect/whatsapp/{session_id}/bootstrap', '/connect/whatsapp/{session_id}/status',
    '/connect/whatsapp/{session_id}/code', '/connect/whatsapp/{session_id}/assets',
    '/connect/whatsapp/{session_id}/cancel', '/connect/whatsapp/{session_id}/retry',
    '/v1/notifications/email/webhooks/{app_id}/{provider}',
}


def routes(items, prefix=''):
    for route in items:
        if hasattr(route, 'original_router'):
            yield from routes(route.original_router.routes, prefix + route.include_context.prefix)
        else:
            yield prefix + route.path, route.methods


@pytest.mark.parametrize('environment', ['production', 'staging', '', 'Development'])
def test_schema_hidden_outside_explicit_development(monkeypatch, environment):
    monkeypatch.setenv('AI_PLATFORM_ENV', environment)
    with TestClient(create_app(repository=SimpleNamespace(), signing_key='a'*48)) as client:
        assert client.get('/openapi.json').status_code == 404


def test_schema_available_only_in_development(monkeypatch):
    monkeypatch.setenv('AI_PLATFORM_ENV', 'development')
    with TestClient(create_app(repository=SimpleNamespace(), signing_key='a'*48)) as client:
        assert client.get('/openapi.json').status_code == 200


def test_every_route_rejects_anonymous_and_invalid_credentials(monkeypatch):
    monkeypatch.setenv('AI_PLATFORM_ENV', 'production')
    for flag in ('AI_AUTOMATIONS', 'AI_EMAIL_NOTIFICATIONS', 'AI_DURABLE_TASKS', 'AI_TASK_AGENTS',
                 'AI_BROWSER_TASKS', 'AI_RAG', 'AI_MEMORY', 'AI_VISION', 'AI_WORKFLOWS',
                 'AI_WEB_VOICE_ENABLED', 'AI_WHATSAPP_CALLING_ENABLED', 'AI_RETAILER_BROWSER', 'AI_LOCAL_MONITOR'):
        monkeypatch.setenv(flag, '1')
    app = create_app(repository=SimpleNamespace(), signing_key='a'*48)
    with TestClient(app, base_url='https://external.example', client=('203.0.113.1', 1234)) as client:
        for template, methods in routes(app.routes):
            if template in PUBLIC:
                continue
            path = re.sub(r'\{[^}]+\}', '00000000-0000-4000-8000-000000000001', template)
            for method in methods - {'HEAD', 'OPTIONS'}:
                for headers in ({}, {'Authorization': 'Bearer invalid'}):
                    response = client.request(method, path, headers=headers, json={})
                    assert response.status_code in {401, 403}, (method, template, response.status_code)


def test_future_api_route_inherits_authentication(monkeypatch):
    app = create_app(repository=SimpleNamespace(), signing_key='a'*48)

    @app.get('/v1/accidentally-unprotected')
    def new_route():
        return {'private': True}

    with TestClient(app) as client:
        assert client.get('/v1/accidentally-unprotected').status_code == 401
        assert client.get('/v1/accidentally-unprotected', headers={'Authorization': 'Bearer invalid'}).status_code == 401
