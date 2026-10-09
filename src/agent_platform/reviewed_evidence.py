"""Operator-reviewed observations, scoped to one app and owner; never a live fetch.

Only the operator filesystem can supply evidence. Agents provide a content ID,
not prices, paths, timestamps, or purported browser results. A real browser
capture must be reviewed before it is placed in this store.
"""
import hashlib
import json
import os
from pathlib import Path

from pydantic import Field

from .automation import scope
from .connector_health import CheckResult, Observation, ProbeResult
from .tasks import Action, Strict, TaskError


class ReviewedInput(Strict):
    evidence_id: str = Field(pattern=r'^[a-f0-9]{64}$')


def evidence_path(root, app_id, owner_id, evidence_id):
    from uuid import UUID
    ReviewedInput(evidence_id=evidence_id)
    return Path(root)/str(UUID(str(app_id)))/hashlib.sha256(str(owner_id).encode()).hexdigest()/(evidence_id+'.json')


def read_evidence(root, app_id, owner_id, evidence_id):
    path = evidence_path(root, app_id, owner_id, evidence_id)
    if not Path(root).is_absolute() or path.resolve() != path.absolute():
        raise TaskError('Evidence store must use absolute paths without symlinks')
    with path.open('rb') as stream:
        data = stream.read(12001)
    if len(data)>12000 or hashlib.sha256(data).hexdigest()!=evidence_id:
        raise TaskError('Evidence content digest mismatch')
    record=json.loads(data)
    if (record.get('app_id')!=str(app_id) or record.get('owner_id')!=str(owner_id)
            or record.get('acquisition')!='operator_reviewed_browser' or record.get('schema_version')!=1):
        raise TaskError('Evidence ownership or provenance mismatch')
    return Observation.model_validate(record['observation'], strict=True)


class ReviewedEvidence:
    version, risk, timeout = '1', 'READ', 10
    input_model, output_model = ReviewedInput, CheckResult
    description = ('Read a content-pinned operator-reviewed browser observation for this application and user. '
                   'This never opens a browser or refreshes prices. Report stale evidence, location uncertainty, '
                   'product conflicts and unknown fees. Never invent an evidence ID.')

    def __init__(self, pool, registry):
        self.pool, self.registry = pool, registry

    def prepare(self, arguments):
        value=ReviewedInput.model_validate(arguments, strict=True)
        return Action('evidence.reviewed', '1', value.model_dump(), 'Read reviewed browser evidence; no retailer request.', 'READ')

    def health_scope(self, action):
        return action.arguments['evidence_id']

    async def execute(self, action, *, task_id, authorized, **context):
        checker=self.registry.adapters['runtime.connector_check']
        check=checker.prepare({'tool':action.tool,'version':'1','arguments':action.arguments,
                               'require_price':True,'max_age_seconds':3600})
        return await checker.execute(check, task_id=task_id, authorized=authorized)

    async def health_check(self, action, *, task_id, authorized):
        if not await authorized():
            raise TaskError('Execution authority revoked')
        async with self.pool.connection() as conn, conn.transaction():
            await scope(conn, worker=True)
            task=await (await conn.execute('SELECT source_app_id,owner_id FROM chore_tasks WHERE id=%s', (task_id,))).fetchone()
        if not task or not task['source_app_id']:
            raise TaskError('Application identity required')
        try:
            observation=read_evidence(os.environ.get('AI_REVIEWED_EVIDENCE_ROOT',''),
                task['source_app_id'],task['owner_id'],action.arguments['evidence_id'])
        except (OSError, ValueError, KeyError):
            return ProbeResult(status='CONFIGURATION_REQUIRED',reason_code='reviewed_evidence_unavailable',
                               verification_mode='stored_observation').model_dump()
        if not await authorized():
            raise TaskError('Execution authority revoked')
        return ProbeResult(status='WORKING',reason_code='operator_reviewed_browser_observation',
                           observations=[observation],verification_mode='stored_observation').model_dump()

    async def reconcile(self, action, **context):
        return None
