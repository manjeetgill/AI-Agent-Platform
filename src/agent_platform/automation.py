"""Bounded generic scheduling and conditions; no domain-specific business rules."""
import json
import math
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID, uuid4

from fastapi import HTTPException
from psycopg.types.json import Jsonb
from pydantic import Field, model_validator

from .app_catalogue import policy
from .task_agents import Blueprint, digest, validate
from .task_store import TaskStore
from .tasks import Strict, TaskError

Lifecycle = Literal['DRAFT', 'ACTIVE', 'PAUSED', 'DISABLED', 'ARCHIVED']


class Manifest(Strict):
    schema_version: Literal[1] = 1
    version: int = Field(default=1, ge=1)
    blueprint: Blueprint
    runtime: Literal['durable_plan'] = 'durable_plan'
    # Tools are pinned in blueprint.steps; grants always come from the operator.
    memory_enabled: bool = True


class RegisterAgent(Strict):
    manifest: Manifest
    idempotency_key: str = Field(min_length=8, max_length=100)
    status: Literal['DRAFT', 'ACTIVE'] = 'DRAFT'


class Trigger(Strict):
    type: Literal['once', 'interval', 'event'] = 'interval'
    interval_seconds: int | None = Field(default=None, ge=60, le=604800)
    start_at: datetime | None = None
    event_type: str | None = Field(default=None, min_length=1, max_length=100)
    event_version: int = Field(default=1, ge=1, le=100)

    @model_validator(mode='after')
    def coherent(self):
        if (self.type == 'interval') != (self.interval_seconds is not None):
            raise ValueError('An interval trigger requires interval_seconds only')
        if (self.type == 'event') != (self.event_type is not None):
            raise ValueError('An event trigger requires event_type only')
        if self.start_at is not None and self.start_at.tzinfo is None:
            raise ValueError('start_at requires a timezone')
        return self


class Condition(Strict):
    # Dotted dictionary path; * expands bounded arrays. No expressions/code/regex.
    path: str = Field(pattern=r'^[A-Za-z0-9_*]+(?:\.[A-Za-z0-9_*]+){0,10}$', max_length=150)
    operator: Literal['lt', 'lte', 'gt', 'gte', 'eq', 'exists']
    value: float | str | bool | None = None
    freshness_path: str | None = Field(default=None, pattern=r'^[A-Za-z0-9_.]+$', max_length=100)
    max_age_seconds: int = Field(default=86400, ge=60, le=31536000)

    @model_validator(mode='after')
    def typed(self):
        if self.operator in {'lt', 'lte', 'gt', 'gte'} and (type(self.value) not in {float, int} or not math.isfinite(self.value)):
            raise ValueError('Numeric conditions require a finite number')
        if isinstance(self.value, str) and len(self.value) > 500:
            raise ValueError('Condition value too long')
        return self


class Notification(Strict):
    channel: Literal['in_app'] = 'in_app'
    title: str = Field(min_length=1, max_length=160)
    body: str = Field(default='Your monitoring condition was met. Review the attached evidence.', max_length=2000)
    on: Literal['crossing', 'every_match'] = 'crossing'


class AutomationDefinition(Strict):
    agent_id: UUID
    name: str = Field(min_length=1, max_length=120)
    trigger: Trigger
    condition: Condition | None = None
    notification: Notification | None = None
    max_runs: int = Field(default=100, ge=1, le=10000)


class CreateAutomation(Strict):
    definition: AutomationDefinition
    idempotency_key: str = Field(min_length=8, max_length=100)


class EventInput(Strict):
    event_type: str = Field(pattern=r'^[A-Za-z][A-Za-z0-9_.-]{0,99}$')
    version: int = Field(default=1, ge=1, le=100)
    correlation_id: str = Field(min_length=1, max_length=100)
    idempotency_key: str = Field(min_length=8, max_length=100)
    payload: dict


class PreferenceInput(Strict):
    value: dict
    expected_version: int = Field(default=0, ge=0)


async def scope(conn, claims=None, *, worker=False):
    for key, value in {'agent.worker': 'on' if worker else 'off',
                       'agent.tenant': (claims or {}).get('tenant', ''),
                       'agent.app': (claims or {}).get('app_id', ''),
                       'agent.owner': (claims or {}).get('user', '')}.items():
        await conn.execute('SELECT set_config(%s,%s,true)', (key, str(value)))


