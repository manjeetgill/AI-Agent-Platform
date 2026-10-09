"""Owner-scoped connector inventory, evidence history, and manual circuit reset."""
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request

from .automation import emit, scope
from .flags import enabled
from .service_identity import authenticate
from .task_api import credential_contract
from .tasks import Strict


class Resume(Strict):
    report_id: UUID


def install_connectors(app, registry):
    router = APIRouter(prefix='/v1/connectors', dependencies=[Depends(credential_contract)])

    async def context(request, authorization, permission):
        if not enabled('AI_AUTOMATIONS'):
            raise HTTPException(503, 'Orchestration is disabled')
        pool = getattr(request.app.state.repository, 'pool', None)
        if pool is None:
            raise HTTPException(503, 'Durable storage unavailable')
        claims = await authenticate(pool, authorization, request.headers.get('X-External-User-ID'), permission)
        return pool, claims

    @router.get('')
    async def inventory(request: Request, authorization: str | None = Header(default=None)):
        pool, claims = await context(request, authorization, 'tasks:read')
        async with pool.connection() as conn, conn.transaction():
            await scope(conn, claims)
            grants = {r['tool'] for r in await (await conn.execute('SELECT tool FROM platform_tool_grants WHERE app_id=%s',
                                                                  (claims['app_id'],))).fetchall()}
            states = await (await conn.execute('''SELECT tool,source,failures,needs_attention,next_check_at,
                lease_until>now() AS check_in_progress FROM connector_check_state WHERE app_id=%s AND owner_id=%s''',
                (claims['app_id'], claims['user']))).fetchall()
            from .app_catalogue import policy
            limits = await policy(conn, claims['app_id'])
            return {'check_tool_granted': 'runtime.connector_check' in grants,
                    'connectors': [{'tool': name, 'version': adapter.version,
                                    'health_check': callable(getattr(adapter, 'health_check', None)) and adapter.risk == 'READ',
                                    **({'execution_location':adapter.executor,'requires_customer_browser':adapter.executor=='paired_browser'}
                                       if name in {'retailer.observe','browser.retailer_observe'} else {})}
                                   for name, adapter in registry.adapters.items() if name in grants and not name.startswith('runtime.')],
                    'states': states, 'limits': {'checks_per_hour': limits.connector_checks_per_hour,
                                                'simultaneous_checks': 3, 'minimum_interval_seconds': 60}}

    @router.get('/reports')
    async def reports(request: Request, authorization: str | None = Header(default=None),
                      after: int = Query(default=0, ge=0), limit: int = Query(default=50, ge=1, le=100),
                      tool: str | None = Query(default=None, max_length=100),
                      order: Literal['asc', 'desc'] = 'asc', before: int | None = Query(default=None, ge=1)):
        pool, claims = await context(request, authorization, 'tasks:read')
        async with pool.connection() as conn, conn.transaction():
            await scope(conn, claims)
            rows = await (await conn.execute('''SELECT id,sequence,task_id,step_position,tool,source,tool_version,result,checked_at
                FROM connector_check_reports WHERE app_id=%s AND owner_id=%s AND sequence>%s
                AND (%s::text IS NULL OR tool=%s) AND (%s::bigint IS NULL OR sequence<%s)
                ORDER BY sequence ''' + ('DESC' if order == 'desc' else 'ASC') + ' LIMIT %s',
                (claims['app_id'], claims['user'], after, tool, tool, before, before, limit + 1))).fetchall()
            page = rows[:limit]
            return {'reports': page, 'next_after': page[-1]['sequence'] if page and order == 'asc' else after,
                    'next_before': page[-1]['sequence'] if len(rows) > limit and order == 'desc' else None}

    @router.post('/{tool}/{source}/resume')
    async def resume(tool: str, source: str, body: Resume, request: Request, authorization: str | None = Header(default=None)):
        pool, claims = await context(request, authorization, 'tasks:write')
        async with pool.connection() as conn, conn.transaction():
            await scope(conn, claims)
            state = await (await conn.execute('''SELECT *,lease_until>now() AS busy FROM connector_check_state
                WHERE app_id=%s AND owner_id=%s AND tool=%s AND source=%s FOR UPDATE''',
                (claims['app_id'], claims['user'], tool, source))).fetchone()
            if not state:
                raise HTTPException(404, 'Connector state not found')
            report = await (await conn.execute('''SELECT id FROM connector_check_reports
                WHERE app_id=%s AND owner_id=%s AND tool=%s AND source=%s ORDER BY sequence DESC LIMIT 1''',
                (claims['app_id'], claims['user'], tool, source))).fetchone()
            if not report or report['id'] != body.report_id or state['busy']:
                raise HTTPException(409, 'Reload the latest report before resuming; checks may still be in progress')
            # Resuming never grants access, bypasses cooldown, or starts remote work.
            await conn.execute('''UPDATE connector_check_state SET needs_attention=false WHERE app_id=%s AND owner_id=%s AND tool=%s AND source=%s''',
                               (claims['app_id'], claims['user'], tool, source))
            await emit(conn, claims, 'ConnectorCheckResumed', {'tool': tool, 'source': source, 'report_id': str(body.report_id)},
                       key='connector-resume:'+str(body.report_id), correlation=str(body.report_id))
            return {'tool': tool, 'source': source, 'needs_attention': False, 'next_check_at': state['next_check_at']}

    app.include_router(router)
