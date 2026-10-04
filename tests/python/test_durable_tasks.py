"""Crash/concurrency tests use a disposable PostgreSQL database and fake site adapters only."""
import asyncio
import os
from contextlib import asynccontextmanager
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from psycopg.errors import UniqueViolation
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from agent_platform.app import create_app
from agent_platform.task_store import TaskStore
from agent_platform.task_worker import tick
from agent_platform.tasks import Action, Reconciliation, Registry, StepInput, TaskCreate, TaskError
from agent_platform.tokens import issue


class FakeSite:
    version = 'fixture-v1'
    timeout = 2

    def __init__(self, risk='READ'):
        self.risk = risk
        self.calls = 0
        self.verified = None

    def prepare(self, arguments):
        if set(arguments) != {'item'} or not isinstance(arguments['item'], str):
            raise TaskError('Invalid fixture input')
        return Action(self.risk.lower(), self.version, arguments, 'Review ' + arguments['item'], self.risk)

    async def execute(self, action, **context):
        assert self.risk != 'USER_ONLY'
        assert await context['authorized']()
        self.calls += 1
        return {'receipt_reference': 'fixture-only'}

    async def reconcile(self, action, **context):
        return self.verified


def database():
    value = os.environ.get('AGENT_TEST_DATABASE_URL')
    if not value:
        pytest.skip('Requires a disposable migrated AGENT_TEST_DATABASE_URL')
    return value


@asynccontextmanager
async def fixture_store():
    async with AsyncConnectionPool(database(), min_size=1, max_size=5, open=False,
                                   kwargs={'row_factory': dict_row}) as pool:
        await pool.wait(timeout=10)
        tenant, user = 'durability-' + uuid4().hex, uuid4()
        store = TaskStore(pool)
        try:
            yield store, tenant, user
        finally:
            async with pool.connection() as conn, conn.transaction():
                for table in ['chore_jobs', 'chore_events', 'chore_approvals', 'chore_steps']:
                    await conn.execute(f'DELETE FROM {table} WHERE task_id IN '
                        '(SELECT id FROM chore_tasks WHERE tenant_id=%s)', (tenant,))
                await conn.execute('DELETE FROM chore_tasks WHERE tenant_id=%s', (tenant,))


async def planned(store, tenant, user, *adapters):
    task = await store.create(tenant, user, TaskCreate(request='Complete fixture chore',
                                                    idempotency_key=uuid4().hex))
    await store.plan(tenant, user, task['id'], [a.prepare({'item': 'fixture'}) for a in adapters])
    return task['id']


def test_unknown_tools_and_changed_action_are_rejected():
    registry = Registry({'send': FakeSite('SEND')})
    with pytest.raises(TaskError):
        registry.prepare(StepInput(tool='pay', arguments={}))
    action = registry.prepare(StepInput(tool='send', arguments={'item': 'A'}))
    with pytest.raises(TaskError):
        registry.resolve({'tool': 'send', 'arguments': {'item': 'B'}, 'action_hash': action.digest})


def test_queue_and_state_rollback_together():
    async def check():
        async with fixture_store() as (store, tenant, user):
            task = await planned(store, tenant, user, FakeSite())
            original = store.event
            async def fail(*args, **kwargs):
                raise RuntimeError('fixture failure before commit')
            store.event = fail
            with pytest.raises(RuntimeError):
                await store.start(tenant, user, task)
            store.event = original
            assert (await store.get(tenant, user, task))['status'] == 'DRAFT'
            assert await store.claim() is None
    asyncio.run(check())


def test_approval_payment_handoff_and_reconciliation():
    async def check():
        async with fixture_store() as (store, tenant, user):
            read, send, payment = FakeSite(), FakeSite('SEND'), FakeSite('USER_ONLY')
            registry = Registry({'read': read, 'send': send, 'user_only': payment})
            task = await planned(store, tenant, user, read, send, payment)
            await store.start(tenant, user, task)
            await tick(store, registry)
            row = await store.get(tenant, user, task)
            assert row['status'] == 'WAITING_APPROVAL' and send.calls == 0
            with pytest.raises(TaskError):
                await store.approve(tenant, user, task, '0' * 64)
            await store.approve(tenant, user, task, row['steps'][1]['action_hash'])
            assert send.calls == 0  # API approval records work; it never executes it.
            await tick(store, registry)
            assert (await store.get(tenant, user, task))['status'] == 'WAITING_USER'
            assert payment.calls == 0
            await store.control(tenant, user, task, 'reconcile')
            await tick(store, registry)
            assert (await store.get(tenant, user, task))['status'] == 'OUTCOME_UNKNOWN'
            payment.verified = Reconciliation(True, {'receipt_reference': 'merchant-confirmation-fixture'})
            await store.control(tenant, user, task, 'reconcile')
            await tick(store, registry)
            assert (await store.get(tenant, user, task))['status'] == 'DONE'
            assert read.calls == send.calls == 1 and payment.calls == 0
    asyncio.run(check())


