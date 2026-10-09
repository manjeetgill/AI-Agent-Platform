"""Bounded, read-only connector probes executed through the ordinary task worker."""
import asyncio
import json
import re
from datetime import timedelta
from uuid import uuid4

from psycopg.types.json import Jsonb

from .app_catalogue import policy
from .automation import emit, scope
from .connector_health import CheckInput, CheckResult, ProbeResult, evaluate
from .connector_recovery import recovery_options
from .tasks import Action, StepInput, TaskError


def resolve_probe(registry, value):
    adapter = registry.adapters.get(value.tool)
    if (value.tool.startswith('runtime.') or adapter is None or adapter.version != value.version
            or adapter.risk != 'READ' or not callable(getattr(adapter, 'health_check', None))):
        raise TaskError('Connector needs a version-pinned, read-only health_check adapter')
    action = registry.prepare(StepInput(tool=value.tool, arguments=value.arguments))
    return adapter, action


class ConnectorCheck:
    version, risk, timeout = '1', 'READ', 30
    description = ('Check an installed connector using its read-only health_check contract. '
                   'Report blocking, missing authorization, stale evidence and unknown fees. '
                   'Do not invent connector arguments or bypass its access restrictions.')
    input_model, output_model = CheckInput, CheckResult

    def __init__(self, pool, registry):
        self.pool, self.registry = pool, registry

    def prepare(self, arguments):
        value = CheckInput.model_validate(arguments, strict=True)
        _, inner = resolve_probe(self.registry, value)
        value.arguments = inner.arguments
        return Action('runtime.connector_check', '1', value.model_dump(),
                      f'Check {value.tool} connectivity and evidence; no purchase or collection submission.', 'READ')

    async def execute(self, action, *, task_id, authorized, **context):
        value = CheckInput.model_validate(action.arguments)
        adapter, inner = resolve_probe(self.registry, value)
        # A trusted adapter defines the canonical source; callers cannot reset
        # blocking by changing arbitrary query arguments. Default is one source.
        source = adapter.health_scope(inner) if callable(getattr(adapter, 'health_scope', None)) else 'default'
        if not isinstance(source, str) or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]{0,99}', source):
            raise TaskError('Invalid adapter health scope')
        if not await authorized():
            raise TaskError('Execution authority revoked')
        async with self.pool.connection() as conn, conn.transaction():
            await scope(conn, worker=True)
            task = await (await conn.execute('SELECT * FROM chore_tasks WHERE id=%s', (task_id,))).fetchone()
            if not task or not task['source_app_id']:
                raise TaskError('Checks require an application identity')
            app_id, owner = task['source_app_id'], task['owner_id']
            if not await (await conn.execute('SELECT 1 FROM platform_tool_grants WHERE app_id=%s AND tool=%s',
                                            (app_id, value.tool))).fetchone():
                raise TaskError('Connector tool is not granted')
            existing = await (await conn.execute('SELECT result,action_hash FROM connector_check_reports WHERE task_id=%s AND step_position=%s',
                                                 (task_id, task['current_step']))).fetchone()
            if existing:
                if existing['action_hash'] != action.digest:
                    raise TaskError('Saved check differs from this action')
                return {**existing['result'], 'execution': 'CACHED'}
            # One app lock covers both hourly reservations and concurrent probe admission.
            await conn.execute('SELECT id FROM platform_apps WHERE id=%s FOR UPDATE', (app_id,))
            await conn.execute('''INSERT INTO connector_check_state(tenant_id,app_id,owner_id,tool,source)
                VALUES(%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING''', (task['tenant_id'], app_id, owner, value.tool, source))
            state = await (await conn.execute('''SELECT *,now() AS clock FROM connector_check_state
                WHERE app_id=%s AND owner_id=%s AND tool=%s AND source=%s FOR UPDATE''', (app_id, owner, value.tool, source))).fetchone()
            now = state['clock']
            reason = None
            if state['needs_attention']:
                reason = 'owner_action_required'
            elif state['lease_until'] and state['lease_until'] > now:
                reason = 'check_in_progress'
            elif state['next_check_at'] > now:
                reason = 'connector_cooldown'
            else:
                active = await (await conn.execute('SELECT count(*) AS n FROM connector_check_state WHERE app_id=%s AND lease_until>now()',
                                                   (app_id,))).fetchone()
                if active['n'] >= 3:
                    reason = 'app_concurrency_limit'
            limits = await policy(conn, app_id)
            if not reason:
                used = await (await conn.execute('''INSERT INTO connector_check_usage(app_id,hour,checks)
                    SELECT %s,date_trunc('hour',now()),1 WHERE %s>0
                    ON CONFLICT(app_id,hour) DO UPDATE SET checks=connector_check_usage.checks+1
                    WHERE connector_check_usage.checks<%s RETURNING checks''',
                    (app_id, limits.connector_checks_per_hour, limits.connector_checks_per_hour))).fetchone()
                if not used:
                    reason = 'app_hourly_limit'
            if reason:
                last = await (await conn.execute('''SELECT result FROM connector_check_reports
                    WHERE app_id=%s AND owner_id=%s AND tool=%s AND source=%s ORDER BY sequence DESC LIMIT 1''',
                    (app_id, owner, value.tool, source))).fetchone()
                last_status = last['result']['status'] if last and state['needs_attention'] else 'NOT_CHECKED'
                return CheckResult(connector=value.tool, source=source, execution='DEFERRED', status='NOT_CHECKED',
                    reason_code=reason, needs_attention=state['needs_attention'],
                    recovery_options=recovery_options(app_id, value.tool, source, last_status),
                    next_check_at=state['next_check_at'].isoformat()).model_dump(mode='json')
            lease = uuid4()
            await conn.execute('''UPDATE connector_check_state SET lease_token=%s,lease_until=now()+interval '120 seconds'
                WHERE app_id=%s AND owner_id=%s AND tool=%s AND source=%s''', (lease, app_id, owner, value.tool, source))

        async def probe_authorized():
            if not await authorized():
                return False
            async with self.pool.connection() as conn:
                return bool(await (await conn.execute('SELECT 1 FROM platform_tool_grants WHERE app_id=%s AND tool=%s',
                                                       (app_id, value.tool))).fetchone())

        try:
            async with asyncio.timeout(min(adapter.timeout, 20)):
                if not await probe_authorized():
                    raise TaskError('Connector authority revoked')
                raw = await adapter.health_check(inner, task_id=task_id, authorized=probe_authorized)
                # Contracts carry codes and public observations, not raw HTTP/page dumps.
                result = ProbeResult.model_validate(raw, strict=True)
                if len(json.dumps(result.model_dump(), allow_nan=False).encode()) > 12000:
                    raise ValueError('Evidence too large')
        except TimeoutError:
            result = ProbeResult(status='UNAVAILABLE', reason_code='probe_timeout')
        except Exception:  # noqa: BLE001 -- credentials/provider bodies must never enter logs or reports
            result = ProbeResult(status='BROKEN', reason_code='invalid_probe_or_connector_error')
        if not await probe_authorized():
            # A revoked task cannot publish evidence or cause more remote work.
            raise TaskError('Connector authority revoked')
        async with self.pool.connection() as conn, conn.transaction():
            await scope(conn, worker=True)
            state = await (await conn.execute('''SELECT *,now() AS clock FROM connector_check_state
                WHERE app_id=%s AND owner_id=%s AND tool=%s AND source=%s AND lease_token=%s FOR UPDATE''',
                (app_id, owner, value.tool, source, lease))).fetchone()
            if not state:
                raise TaskError('Connector lease changed')
            now = state['clock']
            failures = 0 if result.status == 'WORKING' else min(state['failures']+1, 20)
            needs_attention = result.status in {'BLOCKED', 'LOGIN_REQUIRED', 'CONFIGURATION_REQUIRED'}
            delay = max(result.retry_after_seconds or 60, min(3600, 60*2**min(failures, 6)))
            next_at = now + timedelta(seconds=delay)
            evidence, usable = evaluate(result, value, now=now)
            report = CheckResult(connector=value.tool, source=source, execution='EXECUTED', status=result.status,
                reason_code=result.reason_code, report_id=str(uuid4()), checked_at=now.isoformat(),
                next_check_at=next_at.isoformat(), needs_attention=needs_attention,
                verification_mode=result.verification_mode,
                probe=result.probe,
                recovery_options=recovery_options(app_id, value.tool, source, result.status, usable=usable),
                usable_evidence=usable, evidence=evidence,
                evidence_issues=sorted({issue for item in evidence for issue in item['issues']} |
                    ({'missing_observations'} if value.require_observations and not evidence else set()))).model_dump(mode='json')
            if len(json.dumps(report).encode())>16000:
                raise TaskError('Report exceeds storage budget')
            await conn.execute('''INSERT INTO connector_check_reports
                (id,tenant_id,app_id,owner_id,task_id,step_position,tool,source,tool_version,action_hash,result,checked_at)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)''',
                (report['report_id'], task['tenant_id'], app_id, owner, task_id, task['current_step'],
                 value.tool, source, value.version, action.digest, Jsonb(report), now))
            await conn.execute('''UPDATE connector_check_state SET failures=%s,needs_attention=%s,next_check_at=%s,
                lease_token=NULL,lease_until=NULL WHERE app_id=%s AND owner_id=%s AND tool=%s AND source=%s AND lease_token=%s''',
                (failures, needs_attention, next_at, app_id, owner, value.tool, source, lease))
            await emit(conn, {'tenant': task['tenant_id'], 'app_id': app_id, 'user': owner},
                'ConnectorCheckCompleted', {'report_id': report['report_id'], 'tool': value.tool, 'source': source,
                 'status': result.status, 'usable_evidence': usable, 'needs_attention': needs_attention},
                key='connector-check:'+report['report_id'], correlation=task_id)
            return report

    async def reconcile(self, action, **context):
        return None


def install(registry, pool):
    registry.adapters['runtime.connector_check'] = ConnectorCheck(pool, registry)
