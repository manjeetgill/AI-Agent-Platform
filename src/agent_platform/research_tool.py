"""Automatic evidence explanations on the durable task worker, with no blind model retry."""
import asyncio
import logging
import os
from uuid import UUID

from pydantic import Field, field_validator

from .flags import enabled
from .models import ResearchInput
from .repository import Repository
from .runtime import Runtime
from .tasks import Action, Reconciliation, Strict, TaskError


class ExplanationInput(Strict):
    focus: str = Field(default='', max_length=1000)
    snapshot_id: str
    agent_id: str
    agent_version: int = Field(ge=1)

    @field_validator('snapshot_id', 'agent_id')
    @classmethod
    def uuid_string(cls, value):
        return str(UUID(value))


class ExplanationResult(Strict):
    run_id: str
    summary: str
    tokens: int
    provider: str
    model: str


class ResearchExplanation:
    version = '1'
    risk = 'READ'
    timeout = 30
    description = ('Explain an existing NRAIAlgo research snapshot with an existing research agent. '
        'Requires exact snapshot_id, agent_id and agent_version. Never invent identifiers. '
        'Sends saved evidence and saved preferences to NVIDIA under the research integration permission. '
        'No trading, browser actions or payments. Multiple steps are independent evidence reviews.')
    input_model = ExplanationInput
    output_model = ExplanationResult

    def __init__(self, pool, runtime, app_id):
        self.pool, self.runtime, self.app_id = pool, runtime, str(UUID(app_id))
        self.repo = Repository(pool)

    def prepare(self, arguments):
        value = self.input_model.model_validate(arguments, strict=True)
        return Action('research.explain', self.version, value.model_dump(),
            f'Send saved evidence {value.snapshot_id} and your saved research preferences to NVIDIA '
            f'for agent {value.agent_id}, version {value.agent_version}. '
            'One model explanation. No orders or payments.', self.risk)

    async def owner(self, task_id):
        async with self.pool.connection() as conn:
            row = await (await conn.execute('''SELECT u.external_id FROM chore_tasks t
                JOIN platform_external_users u ON u.app_id=t.source_app_id AND u.owner_id=t.owner_id
                WHERE t.id=%s AND t.tenant_id='nraialgo' AND t.source_app_id=%s''',
                (task_id, self.app_id))).fetchone()
        if not row:
            raise TaskError('Research requires the configured NRAIAlgo application')
        return str(UUID(row['external_id']))

    @staticmethod
    def key(task_id, action):
        return 'research:' + task_id + ':' + action.digest

    @staticmethod
    def result(row):
        return {'run_id': str(row['id']), 'summary': row['report']['summary'],
                'tokens': row['tokens'], 'provider': row['provider'], 'model': row['model']}

    async def execute(self, action, *, task_id, authorized, **context):
        user = await self.owner(task_id)
        value = self.input_model.model_validate(action.arguments)
        key = self.key(task_id, action)
        prior = await self.repo.run_by_key('nraialgo', user, key)
        if prior and prior['status'] == 'COMPLETED':
            return self.result(prior)
        if prior and prior['status'] != 'CREATED':
            raise TaskError('Prior model outcome must be reconciled')
        definition = await self.repo.get_agent('nraialgo', user, value.agent_id)
        if (not definition or definition['status'] != 'active' or definition['version'] != value.agent_version
                or 'research_evidence' not in definition['allowed_tools']):
            raise TaskError('Research agent changed or is unavailable; review a new plan')
        if not prior and await self.repo.recent_count('nraialgo', user) >= 50:
            raise TaskError('Daily research run limit reached')
        query = ResearchInput(tool='research_evidence', snapshot_id=value.snapshot_id)
        run_id, _ = await self.repo.create_run('nraialgo', user, value.agent_id, value.agent_version,
                                             query.model_dump(), key)
        if not run_id:
            raise TaskError('Research agent already has an active run')
        # Retrieval rechecks source ownership and current app access. No evidence is accepted from a browser.
        evidence = await self.runtime.retrieve('nraialgo', user, value.agent_id, run_id, query)
        if value.focus:
            evidence = {**evidence, 'userResearchQuestion': value.focus}
        preferences = await self.repo.list_preferences('nraialgo', user) if enabled('AI_MEMORY', default=True) else []
        if not await authorized():
            raise TaskError('Execution authority revoked')
        # Commit dispatch BEFORE calling NVIDIA. A crash after this boundary is uncertain, never re-sent.
        if not await self.repo.transition(run_id, 'CREATED', 'RUNNING'):
            raise TaskError('Model dispatch already attempted')
        try:
            report, tokens, provider, model = await self.runtime.generate(definition, evidence, preferences)
            if not await self.repo.transition(run_id, 'RUNNING', 'COMPLETED', report=report.model_dump(),
                    evidence_refs=[query.model_dump()], tokens=tokens, provider=provider, model=model):
                raise TaskError('Could not persist model outcome')
        except (Exception, asyncio.CancelledError):
            await asyncio.shield(self.repo.transition(run_id, 'RUNNING', 'TIMEOUT',
                                                      error_code='DURABLE_MODEL_OUTCOME_UNKNOWN'))
            raise
        if enabled('AI_MEMORY', default=True):
            try:
                await self.repo.record_observations('nraialgo', user, run_id, report.model_dump())
            except Exception:  # noqa: BLE001 -- a memory failure must not repeat the model call
                logging.getLogger(__name__).warning("Research memory update failed run_id=%s", run_id)
        return self.result(await self.repo.get_run('nraialgo', user, run_id))

    async def reconcile(self, action, *, task_id):
        user = await self.owner(task_id)
        row = await self.repo.run_by_key('nraialgo', user, self.key(task_id, action))
        if row and row['status'] == 'COMPLETED':
            return Reconciliation(True, self.result(row))
        if not row or row['status'] == 'CREATED':
            return Reconciliation(False, {'reason': 'No committed model dispatch exists for this exact action.'})
        return None


def install_research_tool(registry, pool, runtime=None):
    app_id = os.environ.get('AI_PLATFORM_RESEARCH_APP_ID')
    if app_id:
        runtime = runtime or Runtime(os.environ.get('AI_PLATFORM_CONNECTOR_URL', ''),
            os.environ.get('AI_PLATFORM_SIGNING_KEY', ''), os.environ.get('NVIDIA_API_KEY', ''))
        if isinstance(runtime, Runtime):
            # Isolate retry policy from the older synchronous research endpoints.
            runtime = Runtime(runtime.connector_url, runtime.signing_key, runtime.provider_key,
                              connector_client=runtime.connector_client, model_client=runtime.gateway.client)
            runtime.gateway.max_attempts = 1
        registry.adapters['research.explain'] = ResearchExplanation(pool, runtime, app_id)