@pytest.mark.parametrize('risk,expected', [('READ', 'READY'), ('SEND', 'OUTCOME_UNKNOWN')])
def test_crash_recovery_never_repeats_uncertain_submission(risk, expected):
    async def check():
        async with fixture_store() as (store, tenant, user):
            adapter = FakeSite(risk)
            task = await planned(store, tenant, user, adapter)
            await store.start(tenant, user, task)
            if risk == 'SEND':
                await store.approve(tenant, user, task, adapter.prepare({'item': 'fixture'}).digest)
            claimed = await store.claim()
            assert claimed
            async with store.pool.connection() as conn:
                await conn.execute("UPDATE chore_tasks SET lease_until=now()-interval '1 second' WHERE id=%s", (task,))
            await store.recover()
            assert (await store.get(tenant, user, task))['status'] == expected
            assert not await store.finish(claimed, {'late': True})
            if risk == 'SEND':
                assert await store.claim() is None
    asyncio.run(check())


def test_competing_workers_and_per_user_exclusion():
    async def check():
        async with fixture_store() as (store, tenant, user):
            task = await planned(store, tenant, user, FakeSite())
            other = await planned(store, tenant, user, FakeSite())
            await store.start(tenant, user, task)
            with pytest.raises(UniqueViolation):
                await store.start(tenant, user, other)
            claims = await asyncio.gather(store.claim(), store.claim())
            assert sum(c is not None for c in claims) == 1
            assert (await store.get(tenant, user, other))['status'] == 'DRAFT'
            with pytest.raises(TaskError):
                await store.get(tenant, uuid4(), task)
            with pytest.raises(TaskError):
                await store.control('another-tenant', user, task, 'cancel')
    asyncio.run(check())


def test_takeover_fences_old_worker_and_invalidates_approval():
    async def check():
        async with fixture_store() as (store, tenant, user):
            send = FakeSite('SEND')
            task = await planned(store, tenant, user, send)
            await store.start(tenant, user, task)
            await store.approve(tenant, user, task, send.prepare({'item': 'fixture'}).digest)
            claimed = await store.claim()
            await store.control(tenant, user, task, 'pause')
            assert not await store.authorized(task, claimed['lease_token'])
            assert not await store.finish(claimed, {'late': True})
            assert (await store.get(tenant, user, task))['status'] == 'OUTCOME_UNKNOWN'
            with pytest.raises(TaskError):
                await store.control(tenant, user, task, 'resume')
    asyncio.run(check())


def test_expired_approval_requires_new_yes():
    async def check():
        async with fixture_store() as (store, tenant, user):
            send = FakeSite('SEND')
            task = await planned(store, tenant, user, send)
            await store.start(tenant, user, task)
            await store.approve(tenant, user, task, send.prepare({'item': 'fixture'}).digest)
            async with store.pool.connection() as conn:
                await conn.execute("UPDATE chore_approvals SET expires_at=now()-interval '1 second' WHERE task_id=%s", (task,))
            assert await store.claim() is None
            assert (await store.get(tenant, user, task))['status'] == 'WAITING_APPROVAL'
    asyncio.run(check())


@pytest.mark.parametrize('risk,expected_calls', [('READ', 3), ('SEND', 1)])
def test_failures_retry_reads_only(risk, expected_calls):
    class BrokenSite(FakeSite):
        async def execute(self, action, **context):
            self.calls += 1
            raise TimeoutError('fixture interruption')

    async def check():
        async with fixture_store() as (store, tenant, user):
            adapter = BrokenSite(risk)
            task = await planned(store, tenant, user, adapter)
            await store.start(tenant, user, task)
            if risk == 'SEND':
                await store.approve(tenant, user, task, adapter.prepare({'item': 'fixture'}).digest)
            registry = Registry({risk.lower(): adapter})
            for _ in range(4):
                await tick(store, registry)
                async with store.pool.connection() as conn:
                    await conn.execute('UPDATE chore_jobs SET available_at=now() WHERE task_id=%s', (task,))
            assert adapter.calls == expected_calls
            assert (await store.get(tenant, user, task))['status'] == (
                'FAILED' if risk == 'READ' else 'OUTCOME_UNKNOWN')
    asyncio.run(check())


def test_cancelled_handoff_requires_reconciliation_before_releasing_user():
    async def check():
        async with fixture_store() as (store, tenant, user):
            payment, read = FakeSite('USER_ONLY'), FakeSite()
            task = await planned(store, tenant, user, payment, read)
            await store.start(tenant, user, task)
            await store.control(tenant, user, task, 'cancel')
            assert (await store.get(tenant, user, task))['status'] == 'OUTCOME_UNKNOWN'
            payment.verified = Reconciliation(False, {'checkout_status': 'expired-without-payment'})
            await store.control(tenant, user, task, 'reconcile')
            await tick(store, Registry({'user_only': payment, 'read': read}))
            assert (await store.get(tenant, user, task))['status'] == 'CANCELLED'
            assert await store.claim() is None
            assert payment.calls == read.calls == 0
    asyncio.run(check())


