"""App/owner-scoped preflight from saved state; never invokes models or providers."""
import asyncio
import re
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from .app_catalogue import policy
from .automation import scope
from .flags import enabled
from .gateway import model_configuration
from .migrate import schema_ready
from .service_identity import authenticate
from .task_agents import catalog
from .task_api import credential_contract
from .worker_readiness import FRESH_SECONDS


async def assess(pool, claims, registry, *, required_tools=(), planning=True, source=None):
    checks = []

    def check(code, status, summary, action=None):
        checks.append({'code': code, 'status': status, 'summary': summary, 'next_action': action})

    async with pool.connection() as conn, conn.transaction():
        await scope(conn, claims)
        if not await schema_ready(conn):
            return {'status': 'BLOCKED', 'ready': False, 'checks': [{
                'code': 'database_migrations', 'status': 'BLOCKED',
                'summary': 'Database migrations do not match this API.',
                'next_action': 'Operator: apply platform migrations, then restart API and worker together.'}],
                'provider_checks_performed': False}
        check('database_migrations', 'PASS', 'Database and migration checks passed.')
        clock = (await (await conn.execute('SELECT now() AS clock')).fetchone())['clock']
        credential = await (await conn.execute('SELECT expires_at FROM platform_service_keys WHERE id=%s AND app_id=%s',
            (UUID(claims['principal'].removeprefix('service:')), claims['app_id']))).fetchone()
        soon = (credential['expires_at'] - clock).total_seconds() < 3 * 86400
        check('credential', 'WARNING' if soon else 'PASS',
              'Credential expires within three days.' if soon else 'Application credential is valid.',
              'Operator: rotate the service key while preserving the application identity.' if soon else None)
        needed = {'agents:run', 'tasks:read'} | ({'agents:read', 'agents:write'} if planning else set())
        missing_scopes = sorted(needed - set(claims['scopes']))
        check('scopes', 'BLOCKED' if missing_scopes else 'PASS',
              'Missing scopes: ' + ', '.join(missing_scopes) if missing_scopes else 'Required execution scopes are granted.',
              'Operator: issue a credential with only the required missing scopes.' if missing_scopes else None)
        flags = enabled('AI_TASK_AGENTS') and (enabled('AI_DURABLE_TASKS') or enabled('AI_BROWSER_TASKS'))
        check('task_runtime', 'PASS' if flags else 'BLOCKED',
              'Task execution is enabled.' if flags else 'Task execution is disabled in this API.',
              None if flags else 'Operator: enable task agents and durable tasks, then restart the local services.')
        grants = {r['tool'] for r in await (await conn.execute(
            'SELECT tool FROM platform_tool_grants WHERE app_id=%s', (claims['app_id'],))).fetchall()}
        tools = catalog(registry, grants)
        installed = {t['tool']: t['version'] for t in tools}
        missing_grants = sorted(set(required_tools) - grants)
        missing_adapters = sorted((set(required_tools) & grants) - installed.keys())
        check('tool_grants', 'BLOCKED' if missing_grants or missing_adapters or not installed else 'PASS',
              ('Required tools are not granted or installed.' if missing_grants or missing_adapters else
               'Typed tools are available.' if installed else 'No granted typed tools are installed.'),
              'Operator: install and grant the requested tool package to this app.'
              if missing_grants or missing_adapters or not installed else None)
        workers = await (await conn.execute('''SELECT tool_versions,server_renderer FROM platform_worker_heartbeats
            WHERE last_seen_at>now()-(%s * interval '1 second')''', (FRESH_SECONDS,))).fetchall()
        required = set(required_tools) if required_tools else set(installed)
        # Task claiming is not routed by adapter version. Every online worker
        # must support a required tool, or a mismatched worker could claim it.
        compatible = {name: bool(workers) and name in installed and all(
            w['tool_versions'].get(name) == installed[name] for w in workers) for name in required}
        available = all(compatible.values()) if required_tools else any(compatible.values())
        check('task_worker', 'PASS' if available else 'BLOCKED',
              'Fresh workers support the requested tool versions.' if available else
              'Workers are offline or their tool versions do not all match this API.',
              None if available else 'Operator: start/restart the local task worker with the same adapters as the API.')
        renderer = any(w['server_renderer'] and w['tool_versions'].get('retailer.observe') == installed.get('retailer.observe')
                       for w in workers) if 'retailer.observe' in installed else False
        if 'retailer.observe' in required_tools:
            check('server_renderer', 'PASS' if renderer else 'BLOCKED',
                  'Server renderer loop and runtime files are available; browser launch is not tested here.' if renderer else
                  'Server renderer is disabled, offline or missing its local runtime.',
                  None if renderer else 'Operator: configure the browser runtime and enable AI_RETAILER_SERVER_BROWSER; restart the worker.')
        if 'browser.retailer_observe' in required_tools:
            paired = await (await conn.execute('''SELECT 1 FROM retailer_browser_devices d
                JOIN platform_service_keys k ON k.id=d.key_id
                WHERE d.app_id=%s AND d.owner_id=%s AND d.revoked_at IS NULL AND d.expires_at>now()
                AND k.revoked_at IS NULL AND k.expires_at>now() AND 'tasks:write'=ANY(k.scopes)
                AND d.last_seen_at>now()-interval '90 seconds'
                AND (%s::text IS NULL OR %s=ANY(d.retailers)) LIMIT 1''',
                (claims['app_id'], claims['user'], source, source))).fetchone()
            check('paired_browser', 'PASS' if paired else 'BLOCKED',
                  'A paired browser checked in recently.' if paired else 'No recently connected paired browser.',
                  None if paired else 'Use retailer.observe for server execution, or explicitly pair the optional browser.')
        limits = await policy(conn, claims['app_id'])
        usage = await (await conn.execute('''SELECT
            coalesce((SELECT calls FROM app_planning_usage WHERE app_id=%s AND day=CURRENT_DATE),0) AS app_calls,
            coalesce((SELECT calls FROM agent_planning_usage WHERE app_id=%s AND owner_id=%s AND day=CURRENT_DATE),0) AS user_calls''',
            (claims['app_id'], claims['app_id'], claims['user']))).fetchone()
        remaining = {'app': max(0, limits.planning_calls_per_day - usage['app_calls']),
                     'user': max(0, limits.user_planning_calls_per_day - usage['user_calls'])}
        model = model_configuration()
        recent = await (await conn.execute('''SELECT status,provider,model,created_at FROM platform_model_calls
            WHERE app_id=%s AND owner_id=%s AND operation='plan' ORDER BY created_at DESC LIMIT 1''',
            (claims['app_id'], claims['user']))).fetchone()
        if planning:
            check('planning_quota', 'PASS' if all(remaining.values()) else 'BLOCKED',
                  'Daily planning allowance is available.' if all(remaining.values()) else 'Daily planning allowance is exhausted or disabled.',
                  None if all(remaining.values()) else 'Wait for the daily quota reset or ask the operator to review this app’s limits.')
            check('planning_model', 'WARNING' if model['configured'] else 'BLOCKED',
                  'Model is configured; current provider availability has not been tested.' if model['configured'] else 'Planning model is not configured.',
                  'Run a normal reviewed planning request to verify provider access.' if model['configured'] else
                  'Operator: configure the model credential and model ID privately, then restart the API.')
        # Connector health is evidence for this owner only; an unrelated blocked
        # retailer must not disable catalogue search or another application's tasks.
        states = await (await conn.execute('''SELECT s.tool,s.source,s.needs_attention,s.next_check_at,
            r.result->>'status' AS status,r.result->>'reason_code' AS reason_code,r.checked_at,
            count(*) OVER() AS total FROM connector_check_state s
            LEFT JOIN LATERAL (SELECT result,checked_at FROM connector_check_reports r
                WHERE r.app_id=s.app_id AND r.owner_id=s.owner_id AND r.tool=s.tool AND r.source=s.source
                ORDER BY sequence DESC LIMIT 1) r ON true
            WHERE s.app_id=%s AND s.owner_id=%s AND s.tool=ANY(%s::text[])
            AND (%s::text IS NULL OR s.source=%s)
            ORDER BY s.needs_attention DESC,s.tool,s.source LIMIT 50''',
            (claims['app_id'], claims['user'], sorted(required & grants), source, source))).fetchall()
        attention = any(s['needs_attention'] for s in states)
        cooldown = bool(source and any(s['next_check_at'] > clock for s in states))
        unhealthy = any(s['status'] != 'WORKING' for s in states)
        check('connectors', 'BLOCKED' if source and (attention or cooldown) else 'WARNING' if attention or unhealthy or not states else 'PASS',
              'Selected source needs attention.' if source and attention else
              'Selected source is in cooldown; a check would be deferred.' if cooldown else
              'Some sources need attention; other tools can still run.' if attention else
              'No saved checks for the selected tools/source; provider access is unverified.' if not states else
              'Some saved checks were unsuccessful; inspect their reports before retrying.' if unhealthy else
              'Saved connector checks found; this does not verify current source availability.',
              'Review /v1/connectors/reports and resolve the source issue before explicitly resuming.' if attention else
              'Wait until the recorded next_check_at before submitting a new check.' if cooldown else
              'Inspect /v1/connectors/reports; preserve the recorded backoff.' if unhealthy else
              'Run a bounded connector check when needed.' if not states else None)
        feeds = await (await conn.execute('''SELECT id,profile,profile_hash,state,reason_code,disabled,
            credential_cipher IS NOT NULL AS credential_saved FROM feed_connections
            WHERE app_id=%s AND owner_id=%s AND (%s::text IS NULL OR id::text=%s)
            ORDER BY updated_at DESC LIMIT 50''',
            (claims['app_id'], claims['user'], source, source))).fetchall() if 'feed.read' in grants and (not required_tools or 'feed.read' in required_tools) else []
        if feeds:
            from .feed_config import profiles
            from .feed_crypto import keyring
            try:
                configured_profiles = profiles(claims['app_id'])
            except (ValueError, TypeError):
                configured_profiles = {}
            try:
                keyring()
                encryption = True
            except (ValueError, TypeError, AttributeError):
                encryption = False
            for feed in feeds:
                profile = configured_profiles.get(feed['profile'])
                saved_hash = feed.pop('profile_hash')
                feed['configuration_current'] = bool(profile and profile.fingerprint == saved_hash)
                feed['credentials_configured'] = bool(profile and (profile.auth == 'none' or (
                    encryption and feed['credential_saved'])))
                feed['ready_from_saved_state'] = bool(feed['configuration_current'] and feed['credentials_configured']
                                                     and not feed['disabled'] and feed['state'] == 'READY')
        if 'feed.read' in required_tools:
            feed_ready = any(f['ready_from_saved_state'] for f in feeds)
            check('feed_connections', 'WARNING' if feed_ready else 'BLOCKED',
                  'A feed passed its saved checks; current upstream access is unverified.' if feed_ready else
                  'No selected feed has current configuration, authorization and a successful saved test.',
                  'Use /v1/connectors/feeds/profiles to configure and test an approved connection.')
        blocked = any(c['status'] == 'BLOCKED' for c in checks)
        return {'app_id': claims['app_id'], 'checked_at': clock,
                'status': 'BLOCKED' if blocked else 'READY_WITH_WARNINGS' if any(c['status'] == 'WARNING' for c in checks) else 'READY',
                'ready': not blocked, 'checks': checks, 'required_tools': list(required_tools), 'planning_requested': planning,
                'credential_expires_at': credential['expires_at'], 'missing_scopes': missing_scopes,
                'missing_tool_grants': missing_grants, 'missing_adapters': missing_adapters,
                'tools': [{'tool': t['tool'], 'version': t['version'],
                           'worker_available': bool(workers) and all(w['tool_versions'].get(t['tool']) == t['version'] for w in workers),
                           'execution_location': t.get('execution_location', 'worker')} for t in tools],
                'workers': {'online': len(workers), 'freshness_seconds': FRESH_SECONDS, 'server_renderer_available': renderer},
                'model': {'configured': model['configured'], 'last_planning_call': recent, 'live_verified': False},
                'planning_calls_remaining': remaining, 'connectors': [{k: v for k, v in s.items() if k != 'total'} for s in states],
                'connectors_truncated': bool(states and states[0]['total'] > len(states)), 'feeds': feeds,
                'provider_checks_performed': False,
                'note': 'Preflight only. Grants, quotas, approvals and source state are rechecked during execution. Ready is not a guarantee of task success.'}