def bounded(value, maximum=8000):
    try:
        encoded = json.dumps(value, allow_nan=False, separators=(',', ':')).encode()
    except (ValueError, TypeError):
        raise HTTPException(422, 'Invalid JSON value') from None
    if len(encoded) > maximum:
        raise HTTPException(422, 'Payload exceeds the permitted size')
    return value


def condition_matches(condition, result, now=None):
    """Missing, malformed and stale evidence can never satisfy a numeric trigger."""
    if condition is None:
        return True
    now = now or datetime.now(UTC)
    nodes = [(result, result)]
    for part in condition.path.split('.'):
        next_nodes = []
        for node, parent in nodes:
            if part == '*' and isinstance(node, list):
                next_nodes.extend((item, item) for item in node[:200])
            elif isinstance(node, dict) and part in node:
                next_nodes.append((node[part], node))
        nodes = next_nodes[:200]
    for value, parent in nodes:
        if condition.freshness_path:
            stamp = parent
            for part in condition.freshness_path.split('.'):
                stamp = stamp.get(part) if isinstance(stamp, dict) else None
            try:
                age = (now - datetime.fromisoformat(stamp)).total_seconds()
                if age < -60 or age > condition.max_age_seconds:
                    continue
            except (ValueError, TypeError, AttributeError):
                continue
        if condition.operator == 'exists' and value is not None:
            return True
        if condition.operator == 'eq':
            numeric = type(value) in {int, float} and type(condition.value) in {int, float}
            if (numeric or type(value) is type(condition.value)) and value == condition.value:
                return True
        if condition.operator in {'lt', 'lte', 'gt', 'gte'} and type(value) in {int, float} and math.isfinite(value):
            expected = condition.value
            if {'lt': value < expected, 'lte': value <= expected,
                'gt': value > expected, 'gte': value >= expected}[condition.operator]:
                return True
    return False


async def owned_agent(conn, claims, agent_id, *, lock=False):
    row = await (await conn.execute('''SELECT * FROM task_agents WHERE id=%s AND tenant_id=%s
        AND app_id=%s AND owner_id=%s''' + (' FOR UPDATE' if lock else ''),
        (agent_id, claims['tenant'], claims['app_id'], claims['user']))).fetchone()
    if row is None:
        raise HTTPException(404, 'Agent not found')
    return row


async def enqueue(conn, registry, claims, definition, occurrence):
    """Called inside the caller's transaction. Task and queue become visible together."""
    agent = await owned_agent(conn, claims, definition.agent_id, lock=True)
    if agent['status'] != 'ACTIVE':
        raise TaskError('Agent is not active')
    blueprint = Blueprint.model_validate(agent['blueprint'])
    grants = {r['tool'] for r in await (await conn.execute(
        'SELECT tool FROM platform_tool_grants WHERE app_id=%s', (claims['app_id'],))).fetchall()}
    if len(blueprint.steps) > (await policy(conn, claims['app_id'])).max_plan_steps:
        raise TaskError('Plan exceeds current app limit')
    actions = validate(blueprint, registry, grants)
    # Serialise with other invocations using the existing per-owner unique constraint.
    task_id = uuid4()
    await conn.execute('''INSERT INTO chore_tasks(id,tenant_id,owner_id,request,request_key,
        source_agent_id,source_app_id,invocation_hash,revision) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,1)''',
        (task_id, claims['tenant'], claims['user'], definition.name, occurrence,
         definition.agent_id, claims['app_id'], digest({'agent': str(definition.agent_id), 'occurrence': occurrence})))
    for position, action in enumerate(actions):
        await conn.execute('''INSERT INTO chore_steps(task_id,position,tool,tool_version,arguments,
            action_hash,summary,risk) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)''',
            (task_id, position, action.tool, action.version, Jsonb(action.arguments), action.digest, action.summary, action.risk))
    store = TaskStore(None, principal=claims['principal'])
    await store.event(conn, task_id, 'AUTOMATION_INVOKED', {'agent_id': str(definition.agent_id)})
    await store.next(conn, task_id, 0)
    return task_id