def test_confirmed_non_submission_needs_fresh_approval():
    async def check():
        async with fixture_store() as (store, tenant, user):
            send = FakeSite('SEND')
            task = await planned(store, tenant, user, send)
            await store.start(tenant, user, task)
            await store.approve(tenant, user, task, send.prepare({'item': 'fixture'}).digest)
            claimed = await store.claim()
            await store.finish(claimed, failed=True)
            send.verified = Reconciliation(False, {'merchant_lookup': 'submission-absent'})
            await store.control(tenant, user, task, 'reconcile')
            await tick(store, Registry({'send': send}))
            assert (await store.get(tenant, user, task))['status'] == 'WAITING_APPROVAL'
            assert await store.claim() is None and send.calls == 0
    asyncio.run(check())


def test_task_api_auth_and_exact_approval(monkeypatch):
    monkeypatch.setenv('AI_PLATFORM_DATABASE_URL', database())
    monkeypatch.setenv('AI_BROWSER_TASKS', '1')
    secret, tenant, user = 't' * 48, 'api-' + uuid4().hex, uuid4()
    def headers(owner=user):
        return {'Authorization': 'Bearer ' + issue(secret, issuer='nraialgo', audience='agent-platform',
            tenant=tenant, user=owner, scope='agents:access')}
    send = FakeSite('SEND')
    with TestClient(create_app(signing_key=secret, task_registry=Registry({'send': send}))) as client:
        assert client.get('/v1/tasks').status_code == 401
        body = {'request': 'Book fixture ticket', 'idempotency_key': uuid4().hex}
        response = client.post('/v1/tasks', headers=headers(), json=body)
        assert response.status_code == 201
        task = response.json()['id']
        assert client.post('/v1/tasks', headers=headers(), json=body).json()['id'] == task
        assert client.post('/v1/tasks', headers=headers(), json={**body, 'request': 'A different task'}).status_code == 409
        assert client.get('/v1/tasks/' + task, headers=headers(uuid4())).status_code == 404
        assert client.put(f'/v1/tasks/{task}/plan', headers=headers(), json={
            'steps': [{'tool': 'send', 'arguments': {'item': 'ticket'}}]}).status_code == 200
        state = client.post(f'/v1/tasks/{task}/start', headers=headers()).json()
        approval = {'decision': 'yes', 'action_hash': state['steps'][0]['action_hash']}
        assert client.post(f'/v1/tasks/{task}/approve', headers=headers(), json=approval).status_code == 200
        assert send.calls == 0
        assert client.put(f'/v1/tasks/{task}/plan', headers=headers(), json={
            'steps': [{'tool': 'send', 'arguments': {'item': 'changed ticket'}}]}).status_code == 409
        assert client.post(f'/v1/tasks/{task}/approve', headers=headers(), json=approval).status_code == 409
        assert client.post(f'/v1/tasks/{task}/cancel', headers=headers()).status_code == 200


@pytest.mark.parametrize('command', ['pause', 'cancel'])
@pytest.mark.parametrize('risk', ['SEND', 'USER_ONLY'])
def test_queued_reconciliation_preserves_uncertainty(command, risk):
    async def check():
        async with fixture_store() as (store, tenant, user):
            adapter = FakeSite(risk)
            task = await planned(store, tenant, user, adapter)
            await store.start(tenant, user, task)
            if risk == 'SEND':
                await store.approve(tenant, user, task, adapter.prepare({'item': 'fixture'}).digest)
                claimed = await store.claim()
                await store.finish(claimed, failed=True)
            await store.control(tenant, user, task, 'reconcile')
            await store.control(tenant, user, task, command)
            row = await store.get(tenant, user, task)
            assert row['status'] == 'OUTCOME_UNKNOWN'
            assert row['steps'][0]['outcome_pending']
            with pytest.raises(TaskError):
                await store.control(tenant, user, task, 'resume')
            with pytest.raises(TaskError):
                await store.approve(tenant, user, task, adapter.prepare({'item': 'fixture'}).digest)
            other = await planned(store, tenant, user, FakeSite())
            with pytest.raises(UniqueViolation):
                await store.start(tenant, user, other)
            assert await store.claim() is None
            # A worker restart during reconciliation also preserves uncertainty.
            await store.control(tenant, user, task, 'reconcile')
            claimed = await store.claim()
            async with store.pool.connection() as conn:
                await conn.execute("UPDATE chore_tasks SET lease_until=now()-interval '1 second' WHERE id=%s", (task,))
            await store.recover()
            assert not await store.finish(claimed, {'late': True})
            adapter.verified = Reconciliation(False, {'verified_state': 'not-applied'})
            await store.control(tenant, user, task, 'reconcile')
            await tick(store, Registry({risk.lower(): adapter}))
            row = await store.get(tenant, user, task)
            assert row['status'] == ('PAUSED' if command == 'pause' else 'CANCELLED')
            assert not row['steps'][0]['outcome_pending']
            assert adapter.calls == 0
    asyncio.run(check())
