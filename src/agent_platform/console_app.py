"""Loopback-only operator console. Service credentials never leave the server."""
import os
import re
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import HTTPException, Request
from fastapi.responses import FileResponse, Response

from .app import create_app
from .service_identity import provision
from .workspace_shell import install_bridge, page

ASSETS = Path(__file__).with_name('console')
PATH = re.compile(r'(?:integrations/(?:task-readiness|capability-readiness|capabilities)|orchestration/automations(?:/[a-zA-Z0-9-]+(?:/(?:approve|runs))?)?|connectors/feeds(?:/(?:profiles|[a-zA-Z0-9-]+)(?:/(?:credentials|test))?)?|(task-agents|tasks)(/[a-zA-Z0-9-]+){0,2}|telephony/(capabilities|connections)(/[a-zA-Z0-9-]+){0,2}|memory/(settings|metrics|records|candidates|search|events|feedback|index|examples|evaluations|dataset)(/[a-zA-Z0-9-]+){0,2}|knowledge/(documents(?:/[a-f0-9-]{36})?|ask|search)|voice/(capabilities|usage|agents|sessions)(/[a-zA-Z0-9-]+){0,3}|calling/(capabilities|agents)(/[a-zA-Z0-9-]+){0,4}|whatsapp/(setup|channels|connections|onboarding)(/[a-zA-Z0-9-]+){0,4})$')


def local(request, *, mutation=False):
    if request.client.host not in {'127.0.0.1', '::1', 'testclient'} or request.url.hostname not in {'127.0.0.1', 'localhost', 'testserver'}:
        raise HTTPException(403, 'Console is available only on this computer')
    origin = request.headers.get('origin')
    if (origin and origin != str(request.base_url).rstrip('/')) or request.headers.get('sec-fetch-site')=='cross-site':
        raise HTTPException(403, 'Cross-origin access denied')
    if mutation and request.headers.get('x-console-intent') != 'user-action':
        raise HTTPException(403, 'Use the console to request this action')


def create_console(*, registry=None, app=None):
    from .browser_runner import BrowserSearch, install_browser
    from .decision_tool import default_registry
    registry = registry if registry is not None else default_registry()
    application = app or create_app(task_registry=registry)
    from .self_hosting import install as install_self_hosting
    install_self_hosting(application, local)
    prior = application.router.lifespan_context

    @asynccontextmanager
    async def lifespan(instance):
        async with prior(instance):
            from psycopg.rows import dict_row
            from psycopg_pool import AsyncConnectionPool

            from .db_scope import assert_runtime_role, worker_connection
            url = os.environ.get('AI_PLATFORM_OPERATOR_DATABASE_URL')
            if not url:
                raise RuntimeError('Local console requires a separate operator database credential')
            pool = instance.state.repository.pool.operator_pool
            if pool is None:
                pool = AsyncConnectionPool(url,open=False,kwargs={'row_factory':dict_row},configure=worker_connection)
                await pool.open()
                await pool.wait(timeout=10)
                await assert_runtime_role(pool,'worker')
                instance.state.repository.pool.operator_pool = pool
            registry.adapters['browser.wikipedia_search'] = BrowserSearch(pool)
            async with pool.connection() as conn:
                existing = await (await conn.execute("SELECT id FROM platform_apps WHERE tenant_id='local-console' AND name='Console' ORDER BY created_at LIMIT 1")).fetchone()
            credential = await provision(pool, tenant='local-console', organisation='Local operator', app_name='Console',
                app_id=existing['id'] if existing else None, scopes=['agents:read', 'agents:write', 'agents:run',
                    'tasks:read', 'tasks:write', 'tasks:approve', 'channels:read', 'channels:write',
                    'calling:read', 'calling:write',
                    'memory:read', 'memory:write', 'memory:review', 'knowledge:read', 'knowledge:write', 'knowledge:publish', 'telephony:read', 'telephony:write'])
            instance.state.console_credential = credential['credential']
            instance.state.console_app_id = credential['app_id']
            async with pool.connection() as conn:
                for tool in ('decision.compare', 'weather.forecast', 'knowledge.answer', *(['feed.read'] if 'feed.read' in registry.adapters else [])):
                    await conn.execute('INSERT INTO platform_tool_grants(app_id,tool) VALUES(%s,%s) ON CONFLICT DO NOTHING',
                                       (credential['app_id'], tool))
            try:
                yield
            finally:
                async with pool.connection() as conn:
                    await conn.execute('UPDATE platform_service_keys SET revoked_at=now() WHERE id=%s', (credential['key_id'],))
                await pool.close()
    application.router.lifespan_context = lifespan

    @application.get('/')
    async def home(request: Request):
        local(request)
        return page('dashboard.html', '/')

    @application.get('/setup')
    async def setup_home(request: Request):
        local(request)
        return page('setup.html', '/setup')

    @application.get('/workspace')
    async def builder(request: Request):
        local(request)
        return page('index.html', '/workspace')

    @application.get('/connectors')
    async def feed_home(request: Request):
        local(request)
        return page('feeds.html', request.url.path)

    @application.get('/memory')
    async def memory_home(request: Request):
        local(request)
        return page('memory.html', request.url.path)

    @application.get('/whatsapp')
    async def whatsapp_home(request: Request):
        local(request)
        return page('whatsapp.html', request.url.path)

    @application.get('/whatsapp/create')
    async def whatsapp_customer(request: Request):
        local(request)
        return page('whatsapp-customer.html', request.url.path)

    from .whatsapp_console import install_connection_form
    install_connection_form(application, local)

    @application.get('/voice')
    async def voice_home(request: Request):
        local(request)
        return page('voice.html', request.url.path)

    @application.get('/telephony')
    async def telephony_home(request: Request):
        local(request)
        return page('telephony.html', request.url.path)

    @application.get('/calling')
    async def calling_home(request: Request):
        local(request)
        return page('calling.html', request.url.path)

    @application.get('/whatsapp/send')
    async def whatsapp_send(request: Request):
        local(request)
        return page('whatsapp-send.html', request.url.path)

    @application.get('/console/{asset}')
    async def asset(asset: str, request: Request):
        local(request)
        if asset not in {'setup.js', 'workspace.css', 'feeds.js', 'memory.js', 'app.js', 'style.css', 'whatsapp.js', 'whatsapp.css', 'whatsapp-customer.js', 'whatsapp-send.js', 'calling.js', 'voice.js', 'voice-widget.js', 'voice-errors.js', 'telephony.js'}:
            raise HTTPException(404)
        return FileResponse(ASSETS/asset)

    @application.api_route('/console/api/{path:path}', methods=['GET', 'POST', 'PUT', 'PATCH', 'DELETE'])
    async def proxy(path: str, request: Request):
        local(request, mutation=True)
        if not PATH.fullmatch(path) or path.endswith('/stream'):
            raise HTTPException(404)
        payload = await request.body()
        if len(payload) > 200_000:
            raise HTTPException(413)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url='http://internal') as client:
            response = await client.request(request.method, '/v1/'+path, params=request.query_params,
                headers={'Authorization': 'Bearer '+application.state.console_credential,
                         'X-External-User-ID': 'operator', 'Content-Type': 'application/json'}, content=payload)
        return Response(response.content, status_code=response.status_code, media_type='application/json',
                        headers={'Cache-Control': 'no-store'})
    install_bridge(application, local)
    install_browser(application, registry)
    return application
