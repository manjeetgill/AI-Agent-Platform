"""Standalone agent API. Start with: uvicorn agent_platform.app:app --port 4060."""
import asyncio
import json
import logging
import os
import re
import time
from contextlib import asynccontextmanager
from uuid import NAMESPACE_URL, UUID, uuid5

from fastapi import FastAPI, Header, HTTPException, Request
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from .catalog import BUILTINS, TEMPLATES
from .decision_tool import default_registry
from .flags import capabilities, enabled
from .gateway import model_configuration
from .migrate import schema_ready
from .models import AgentDraft, AgentIdea, PreferenceIn, RunRequest, StrategyPlan, WorkflowRequest
from .repository import Repository
from .runtime import RunFailure, Runtime
from .task_agent_api import install_task_agents
from .task_api import install_tasks
from .tokens import TokenError, verify
from .vision import install_vision
from .workflow_report import assemble_workflow


def create_app(*, repository=None, runtime=None, signing_key=None, task_registry=None):
    key = signing_key or os.environ.get('AI_PLATFORM_SIGNING_KEY', '')
    database_url = os.environ.get('AI_PLATFORM_DATABASE_URL', '')
    memory_enabled = enabled('AI_MEMORY', default=True)
    logger = logging.getLogger('agent_platform')

    @asynccontextmanager
    async def lifespan(application):
        if repository is None:
            if not database_url or len(key) < 32:
                raise RuntimeError('Platform database and signing key are required')
            pool = AsyncConnectionPool(database_url, min_size=1, max_size=8, open=False,
                                       kwargs={'row_factory': dict_row, 'connect_timeout': 3,
                                               'options': '-c statement_timeout=5000'})
            await pool.open()
            await pool.wait(timeout=10)
            application.state.pool = pool
            application.state.repository = Repository(pool)
        else:
            application.state.repository = repository
        application.state.runtime = runtime or Runtime(
            os.environ.get('AI_PLATFORM_CONNECTOR_URL', ''), key, os.environ.get('NVIDIA_API_KEY', ''))
        from .research_tool import install_research_tool
        if getattr(application.state.repository, "pool", None) is not None:
            install_research_tool(registry, application.state.repository.pool, application.state.runtime)
            from .knowledge import KnowledgeAnswer
            registry.adapters['knowledge.answer'] = KnowledgeAnswer(application.state.repository.pool)
        application.state.workflow_tasks = set()
        try:
            yield
        finally:
            for task in application.state.workflow_tasks:
                task.cancel()
            await asyncio.gather(*application.state.workflow_tasks, return_exceptions=True)
            if repository is None:
                await application.state.pool.close()

    app = FastAPI(title='AI Agent Platform', docs_url=None, redoc_url=None, lifespan=lifespan)

    def actor(authorization):
        if not authorization or not authorization.startswith('Bearer '):
            raise HTTPException(401, 'Delegation required')
        try:
            return verify(key, authorization[7:], issuer='nraialgo', audience='agent-platform',
                          scope='agents:access')
        except TokenError:
            raise HTTPException(401, 'Invalid delegation') from None

    def uuid(value):
        try:
            return str(UUID(value))
        except (ValueError, TypeError):
            raise HTTPException(422, 'Invalid identifier') from None

    @app.get('/health')
    async def health():
        return {'status': 'ok', 'service': 'agent-platform'}

    @app.get('/readiness')
    async def readiness(request: Request):
        if repository is not None:
            return {'status': 'ready', 'service': 'agent-platform'}
        try:
            async with request.app.state.pool.connection() as connection:
                if not await schema_ready(connection):
                    raise RuntimeError('Platform migration mismatch')
        except Exception:  # noqa: BLE001 -- no database details in readiness response
            raise HTTPException(503, 'Agent platform database unavailable') from None
        return {'status': 'ready', 'service': 'agent-platform'}

    @app.get('/v1/agents/catalog')
    async def catalog(request: Request, authorization: str | None = Header(default=None)):
        claims = actor(authorization)
        repo = request.app.state.repository
        if enabled('AI_BUILTIN_AGENTS', default=True):
            await repo.ensure_builtins(claims['tenant'], claims['user'], BUILTINS)
        agents = await repo.list_agents(claims['tenant'], claims['user'])
        builtin_keys = {str(uuid5(NAMESPACE_URL, f"agent-platform:{claims['tenant']}:{claims['user']}:{slug}")): slug
                        for slug in BUILTINS}
        return {'agents': [{**agent, 'builtin_key': builtin_keys.get(str(agent['id']))
                            if agent['kind'] == 'builtin' else None} for agent in agents], 'templates': TEMPLATES,
                'features': capabilities(), 'model': model_configuration()}

    @app.post('/v1/agents')
    async def create_agent(draft: AgentDraft, request: Request, authorization: str | None = Header(default=None)):
        claims = actor(authorization)
        if not enabled('AI_CUSTOM_AGENTS', default=True):
            raise HTTPException(503, 'Custom agents disabled')
        if not draft.allowed_tools or len(set(draft.allowed_tools)) != len(draft.allowed_tools):
            raise HTTPException(422, 'Select distinct read-only evidence tools')
        repo = request.app.state.repository
        agent_id = await repo.create_agent(claims['tenant'], claims['user'], draft)
        return {'id': agent_id, 'version': 1}

    @app.post('/v1/agents/interpret')
    async def interpret(idea: AgentIdea, request: Request, authorization: str | None = Header(default=None)):
        claims = actor(authorization)
        if not enabled('AI_CUSTOM_AGENTS', default=True):
            raise HTTPException(503, 'Custom agents disabled')
        if not await request.app.state.repository.reserve_idea(claims['tenant'], claims['user']):
            raise HTTPException(429, 'Daily agent builder limit reached')
        try:
            suggestion, _, _, _ = await request.app.state.runtime.gateway.suggest(idea.request)
        except RunFailure:
            raise HTTPException(503, 'Agent suggestion unavailable') from None
        return suggestion

    @app.post('/v1/agents/strategy/interpret')
    async def strategy_interpret(idea: AgentIdea, request: Request,
                                 authorization: str | None = Header(default=None)):
        claims = actor(authorization)
        if re.search(r'\b(RSI|MACD|volume|VWAP|Bollinger|intraday|code|Python)\b', idea.request, re.IGNORECASE):
            return StrategyPlan(supported=False, interpretation='The current archive backtest engine cannot express this rule.',
                                draft=None, missing=['Choose a supported first-close or SMA5 entry rule.'])
        if not await request.app.state.repository.reserve_idea(claims['tenant'], claims['user']):
            raise HTTPException(429, 'Daily strategy interpretation limit reached')
        try:
            plan, _, _, _ = await request.app.state.runtime.gateway.interpret_strategy(idea.request)
        except RunFailure:
            raise HTTPException(503, 'Strategy interpretation unavailable') from None
        return plan

    @app.get('/v1/agents/memory')
    async def memory(request: Request, q: str = '', authorization: str | None = Header(default=None)):
        claims = actor(authorization)
        if not memory_enabled:
            raise HTTPException(503, 'Agent memory disabled')
        repo = request.app.state.repository
        return {'preferences': await repo.list_preference_rows(claims['tenant'], claims['user']),
                'results': await repo.search_memory(claims['tenant'], claims['user'], q[:200])
                if q.strip() and enabled('AI_RAG') else []}

    @app.post('/v1/agents/memory/preferences')
    async def add_preference(preference: PreferenceIn, request: Request,
                             authorization: str | None = Header(default=None)):
        claims = actor(authorization)
        if not memory_enabled:
            raise HTTPException(503, 'Agent memory disabled')
        memory_id = await request.app.state.repository.add_preference(claims['tenant'], claims['user'],
                                                                       preference.content)
        if memory_id is None:
            raise HTTPException(409, 'Preference limit reached')
        return {'id': memory_id}

    @app.delete('/v1/agents/memory/preferences/{memory_id}')
    async def remove_preference(memory_id: str, request: Request,
                                authorization: str | None = Header(default=None)):
        claims = actor(authorization)
        if not memory_enabled:
            raise HTTPException(503, 'Agent memory disabled')
        found = await request.app.state.repository.remove_preference(claims['tenant'], claims['user'], uuid(memory_id))
        if not found:
            raise HTTPException(404, 'Preference not found')
        return {'removed': True}

    @app.put('/v1/agents/{agent_id}')
    async def update_agent(agent_id: str, draft: AgentDraft, request: Request,
                           authorization: str | None = Header(default=None)):
        claims = actor(authorization)
        if not enabled('AI_CUSTOM_AGENTS', default=True):
            raise HTTPException(503, 'Custom agents disabled')
        if not draft.allowed_tools or len(set(draft.allowed_tools)) != len(draft.allowed_tools):
            raise HTTPException(422, 'Select distinct read-only evidence tools')
        version = await request.app.state.repository.update_agent(claims['tenant'], claims['user'],
                                                                   uuid(agent_id), draft)
        if version is None:
            raise HTTPException(404, 'Agent not found')
        return {'id': agent_id, 'version': version}

    @app.post('/v1/agents/{agent_id}/status')
    async def agent_status(agent_id: str, request: Request, status: str,
                           authorization: str | None = Header(default=None)):
        claims = actor(authorization)
        if not enabled('AI_CUSTOM_AGENTS', default=True):
            raise HTTPException(503, 'Custom agents disabled')
        if status not in {'active', 'disabled'}:
            raise HTTPException(422, 'Invalid status')
        found = await request.app.state.repository.set_status(claims['tenant'], claims['user'],
                                                               uuid(agent_id), status)
        if not found:
            raise HTTPException(404, 'Agent not found')
        return {'status': status}

    async def execute_run(body: RunRequest, request: Request, claims):
        started = time.monotonic()
        repo = request.app.state.repository
        if enabled('AI_BUILTIN_AGENTS', default=True):
            await repo.ensure_builtins(claims['tenant'], claims['user'], BUILTINS)
        definition = await repo.get_agent(claims['tenant'], claims['user'], uuid(body.agent_id))
        if not definition:
            raise HTTPException(404, 'Agent not found')
        if definition['status'] != 'active' or body.evidence.tool not in definition['allowed_tools']:
            raise HTTPException(403, 'Agent is disabled or lacks the selected evidence permission')
        await repo.reap_stale(claims['tenant'], claims['user'])
        previous = await repo.run_by_key(claims['tenant'], claims['user'], body.idempotency_key)
        if previous:
            if str(previous['agent_id']) != str(definition['id']) or previous['input'] != body.evidence.model_dump():
                raise HTTPException(409, 'Idempotency key was used for another request')
            return previous
        if await repo.recent_count(claims['tenant'], claims['user']) >= 50:
            raise HTTPException(429, 'Daily agent run limit reached')
        if await repo.active_count(claims['tenant'], claims['user']) >= 2:
            raise HTTPException(429, 'Too many active agent runs')
        run_id, created = await repo.create_run(claims['tenant'], claims['user'], definition['id'],
                                                definition['version'], body.evidence.model_dump(),
                                                body.idempotency_key)
        if run_id is None:
            raise HTTPException(409, 'Agent already has an active run')
        if not created:
            return await repo.get_run(claims['tenant'], claims['user'], uuid(run_id))
        await repo.transition(run_id, 'CREATED', 'RUNNING')
        try:
            async with asyncio.timeout(55):
                evidence = await request.app.state.runtime.retrieve(claims['tenant'], claims['user'],
                    definition['id'], run_id, body.evidence)
                preferences = await repo.list_preferences(claims['tenant'], claims['user']) if memory_enabled else []
                report, tokens, provider, model = await request.app.state.runtime.generate(
                    definition, evidence, preferences)
                await repo.transition(run_id, 'RUNNING', 'COMPLETED', report=report.model_dump(),
                                      evidence_refs=[body.evidence.model_dump()],
                                      provider=provider, model=model, tokens=tokens)
            if memory_enabled:
                try:
                    await repo.record_observations(claims['tenant'], claims['user'], run_id, report.model_dump())
                except Exception:  # noqa: BLE001 -- memory failure must not undo a completed report
                    logger.warning('Agent observation storage failed for run %s', run_id)
        except TimeoutError:
            await repo.transition(run_id, 'RUNNING', 'TIMEOUT', error_code='RUN_TIMEOUT')
        except RunFailure as exc:
            await repo.transition(run_id, 'RUNNING', 'TIMEOUT' if exc.code.endswith('TIMEOUT') else 'FAILED',
                                  error_code=exc.code)
        except Exception:  # noqa: BLE001 -- convert unexpected provider/connector faults to a safe run state
            await repo.transition(run_id, 'RUNNING', 'FAILED', error_code='INTERNAL_FAILURE')
        result = await repo.get_run(claims['tenant'], claims['user'], uuid(run_id))
        logger.info(json.dumps({'event': 'agent_run_finished', 'run_id': run_id,
            'correlation_id': run_id, 'request_id': claims['jti'],
            'tenant_id': claims['tenant'], 'user_id': claims['user'], 'agent_id': str(definition['id']),
            'agent_version': definition['version'], 'tool': body.evidence.tool, 'status': result['status'],
            'provider': result.get('provider'), 'model': result.get('model'),
            'duration_ms': round((time.monotonic() - started) * 1000)}))
        return result

    @app.get('/v1/agents/usage')
    async def usage(request: Request, authorization: str | None = Header(default=None)):
        claims = actor(authorization)
        return await request.app.state.repository.usage(claims['tenant'], claims['user'])

    @app.post('/v1/agents/runs')
    async def run(body: RunRequest, request: Request, authorization: str | None = Header(default=None)):
        return await execute_run(body, request, actor(authorization))

    async def workflow_work(workflow_id, body, request, claims):
        repo = request.app.state.repository
        steps = {'signal_review': ('signal-research', 'signal-challenger'),
                 'backtest_review': ('backtest-auditor', 'strategy-review'),
                 'trading_review': ('trade-autopsy', 'trader-behavior'),
                 'strategy_comparison': ('strategy-doctor', 'strategy-comparator')}[body.kind]
        try:
            async with asyncio.timeout(115):
                for index, slug in enumerate(steps):
                    agent_id = uuid5(NAMESPACE_URL,
                        f"agent-platform:{claims['tenant']}:{claims['user']}:{slug}")
                    result = await execute_run(RunRequest(agent_id=str(agent_id), evidence=body.evidence,
                        idempotency_key=body.idempotency_key + ':' + str(index)), request, claims)
                    await repo.workflow_step(workflow_id, result['id'])
                    if result['status'] != 'COMPLETED':
                        await repo.workflow_transition(workflow_id, 'RUNNING', 'FAILED')
                        return
            await repo.workflow_transition(workflow_id, 'RUNNING', 'COMPLETED')
        except TimeoutError:
            await repo.workflow_transition(workflow_id, 'RUNNING', 'TIMEOUT')
        except Exception:  # noqa: BLE001 -- workflow failure must not expose service details
            logger.warning('Agent workflow failed for %s', workflow_id)
            await repo.workflow_transition(workflow_id, 'RUNNING', 'FAILED')

    @app.post('/v1/agents/workflows')
    async def start_workflow(body: WorkflowRequest, request: Request,
                             authorization: str | None = Header(default=None)):
        claims = actor(authorization)
        if not enabled('AI_WORKFLOWS') or not enabled('AI_BUILTIN_AGENTS', default=True):
            raise HTTPException(503, 'Agent workflows disabled')
        required = {'signal_review': 'scanner_evidence', 'backtest_review': 'backtest_evidence',
                    'trading_review': 'research_evidence', 'strategy_comparison': 'research_evidence'}[body.kind]
        if body.evidence.tool != required:
            raise HTTPException(422, 'Workflow evidence does not match its steps')
        repo = request.app.state.repository
        await repo.reap_workflows(claims['tenant'], claims['user'])
        workflow_id, created = await repo.create_workflow(claims['tenant'], claims['user'],
                                                          body.kind, body.idempotency_key)
        if workflow_id is None:
            raise HTTPException(409, 'Another workflow is running')
        if not created:
            prior = await repo.get_workflow(claims['tenant'], claims['user'], uuid(workflow_id))
            if prior['kind'] != body.kind:
                raise HTTPException(409, 'Idempotency key was used for another workflow')
            return prior
        await repo.ensure_builtins(claims['tenant'], claims['user'], BUILTINS)
        await repo.workflow_transition(workflow_id, 'CREATED', 'RUNNING')
        task = asyncio.create_task(workflow_work(workflow_id, body, request, claims))
        request.app.state.workflow_tasks.add(task)
        task.add_done_callback(request.app.state.workflow_tasks.discard)
        return await repo.get_workflow(claims['tenant'], claims['user'], uuid(workflow_id))

    @app.get('/v1/agents/workflows')
    async def workflows(request: Request, authorization: str | None = Header(default=None)):
        claims = actor(authorization)
        if not enabled('AI_WORKFLOWS'):
            raise HTTPException(503, 'Agent workflows disabled')
        repo = request.app.state.repository
        await repo.reap_workflows(claims['tenant'], claims['user'])
        return {'workflows': await repo.list_workflows(claims['tenant'], claims['user'])}

    @app.get('/v1/agents/workflows/{workflow_id}')
    async def get_workflow(workflow_id: str, request: Request,
                           authorization: str | None = Header(default=None)):
        claims = actor(authorization)
        if not enabled('AI_WORKFLOWS'):
            raise HTTPException(503, 'Agent workflows disabled')
        repo = request.app.state.repository
        await repo.reap_workflows(claims['tenant'], claims['user'])
        row = await repo.get_workflow(claims['tenant'], claims['user'], uuid(workflow_id))
        if not row:
            raise HTTPException(404, 'Workflow not found')
        return await assemble_workflow(repo, claims, row)

    @app.get('/v1/agents/runs')
    async def runs(request: Request, authorization: str | None = Header(default=None)):
        claims = actor(authorization)
        return {'runs': await request.app.state.repository.list_runs(claims['tenant'], claims['user'])}

    @app.get('/v1/agents/runs/{run_id}')
    async def get_run(run_id: str, request: Request, authorization: str | None = Header(default=None)):
        claims = actor(authorization)
        row = await request.app.state.repository.get_run(claims['tenant'], claims['user'], uuid(run_id))
        if not row:
            raise HTTPException(404, 'Run not found')
        return row

    registry = task_registry if task_registry is not None else default_registry()
    from .knowledge import install_knowledge
    install_knowledge(app)
    install_vision(app, actor)
    install_tasks(app, actor, registry)
    install_task_agents(app, registry)
    return app


app = create_app()