async def emit(conn, claims, event_type, payload, *, key, source='platform', correlation='', version=1):
    bounded(payload, 16000)
    row = await (await conn.execute('''INSERT INTO automation_events
        (id,tenant_id,app_id,owner_id,event_type,version,source,correlation_id,request_key,payload)
        VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT(app_id,owner_id,source,request_key) DO NOTHING RETURNING *''',
        (uuid4(), claims['tenant'], claims['app_id'], claims['user'], event_type, version,
         source, correlation, key, Jsonb(payload)))).fetchone()
    if row is None:
        row = await (await conn.execute('''SELECT * FROM automation_events WHERE app_id=%s
            AND owner_id=%s AND source=%s AND request_key=%s''',
            (claims['app_id'], claims['user'], source, key))).fetchone()
        if (row['event_type'], row['version'], row['payload'], row['correlation_id']) != (event_type, version, payload, correlation):
            raise HTTPException(409, 'Event idempotency key belongs to different content')
    return row


def claims_for(row):
    return {'tenant': row['tenant_id'], 'app_id': str(row['app_id']),
            'user': str(row['owner_id']), 'principal': row.get('principal', 'worker')}


async def dispatch(pool, registry):
    """One occurrence per transaction. Competing schedulers lock/skip the same row."""
    from psycopg.errors import UniqueViolation
    async with pool.connection() as conn, conn.transaction():
        await scope(conn, worker=True)
        row = await (await conn.execute('''SELECT * FROM automations WHERE status='ACTIVE'
            AND next_run_at<=now() ORDER BY next_run_at,id FOR UPDATE SKIP LOCKED LIMIT 1''')).fetchone()
        if row is None:
            return False
        claims = claims_for(row)
        definition = AutomationDefinition.model_validate(row['definition'])
        await conn.execute("UPDATE automations SET next_run_at=now()+interval '30 seconds' WHERE id=%s", (row['id'],))
        if row['run_count'] >= definition.max_runs:
            await conn.execute("UPDATE automations SET status='DISABLED' WHERE id=%s", (row['id'],))
            return True
        # A revoked/expired scheduling principal cannot cause future work.
        principal = row['principal'].removeprefix('service:')
        key = await (await conn.execute('''SELECT 1 FROM platform_service_keys WHERE id=%s AND app_id=%s
            AND revoked_at IS NULL AND expires_at>now() AND 'agents:run'=ANY(scopes)''',
            (principal, row['app_id']))).fetchone()
        if not key:
            await conn.execute("UPDATE automations SET status='PAUSED' WHERE id=%s", (row['id'],))
            await emit(conn, claims, 'AutomationPaused', {'reason': 'principal_unavailable'},
                       key=f'principal:{row["id"]}:{row["run_count"]}', correlation=str(row['id']))
            return True
        event = None
        if definition.trigger.type == 'event':
            event = await (await conn.execute('''SELECT * FROM automation_events WHERE app_id=%s AND owner_id=%s
                AND sequence>%s AND event_type=%s AND version=%s ORDER BY sequence LIMIT 1''',
                (row['app_id'], row['owner_id'], row['last_event_sequence'],
                 definition.trigger.event_type, definition.trigger.event_version))).fetchone()
            if not event:
                return True
        occurrence = f'event:{event["id"]}' if event else f'run:{row["run_count"]}'
        limits = await policy(conn, row['app_id'])
        try:
            # Savepoint rolls quota back when another task occupies the owner slot.
            async with conn.transaction():
                used = await (await conn.execute('''INSERT INTO automation_usage(app_id,runs)
                    SELECT %s,1 WHERE %s>0 ON CONFLICT(app_id,day) DO UPDATE SET runs=automation_usage.runs+1
                    WHERE automation_usage.runs<%s RETURNING runs''',
                    (row['app_id'], limits.automation_runs_per_day, limits.automation_runs_per_day))).fetchone()
                if not used:
                    await conn.execute("UPDATE automations SET next_run_at=date_trunc('day',now())+interval '1 day' WHERE id=%s", (row['id'],))
                    return True
                task_id = await enqueue(conn, registry, claims, definition, f'auto:{row["id"]}:{occurrence}')
                await conn.execute('''INSERT INTO automation_runs(id,automation_id,tenant_id,app_id,owner_id,task_id,occurrence)
                    VALUES(%s,%s,%s,%s,%s,%s,%s)''',
                    (uuid4(), row['id'], row['tenant_id'], row['app_id'], row['owner_id'], task_id, occurrence))
        except UniqueViolation:
            return True  # Backpressure. No lost occurrence, no quota charge.
        except (TaskError, ValueError, HTTPException):
            await conn.execute("UPDATE automations SET status='PAUSED' WHERE id=%s", (row['id'],))
            await emit(conn, claims, 'AutomationPaused', {'reason': 'agent_or_grant_unavailable'},
                       key=f'grant:{row["id"]}:{row["run_count"]}', correlation=str(row['id']))
            return True
        next_at = datetime.now(UTC) + timedelta(seconds=definition.trigger.interval_seconds or 30)
        await conn.execute('''UPDATE automations SET run_count=run_count+1,next_run_at=%s,
            last_event_sequence=%s,status=%s,updated_at=now() WHERE id=%s''',
            (next_at, event['sequence'] if event else row['last_event_sequence'],
             'DISABLED' if definition.trigger.type == 'once' else 'ACTIVE', row['id']))
        return True