def install(app, registry):
    router = APIRouter(prefix='/v1/integrations', dependencies=[Depends(credential_contract)])

    @router.get('/task-readiness')
    async def readiness(request: Request, required_tool: Annotated[list[str] | None, Query(max_length=20)] = None,
                        planning: bool = True, source: str | None = Query(default=None, max_length=100)):
        required_tool = required_tool or []
        if any(not re.fullmatch(r'[a-zA-Z0-9_.-]{1,100}', t) for t in required_tool):
            raise HTTPException(422, 'Invalid required tool name')
        required = sorted(set(required_tool))
        if source is not None and (len(required) != 1 or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]{0,99}', source)):
            raise HTTPException(422, 'Select exactly one required tool with a valid source')
        try:
            async with asyncio.timeout(8):
                pool = request.app.state.repository.pool
                authorization = request.headers.get('authorization', '')
                if not authorization.startswith('Bearer apk_'):
                    raise HTTPException(401, 'Application service credential required')
                claims = await authenticate(pool, authorization,
                                            request.headers.get('X-External-User-ID'), 'tasks:read')
                return await assess(pool, claims, registry, required_tools=required, planning=planning, source=source)
        except HTTPException:
            raise
        except Exception:  # noqa: BLE001 -- no credentials or raw database errors returned
            raise HTTPException(503, 'Readiness unavailable; operator should check the database and migrations.') from None

    app.include_router(router)
