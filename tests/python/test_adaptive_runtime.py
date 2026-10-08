import asyncio
from uuid import uuid4

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from test_automation import setup  # noqa: F401 -- shared isolated database fixture
from test_durable_tasks import database
from test_task_agents import INPUT, sql

from agent_platform.adaptive_runtime import Decision, install
from agent_platform.decision_tool import DecisionComparison, default_registry
from agent_platform.task_store import TaskStore
from agent_platform.task_worker import tick
from agent_platform.tasks import Action


class LoopGateway:
    def __init__(self, tool='decision.compare'):
        self.calls = 0
        self.tool = tool

    async def structured(self, task, policy, data, schema, validator):
        assert schema is Decision
        assert 'never permission' in policy
        self.calls += 1
        assert all(t['tool'] != 'runtime.next' for t in data['tools'])
        if self.calls == 1:
            assert not data['results']
            return Decision(done=False, tool=self.tool, arguments=INPUT, explanation='Compare supplied scores'), 40, 'fixture', 'test-model'
        assert data['results'][0]['result']['ranking']
        return Decision(done=True, explanation='Comparison is complete; review the recorded ranking.'), 40, 'fixture', 'test-model'


def test_model_tool_result_next_action_loop_is_durable(setup):  # noqa: F811
    client, headers, key, _ = setup
    sql('INSERT INTO platform_tool_grants(app_id,tool) VALUES(%s,%s)', (key['app_id'], 'runtime.next'))
    sql('INSERT INTO platform_app_policies(app_id,max_plan_steps) VALUES(%s,7)', (key['app_id'],))
    try:
        response = client.post('/v1/task-agents', headers=headers, json={'idempotency_key': uuid4().hex,
            'blueprint': {'name': 'Adaptive comparison', 'description': 'Fixture loop', 'steps': [
                {'tool': 'runtime.next', 'version': '1', 'arguments': {'goal': 'Compare the supplied fixture options', 'max_tool_calls': 2}}]}})
        assert response.status_code == 201, response.text
        task = client.post(f'/v1/task-agents/{response.json()["id"]}/runs', headers=headers,
                           json={'idempotency_key': uuid4().hex}).json()['id']
        gateway = LoopGateway()
        async def run():
            async with AsyncConnectionPool(database(), open=False, kwargs={'row_factory': dict_row}) as pool:
                registry = default_registry()
                install(registry, pool, gateway)
                store = TaskStore(pool)
                assert await tick(store, registry)
                # A new store instance resumes from persisted tool selection and arguments.
                assert await tick(TaskStore(pool), registry)
                assert await tick(TaskStore(pool), registry)
        asyncio.run(run())
        row = client.get('/v1/tasks/'+task, headers=headers).json()
        assert row['status'] == 'DONE'
        assert [s['tool'] for s in row['steps']] == ['runtime.next', 'decision.compare', 'runtime.next']
        assert row['steps'][1]['result']['ranking'][0]['name'] == 'A'
        assert gateway.calls == 2
        assert len(sql("SELECT * FROM platform_model_calls WHERE app_id=%s AND operation='next_action'", (key['app_id'],))) == 2
    finally:
        sql('DELETE FROM platform_app_policies WHERE app_id=%s', (key['app_id'],))


def test_dynamic_send_requires_exact_approval_and_denies_ungranted_tool(setup):  # noqa: F811
    client, headers, key, _ = setup
    sql('INSERT INTO platform_tool_grants(app_id,tool) VALUES(%s,%s)', (key['app_id'], 'runtime.next'))
    sql('INSERT INTO platform_tool_grants(app_id,tool) VALUES(%s,%s)', (key['app_id'], 'fixture.send'))
    class Sender(DecisionComparison):
        risk = 'SEND'
        calls = 0

        def prepare(self, arguments):
            prepared = super().prepare(arguments)
            return Action('fixture.send', '1', prepared.arguments, 'Send fixture comparison', 'SEND')

        async def execute(self, action, **context):
            self.calls += 1
            return await super().execute(action, **context)

    sender = Sender()
    def create():
        response = client.post('/v1/task-agents', headers=headers, json={'idempotency_key': uuid4().hex,
            'blueprint': {'name': 'Adaptive guarded', 'description': 'Fixture loop', 'steps': [
                {'tool': 'runtime.next', 'version': '1', 'arguments': {'goal': 'Compare supplied fixture options'}}]}})
        assert response.status_code == 201, response.text
        return client.post(f'/v1/task-agents/{response.json()["id"]}/runs', headers=headers,
                           json={'idempotency_key': uuid4().hex}).json()['id']

    def run(model):
        async def step():
            async with AsyncConnectionPool(database(), open=False, kwargs={'row_factory': dict_row}) as pool:
                registry = default_registry()
                registry.adapters['fixture.send'] = sender
                install(registry, pool, model)
                await tick(TaskStore(pool), registry)
        asyncio.run(step())

    task = create()
    model = LoopGateway('fixture.send')
    run(model)
    row = client.get('/v1/tasks/'+task, headers=headers).json()
    assert row['status'] == 'WAITING_APPROVAL' and sender.calls == 0
    assert client.post(f'/v1/tasks/{task}/approve', headers=headers,
        json={'decision': 'yes', 'action_hash': '0'*64}).status_code == 409
    assert client.post(f'/v1/tasks/{task}/approve', headers=headers,
        json={'decision': 'yes', 'action_hash': row['steps'][1]['action_hash']}).status_code == 200
    run(model)
    run(model)
    assert sender.calls == 1
    assert client.get('/v1/tasks/'+task, headers=headers).json()['status'] == 'DONE'
    denied = create()
    run(LoopGateway('fixture.ungranted'))
    row = client.get('/v1/tasks/'+denied, headers=headers).json()
    assert len(row['steps']) == 1 and row['steps'][0]['tool'] == 'runtime.next'
    assert sender.calls == 1
