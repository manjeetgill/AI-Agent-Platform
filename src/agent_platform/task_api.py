"""Authenticated task lifecycle; approval endpoints only commit durable queue work."""
import asyncio
import json
import time
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Security
from fastapi.encoders import jsonable_encoder
from fastapi.responses import StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from psycopg.errors import UniqueViolation

from .flags import enabled
from .service_identity import authenticate
from .task_store import TaskStore
from .tasks import ApprovalInput, PlanInput, TaskCreate, TaskError

BEARER = Security(HTTPBearer(auto_error=False))


def credential_contract(
    external_user: str | None = Header(default=None, alias='X-External-User-ID',
        description='Required for service keys; derive from the calling app authenticated user.'),
    credential: HTTPAuthorizationCredentials | None = BEARER,
):
    """Expose headers/security in OpenAPI. context() verifies credentials and endpoint scope."""


def install_tasks(app, actor, registry):
    router = APIRouter(prefix='/v1/tasks', dependencies=[Depends(credential_contract)])

    async def context(request, authorization, scope=None):
        if not (enabled('AI_DURABLE_TASKS') or enabled('AI_BROWSER_TASKS')):
            raise HTTPException(503, 'Durable tasks are disabled')
        pool = getattr(request.app.state.repository, 'pool', None)
        if pool is None:
            raise HTTPException(503, 'Durable task storage unavailable')
        if authorization and authorization.startswith('Bearer apk_'):
            claims = await authenticate(pool, authorization, request.headers.get('X-External-User-ID'),
                                        scope or ('tasks:read' if request.method == 'GET' else 'tasks:write'))
        else:
            claims = actor(authorization)
        principal = claims.get('principal', 'user:' + str(claims['user']))
        return TaskStore(pool, principal=principal, app_id=claims.get('app_id')), claims['tenant'], claims['user']

    async def perform(operation):
        try:
            return await operation
        except TaskError as error:
            raise HTTPException(404 if str(error) == 'Task not found' else 409, str(error)) from None
        except UniqueViolation:
            raise HTTPException(409, 'Another task is active for this user') from None

    @router.post('', status_code=201)
    async def create(body: TaskCreate, request: Request, authorization: str | None = Header(default=None)):
        store, tenant, user = await context(request, authorization)
        row = await perform(store.create(tenant, user, body))
        return await perform(store.get(tenant, user, row['id']))

    @router.get('')
    async def listing(request: Request, authorization: str | None = Header(default=None),
                      limit: int = Query(default=50, ge=1, le=100), before: UUID | None = None):
        store, tenant, user = await context(request, authorization)
        return await perform(store.page(tenant, user, limit=limit, before=before))

    @router.get('/{task_id}')
    async def detail(task_id: UUID, request: Request, authorization: str | None = Header(default=None)):
        store, tenant, user = await context(request, authorization)
        return await perform(store.get(tenant, user, task_id))

    @router.get('/{task_id}/events')
    async def events(task_id: UUID, request: Request, authorization: str | None = Header(default=None),
                     after: int = Query(default=0, ge=0), limit: int = Query(default=100, ge=1, le=100)):
        store, tenant, user = await context(request, authorization)
        rows = await perform(store.events(tenant, user, task_id, after, limit))
        return {'events': rows, 'next_after': rows[-1]['id'] if rows else after}

    @router.get('/{task_id}/stream')
    async def stream(task_id: UUID, request: Request, authorization: str | None = Header(default=None),
                     after: int = Query(default=0, ge=0), last_event_id: int | None = Header(default=None, ge=0)):
        store, tenant, user = await context(request, authorization)
        cursor = max(after, last_event_id or 0)
        await perform(store.events(tenant, user, task_id, cursor, 1))

        async def generate():
            nonlocal cursor
            deadline = time.monotonic() + 25
            while time.monotonic() < deadline and not await request.is_disconnected():
                try:
                    # Recheck revocation/expiry while connected. Client reconnects with its last event ID.
                    await context(request, authorization)
                except HTTPException:
                    yield 'event: auth_required\ndata: {}\n\n'
                    return
                rows = await store.events(tenant, user, task_id, cursor, 100)
                for row in rows:
                    cursor = row['id']
                    yield f"id: {cursor}\nevent: task_event\ndata: {json.dumps(jsonable_encoder(row))}\n\n"
                if len(rows) < 100:
                    yield ': keep-alive\n\n'
                    await asyncio.sleep(1)
        return StreamingResponse(generate(), media_type='text/event-stream',
                                 headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})

    @router.put('/{task_id}/plan')
    async def plan(task_id: UUID, body: PlanInput, request: Request,
                   authorization: str | None = Header(default=None)):
        store, tenant, user = await context(request, authorization)
        try:
            actions = [registry.prepare(step) for step in body.steps]
        except (TaskError, ValueError):
            raise HTTPException(422, 'Plan contains unsupported tools or invalid action arguments') from None
        await perform(store.plan(tenant, user, task_id, actions))
        return await perform(store.get(tenant, user, task_id))

    @router.post('/{task_id}/approve')
    async def approve(task_id: UUID, body: ApprovalInput, request: Request,
                      authorization: str | None = Header(default=None)):
        store, tenant, user = await context(request, authorization, 'tasks:approve')
        await perform(store.approve(tenant, user, task_id, body.action_hash))
        return await perform(store.get(tenant, user, task_id))

    @router.post('/{task_id}/{command}')
    async def control(task_id: UUID, command: Literal['start', 'pause', 'resume', 'cancel', 'reconcile'],
                      request: Request, authorization: str | None = Header(default=None)):
        store, tenant, user = await context(request, authorization)
        if command == 'start':
            await perform(store.start(tenant, user, task_id))
        else:
            await perform(store.control(tenant, user, task_id, command))
        return await perform(store.get(tenant, user, task_id))

    app.include_router(router)
    return router
