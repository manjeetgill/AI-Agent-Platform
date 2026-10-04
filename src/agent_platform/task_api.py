"""Authenticated task lifecycle; approval endpoints only commit durable queue work."""
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, Request
from psycopg.errors import UniqueViolation

from .flags import enabled
from .task_store import TaskStore
from .tasks import ApprovalInput, PlanInput, TaskCreate, TaskError


def install_tasks(app, actor, registry):
    router = APIRouter(prefix='/v1/tasks')

    def context(request, authorization):
        claims = actor(authorization)
        if not enabled('AI_BROWSER_TASKS'):
            raise HTTPException(503, 'Browser tasks are disabled')
        pool = getattr(request.app.state.repository, 'pool', None)
        if pool is None:
            raise HTTPException(503, 'Durable task storage unavailable')
        return TaskStore(pool), claims['tenant'], claims['user']

    async def perform(operation):
        try:
            return await operation
        except TaskError as error:
            raise HTTPException(404 if str(error) == 'Task not found' else 409, str(error)) from None
        except UniqueViolation:
            raise HTTPException(409, 'Another task is active for this user') from None

    @router.post('', status_code=201)
    async def create(body: TaskCreate, request: Request, authorization: str | None = Header(default=None)):
        store, tenant, user = context(request, authorization)
        row = await perform(store.create(tenant, user, body))
        return await perform(store.get(tenant, user, row['id']))

    @router.get('')
    async def listing(request: Request, authorization: str | None = Header(default=None)):
        store, tenant, user = context(request, authorization)
        return {'tasks': await perform(store.list(tenant, user))}

    @router.get('/{task_id}')
    async def detail(task_id: UUID, request: Request, authorization: str | None = Header(default=None)):
        store, tenant, user = context(request, authorization)
        return await perform(store.get(tenant, user, task_id))

    @router.put('/{task_id}/plan')
    async def plan(task_id: UUID, body: PlanInput, request: Request,
                   authorization: str | None = Header(default=None)):
        store, tenant, user = context(request, authorization)
        try:
            actions = [registry.prepare(step) for step in body.steps]
        except (TaskError, ValueError):
            raise HTTPException(422, 'Plan contains unsupported tools or invalid action arguments') from None
        await perform(store.plan(tenant, user, task_id, actions))
        return await perform(store.get(tenant, user, task_id))

    @router.post('/{task_id}/approve')
    async def approve(task_id: UUID, body: ApprovalInput, request: Request,
                      authorization: str | None = Header(default=None)):
        store, tenant, user = context(request, authorization)
        await perform(store.approve(tenant, user, task_id, body.action_hash))
        return await perform(store.get(tenant, user, task_id))

    @router.post('/{task_id}/{command}')
    async def control(task_id: UUID, command: Literal['start', 'pause', 'resume', 'cancel', 'reconcile'],
                      request: Request, authorization: str | None = Header(default=None)):
        store, tenant, user = context(request, authorization)
        if command == 'start':
            await perform(store.start(tenant, user, task_id))
        else:
            await perform(store.control(tenant, user, task_id, command))
        return await perform(store.get(tenant, user, task_id))

    app.include_router(router)
