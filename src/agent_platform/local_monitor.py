"""Loopback operator UI; every task read/control still uses a scoped service key."""
import asyncio
import json
import os
import re
from pathlib import Path
from uuid import UUID

import httpx
from fastapi import HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response

from .flags import enabled
from .service_identity import authenticate

ASSETS = Path(__file__).with_name('console')
CSP = "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; form-action 'self'"
READ = re.compile(r'(?:tasks(?:/summary|/[a-f0-9-]{36}(?:/events)?)?|integrations/(?:task-readiness|capability-readiness)|orchestration/usage|connectors(?:/reports)?|notifications/(?:email|sms)(?:/usage|/configuration|/templates|/[a-f0-9-]{36})?)$')
WRITE = re.compile(r'tasks/[a-f0-9-]{36}/(?:pause|resume|cancel|reconcile)$')


def local(request, *, api=False):
    if not enabled('AI_LOCAL_MONITOR'):
        raise HTTPException(404, 'Local monitoring is disabled')
    if (not request.client or request.client.host not in {'127.0.0.1', '::1', 'testclient'}
            or request.url.hostname not in {'127.0.0.1', 'localhost', 'testserver'}):
        raise HTTPException(403, 'Monitor is available only on this computer')
    origin = request.headers.get('origin')
    if (origin and origin != str(request.base_url).rstrip('/')) or request.headers.get('sec-fetch-site') == 'cross-site':
        raise HTTPException(403, 'Cross-origin access denied')
    if api and request.headers.get('x-monitor-intent') != 'user-action':
        raise HTTPException(403, 'Use the local monitor for this operation')


def private_json(path):
    path = Path(path)
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise ValueError()
    if path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o077 or path.stat().st_size > 64000:
        raise ValueError()
    return json.loads(path.read_text())


def connections():
    try:
        config = private_json(os.environ.get('AI_LOCAL_MONITOR_CONNECTIONS', ''))
        if not isinstance(config, dict) or not isinstance(config.get('connections'), list) or len(config['connections']) > 30:
            raise ValueError()
        profiles = {}
        for row in config['connections']:
            if (not re.fullmatch(r'[a-z][a-z0-9-]{0,59}', row['id']) or row['id'] in profiles
                    or not isinstance(row['name'], str) or not 1 <= len(row['name']) <= 100):
                raise ValueError()
            profiles[row['id']] = row
        return profiles
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        raise HTTPException(503, 'Operator: configure the private local monitor connections file (0600).') from None


def credential(profile):
    try:
        data = private_json(profile['client_file'])
        if data['url'].rstrip('/') not in {'http://127.0.0.1:4060', 'http://localhost:4060'}:
            raise ValueError()
        UUID(data['app_id'])
        if not isinstance(data['credential'], str) or not isinstance(data['external_user'], str):
            raise TypeError()
        return data
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        raise HTTPException(503, 'Operator: fix this connection’s private local client file (0600).') from None


def install(app):
    @app.get('/monitor', include_in_schema=False)
    async def home(request: Request):
        local(request)
        return FileResponse(ASSETS / 'monitor.html', headers={'Cache-Control': 'no-store', 'Content-Security-Policy': CSP})

    @app.get('/monitor/sms', include_in_schema=False)
    async def sms_home(request: Request):
        local(request)
        return FileResponse(ASSETS / 'sms.html', headers={'Cache-Control': 'no-store', 'Content-Security-Policy': CSP})

    @app.get('/monitor/email', include_in_schema=False)
    async def email_home(request: Request):
        local(request)
        return FileResponse(ASSETS / 'email.html', headers={'Cache-Control': 'no-store', 'Content-Security-Policy': CSP})

    @app.get('/monitor/assets/{asset}', include_in_schema=False)
    async def asset(asset: str, request: Request):
        local(request)
        if asset not in {'monitor.js', 'monitor-model.mjs', 'monitor.css', 'onboard.js', 'email.js', 'sms.js'}:
            raise HTTPException(404)
        return FileResponse(ASSETS / asset, headers={'Cache-Control': 'no-store'})

    @app.get('/monitor/connections', include_in_schema=False)
    async def inventory(request: Request):
        local(request, api=True)
        return {'connections': [{'id': p['id'], 'name': p['name']} for p in connections().values()]}

    @app.api_route('/monitor/api/{connection}/{path:path}', methods=['GET', 'POST'], include_in_schema=False)
    async def proxy(connection: str, path: str, request: Request):
        local(request, api=True)
        if not (READ.fullmatch(path) if request.method == 'GET' else WRITE.fullmatch(path)):
            raise HTTPException(404, 'Operation unavailable in the monitor')
        profile = connections().get(connection)
        if profile is None:
            raise HTTPException(404, 'Connection not configured')
        config = credential(profile)
        # The browser cannot select an arbitrary app, owner, endpoint or key.
        headers = {'Authorization': 'Bearer ' + config['credential'], 'X-External-User-ID': config['external_user']}
        try:
            async with asyncio.timeout(12):
                claims = await authenticate(app.state.repository.pool, headers['Authorization'],
                                            headers['X-External-User-ID'], None)
                if claims['app_id'] != config['app_id']:
                    raise HTTPException(403, 'Client application identity does not match its credential')
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://internal') as client:
                    response = await client.request(request.method, '/v1/' + path, headers=headers,
                                                    params=request.query_params)
        except HTTPException:
            raise
        except Exception:  # noqa: BLE001 -- local paths and credentials never reach browser errors
            raise HTTPException(503, 'Platform unavailable; refresh readiness before trying an action again.') from None
        if response.status_code == 200 and request.method == 'GET' and re.fullmatch(r'tasks/[a-f0-9-]{36}', path):
            return JSONResponse({**response.json(), 'monitor_can_control': 'tasks:write' in claims['scopes']},
                                headers={'Cache-Control': 'no-store'})
        return Response(response.content, status_code=response.status_code, media_type='application/json',
                        headers={'Cache-Control': 'no-store'})
