"""Opt-in local operator setup; credentials stay in private backend files."""
import fcntl
import hashlib
import json
import os
from contextlib import contextmanager
from pathlib import Path
from uuid import UUID, uuid4

import httpx
from fastapi import HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import Field, field_validator

from .app_catalogue import AppManifest, Policy, configure
from .connection_test import TOOL, Input, enqueue
from .flags import enabled
from .local_monitor import ASSETS, CSP, connections, credential, local, private_json
from .service_identity import SCOPES, authenticate, provision
from .task_readiness import assess
from .tasks import Strict, TaskError

DEFAULT_SCOPES = ['tasks:read', 'tasks:write', 'agents:read', 'agents:write', 'agents:run']


class Setup(Strict):
    request_id: UUID
    name: str = Field(min_length=2, max_length=100)
    external_user: str = Field(min_length=1, max_length=200)
    scopes: list[str] = Field(min_length=1, max_length=25)
    tools: list[str] = Field(min_length=1, max_length=100)
    days: int = Field(default=30, ge=1, le=90)
    policy: Policy = Field(default_factory=lambda: Policy(planning_calls_per_day=25,
        user_planning_calls_per_day=5, automation_runs_per_day=0, connector_checks_per_hour=30, feed_requests_per_hour=30))

    @field_validator('name', 'external_user')
    @classmethod
    def clean(cls, value):
        if not value.strip() or value != value.strip() or any(ord(c) < 32 for c in value):
            raise ValueError('Use nonempty text without control characters or surrounding spaces')
        return value


def guard(request):
    local(request, api=True)
    if not enabled('AI_LOCAL_ONBOARDING'):
        raise HTTPException(404, 'Local application setup is disabled')