async def evaluate_completed(pool):
    """Condition, event, inbox and checkpoint commit together; replay creates no duplicate."""
    async with pool.connection() as conn, conn.transaction():
        await scope(conn, worker=True)
        row = await (await conn.execute('''SELECT r.*,t.status AS task_status FROM automation_runs r
            JOIN chore_tasks t ON t.id=r.task_id WHERE r.evaluated_at IS NULL
            AND t.status IN ('DONE','FAILED','CANCELLED') ORDER BY r.created_at
            FOR UPDATE OF r SKIP LOCKED LIMIT 1''')).fetchone()
        if not row:
            return False
        automation = await (await conn.execute('SELECT * FROM automations WHERE id=%s FOR UPDATE',
                                               (row['automation_id'],))).fetchone()
        definition = AutomationDefinition.model_validate(automation['definition'])
        claims = claims_for(row)
        steps = await (await conn.execute('SELECT result FROM chore_steps WHERE task_id=%s ORDER BY position',
                                         (row['task_id'],))).fetchall()
        result = {'steps': [s['result'] for s in steps], 'result': steps[-1]['result'] if steps else None}
        matched = row['task_status'] == 'DONE' and condition_matches(definition.condition, result)
        event = await emit(conn, claims, 'AgentRunCompleted' if row['task_status'] == 'DONE' else 'AgentRunFailed',
                           {'task_id': str(row['task_id']), 'automation_id': str(row['automation_id']),
                            'matched': matched, 'status': row['task_status']},
                           key=f'completed:{row["id"]}', correlation=str(row['task_id']))
        notification = definition.notification
        # Revoked automation suppresses delivery even if an already-running task finishes.
        approved = automation['approved_at'] and automation['status'] in {'ACTIVE', 'DISABLED'}
        if matched and notification and approved and (notification.on == 'every_match' or not automation['last_matched']):
            await conn.execute('''INSERT INTO automation_notifications
                (id,tenant_id,app_id,owner_id,event_id,title,body,evidence) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT(event_id,channel) DO NOTHING''',
                (uuid4(), row['tenant_id'], row['app_id'], row['owner_id'], event['id'], notification.title,
                 notification.body, Jsonb({'task_id': str(row['task_id']), 'result': result['result']})))
        # Failures don't reset crossing state and cannot produce a false re-alert.
        if row['task_status'] == 'DONE':
            await conn.execute('UPDATE automations SET last_matched=%s WHERE id=%s', (matched, automation['id']))
        await conn.execute('UPDATE automation_runs SET evaluated_at=now() WHERE id=%s', (row['id'],))
        return True


async def tick(pool, registry):
    completed = await evaluate_completed(pool)
    dispatched = await dispatch(pool, registry)
    from .event_webhooks import deliver, fanout
    await fanout(pool)
    delivered = await deliver(pool)
    return completed or dispatched or delivered