def atomic(path, data):
    temporary = path.with_name(path.name+'.'+uuid4().hex+'.tmp')
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'w') as stream:
            json.dump(data, stream, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def setup_lock():
    config = Path(os.environ.get('AI_LOCAL_MONITOR_CONNECTIONS', ''))
    private_json(config)  # Require existing private operator configuration.
    directory = config.parent / 'applications'
    if directory.is_symlink():
        raise ValueError('Invalid application directory')
    directory.mkdir(mode=0o700, exist_ok=True)
    if directory.stat().st_uid != os.getuid() or directory.stat().st_mode & 0o077:
        raise ValueError('Application directory must be private')
    fd = os.open(directory / 'setup.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise HTTPException(409, 'Another setup is saving; retry this request shortly.') from None
        yield config, directory
    finally:
        os.close(fd)


async def create(pool, registry, body):
    if len(set(body.scopes)) != len(body.scopes) or not set(body.scopes) <= SCOPES or 'tasks:read' not in body.scopes:
        raise HTTPException(422, 'Select supported unique scopes including tasks:read')
    if len(set(body.tools)) != len(body.tools) or not set(body.tools) <= registry.adapters.keys():
        raise HTTPException(422, 'Select unique installed tools')
    fingerprint = hashlib.sha256(body.model_dump_json().encode()).hexdigest()
    with setup_lock() as (config, directory):
        profiles = connections()
        slug = 'app-' + str(body.request_id)
        if len(profiles) >= 30 and slug not in profiles:
            raise HTTPException(409, 'The local monitor supports up to 30 connections')
        path = directory / (str(body.request_id)+'.json')
        saved = private_json(path) if path.exists() else None
        if saved and saved.get('setup_hash') != fingerprint:
            raise HTTPException(409, 'Setup request already belongs to different settings')
        # A private journal is written before commit. After a crash, replay finds the
        # committed key, or safely reprovisions if the transaction rolled back.
        async with pool.connection() as conn, conn.transaction():
            exists = await (await conn.execute('SELECT id,revoked_at FROM platform_service_keys WHERE id=%s',
                       (saved['key_id'],))).fetchone() if saved else None
            if exists and exists['revoked_at']:
                raise HTTPException(409, 'This credential was revoked; use operator key rotation, not setup replay')
            if not exists:
                tenant = 'local-app-' + str(body.request_id)
                key = await provision(pool, tenant=tenant, organisation=body.name, app_name=body.name,
                                      scopes=body.scopes, days=body.days, connection=conn)
                await configure(pool, registry, AppManifest(app_id=key['app_id'], tools=body.tools, policy=body.policy),
                                connection=conn)
                saved = {**key, 'url': 'http://127.0.0.1:4060', 'external_user': body.external_user,
                         'setup_hash': fingerprint, 'scopes': body.scopes, 'tools': body.tools,
                         'policy': body.policy.model_dump(), 'lifetime_days': body.days}
                atomic(path, saved)
        profiles[slug] = {'id': slug, 'name': body.name, 'client_file': str(path)}
        atomic(config, {'connections': list(profiles.values())})
        return {'connection': slug, 'app_id': saved['app_id'], 'name': body.name,
                'credential_file': str(path), 'scopes': saved['scopes'], 'tools': saved['tools'],
                'policy': saved['policy'], 'monitor_url': '/monitor',
                'next_action': 'Load the private client file in your app backend, then test the connection.'}


def install(app, registry):
    from .service_options import Choice
    from .service_options import inventory as service_inventory
    from .service_options import save as service_save

    @app.get('/monitor/onboard', include_in_schema=False)
    async def home(request: Request):
        local(request)
        if not enabled('AI_LOCAL_ONBOARDING'):
            raise HTTPException(404)
        return FileResponse(ASSETS/'onboard.html', headers={'Cache-Control':'no-store','Content-Security-Policy':CSP})

    @app.get('/monitor/setup/catalog', include_in_schema=False)
    async def catalog(request: Request):
        guard(request)
        return {'scopes': sorted(SCOPES), 'default_scopes': DEFAULT_SCOPES,
                'default_policy': Setup.model_fields['policy'].default_factory().model_dump(),
                'tools': [{'name': name, 'version': tool.version, 'risk': tool.risk,
                           'description': getattr(tool, 'description', '')} for name, tool in sorted(registry.adapters.items())]}

    @app.post('/monitor/setup/apps', include_in_schema=False, status_code=201)
    async def save(body: Setup, request: Request):
        guard(request)
        try:
            return await create(app.state.repository.pool, registry, body)
        except HTTPException:
            raise
        except (ValueError, OSError, TaskError):
            raise HTTPException(503, 'Setup could not be saved. Check private operator files; retry the same request ID.') from None

    async def claims_for(request, connection, scope='tasks:read'):
        guard(request)
        profile = connections().get(connection)
        if not profile:
            raise HTTPException(404, 'Connection not configured')
        config = credential(profile)
        claims = await authenticate(app.state.repository.pool, 'Bearer '+config['credential'], config['external_user'], scope)
        if claims['app_id'] != config['app_id']:
            raise HTTPException(403, 'Application identity mismatch')
        return claims, config

    @app.get('/monitor/setup/{connection}/services', include_in_schema=False)
    async def read_services(connection: str, request: Request):
        # The loopback operator can manage setup without broadening the app's key scopes.
        claims, _ = await claims_for(request, connection, scope=None)
        return await service_inventory(app.state.repository.pool, claims)

    @app.post('/monitor/setup/{connection}/services', include_in_schema=False)
    async def write_services(connection: str, body: Choice, request: Request):
        claims, _ = await claims_for(request, connection, scope=None)
        claims = {**claims, 'principal': 'local-operator:'+claims['principal']}
        return await service_save(app.state.repository.pool, claims, body)

    @app.get('/monitor/setup/{connection}/check', include_in_schema=False)
    async def check(connection: str, request: Request, planning: bool = True):
        claims, config = await claims_for(request, connection)
        report = await assess(app.state.repository.pool, claims, registry, planning=planning)
        # Reuse scoped public endpoints; no callback URL or signing secret is exposed.
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://internal') as client:
            delivery = await client.get('/v1/orchestration/deliveries?limit=5', headers={
                'Authorization':'Bearer '+config['credential'], 'X-External-User-ID':config['external_user']})
        configured = []
        try:
            from .event_webhooks import destination
            mapping = json.loads(os.environ.get('AI_EVENT_WEBHOOK_DESTINATIONS', '{}')).get(claims['app_id'], {})
            for name in mapping:
                try:
                    destination(claims['app_id'], name)
                    configured.append(name)
                except (ValueError, TypeError, AttributeError):
                    continue
        except (ValueError, TypeError, AttributeError):
            pass
        subscriptions = []
        if delivery.status_code == 200:
            from .automation import scope
            async with app.state.repository.pool.connection() as conn, conn.transaction():
                await scope(conn, claims)
                subscriptions = await (await conn.execute('''SELECT destination,event_type,event_version FROM automation_webhooks
                    WHERE app_id=%s AND owner_id=%s AND enabled ORDER BY created_at LIMIT 50''',
                    (claims['app_id'], claims['user']))).fetchall()
        report['callbacks'] = {'live_verified': False,
            'status': 'UNAVAILABLE' if delivery.status_code != 200 else 'CONFIGURATION_REQUIRED' if not configured
                      else 'SUBSCRIPTION_REQUIRED' if not subscriptions else 'READY_FOR_LIVE_TEST',
            'configured_destinations': configured, 'subscriptions': subscriptions,
            'deliveries': delivery.json().get('deliveries', []) if delivery.status_code==200 else [],
            'next_action': 'Optional: configure an approved callback destination, subscribe through /v1/orchestration/webhooks, and verify receipt in your app. Polling and task event streams also work locally.'}
        report['worker_test_available'] = (TOOL in {t['tool'] for t in report.get('tools', [])}
                                          and {'tasks:write','agents:run'} <= set(claims['scopes']))
        return report

    @app.post('/monitor/setup/{connection}/test', include_in_schema=False, status_code=202)
    async def test(connection: str, body: Input, request: Request):
        claims, _ = await claims_for(request, connection, 'tasks:write')
        if not enabled('AI_DURABLE_TASKS') or not {'tasks:read','agents:run'} <= set(claims['scopes']):
            raise HTTPException(403, 'Enable durable tasks and grant tasks:read, tasks:write and agents:run')
        from psycopg.errors import UniqueViolation
        try:
            row = await enqueue(app.state.repository.pool, claims, body)
        except UniqueViolation:
            raise HTTPException(409, 'Another task is active for this user; wait for it to finish.') from None
        return {'task_id': str(row['id']), 'status': row['status']}
