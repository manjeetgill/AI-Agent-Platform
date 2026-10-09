import asyncio
from contextlib import contextmanager
from uuid import uuid4

import pytest
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from test_durable_tasks import database
from test_task_agents import environment, sql

from agent_platform.decision_tool import default_registry
from agent_platform.external_jobs import DeferredJob, JobFailure
from agent_platform.task_store import TaskStore
from agent_platform.task_worker import tick
from agent_platform.tasks import Action, Reconciliation, Strict
from agent_platform.workflow_runtime import Workflow, install, select


class Input(Strict):
    source: str


class Observation(Strict):
    product_id: str
    price: float


class Collector:
    version, risk, timeout = '1', 'SEND', 5
    description = 'Fixture: start bounded collection, then poll its persistent external ID.'
    input_model, output_model = Input, Observation

    def __init__(self):
        self.submissions = 0
        self.polls = 0
        self.ready = False
        self.error = False
        self.empty_poll = False
        self.rejected = False
        self.lost_ack = False
        self.keys = []

    def prepare(self, arguments):
        value = Input.model_validate(arguments, strict=True)
        return Action('fixture.collect', '1', value.model_dump(), 'Collect the supplied source', 'SEND')

    async def execute(self, action, *, operation_id, authorized, **context):
        assert await authorized()
        self.submissions += 1
        self.keys.append(operation_id)
        if self.lost_ack:
            raise TimeoutError('Fixture lost acknowledgement')
        return DeferredJob({'job_id': operation_id}, poll_after_seconds=1)

    async def poll(self, action, *, reference, authorized, **context):
        assert await authorized()
        assert reference == {'job_id': self.keys[0]}
        self.polls += 1
        if self.empty_poll:
            return None
        if self.error:
            raise TimeoutError('Fixture read timeout')
        if self.rejected:
            return JobFailure({'reason': 'retailer_blocked', 'observations': []})
        if self.ready:
            return {'product_id': 'observed-123', 'price': 499.0}
        return DeferredJob(reference, poll_after_seconds=1)

    async def reconcile(self, action, *, operation_id, **context):
        assert operation_id == self.keys[0]
        if self.ready:
            return Reconciliation(True, {'product_id': 'observed-123', 'price': 499.0})
        return DeferredJob({'job_id': operation_id}, poll_after_seconds=1)


class Consumer:
    version, risk, timeout = '1', 'SEND', 5
    description = 'Fixture: notify about an observed product only after approval.'
    input_model, output_model = Observation, Observation

    def __init__(self):
        self.calls = []

    def prepare(self, arguments):
        body = Observation.model_validate(arguments, strict=True)
        return Action('fixture.notify', '1', body.model_dump(), f'Notify price {body.price} for {body.product_id}', 'SEND')

    async def execute(self, action, **context):
        assert await context['authorized']()
        self.calls.append(action.arguments)
        return action.arguments

    async def reconcile(self, action, **context):
        return None


@contextmanager
def fixture(monkeypatch):
    monkeypatch.setenv('AI_AUTOMATIONS','1')
    registry = default_registry()
    collector, consumer = Collector(), Consumer()
    registry.adapters.update({'fixture.collect':collector,'fixture.notify':consumer})
    with environment(monkeypatch, registry) as env:
        client, headers, key, _ = env
        for name in ['runtime.workflow','fixture.collect','fixture.notify']:
            sql('INSERT INTO platform_tool_grants(app_id,tool) VALUES(%s,%s)',(key['app_id'],name))
        yield client, headers, key, registry, collector, consumer


def drive(registry, task=None, *, claim_only=False):
    if task:
        sql('UPDATE chore_jobs SET available_at=now() WHERE task_id=%s',(task,))
    async def run():
        async with AsyncConnectionPool(database(),open=False,kwargs={'row_factory':dict_row}) as pool:
            install(registry, pool)
            store=TaskStore(pool)
            return await store.claim() if claim_only else await tick(store, registry)
    return asyncio.run(run())


def create(client, headers, *, flow=False, missing=False):
    nodes=[{'tool':'fixture.collect','version':'1','arguments':{'source':'known-url'}}]
    if flow:
        nodes=[{'tool':'runtime.workflow','version':'1','arguments':{'steps':nodes+[
            {'tool':'fixture.notify','version':'1','bindings':{
                'product_id':{'step':0,'pointer':'/missing' if missing else '/product_id'},
                'price':{'step':0,'pointer':'/price'}}}]}}]
    agent=client.post('/v1/task-agents',headers=headers,json={'idempotency_key':uuid4().hex,
        'blueprint':{'name':'Fixture external workflow','description':'Bounded fixture job','steps':nodes}})
    assert agent.status_code==201,agent.text
    task=client.post('/v1/task-agents/'+agent.json()['id']+'/runs',headers=headers,json={'idempotency_key':uuid4().hex})
    assert task.status_code==202,task.text
    return task.json()['id']


def state(client,headers,task):
    return client.get('/v1/tasks/'+task,headers=headers).json()


def approve(client,headers,task):
    row=state(client,headers,task)
    assert row['status']=='WAITING_APPROVAL'
    result=client.post('/v1/tasks/'+task+'/approve',headers=headers,
        json={'decision':'yes','action_hash':row['steps'][row['current_step']]['action_hash']})
    assert result.status_code==200,result.text


def test_wait_restart_resolve_arguments_and_exact_send_approval(monkeypatch):
    with fixture(monkeypatch) as (client,headers,_key,registry,collector,consumer):
        task=create(client,headers,flow=True)
        assert drive(registry,task)
        approve(client,headers,task)
        assert drive(registry,task)
        row=state(client,headers,task)
        assert row['status']=='READY' and row['steps'][1]['external_job_state']=='RUNNING'
        deadline=row['steps'][1]['external_job_deadline']
        assert collector.submissions==1 and not consumer.calls
        assert drive(registry,task)  # Pending poll keeps the same deadline and reference.
        assert state(client,headers,task)['steps'][1]['external_job_deadline']==deadline
        claim=drive(registry,task,claim_only=True)
        assert claim['mode']=='POLL'
        sql("UPDATE chore_tasks SET lease_until=now()-interval '1 second' WHERE id=%s",(task,))
        drive(registry)  # New pool/store recovers the crashed poll, without starting the job again.
        collector.ready=True
        assert drive(registry,task)
        assert drive(registry,task)  # Resolve the observed ID and price into next arguments.
        row=state(client,headers,task)
        assert row['status']=='WAITING_APPROVAL' and collector.submissions==1 and not consumer.calls
        pending=row['steps'][row['current_step']]
        assert pending['arguments']=={'product_id':'observed-123','price':499.0}
        assert client.post('/v1/tasks/'+task+'/approve',headers=headers,json={'decision':'yes','action_hash':'0'*64}).status_code==409
        assert client.get('/v1/tasks/'+task,headers={**headers,'X-External-User-ID':'other'}).status_code==404
        approve(client,headers,task)
        assert drive(registry,task)
        assert drive(registry,task)
        assert state(client,headers,task)['status']=='DONE'
        assert consumer.calls==[{'product_id':'observed-123','price':499.0}]


@pytest.mark.parametrize('failure',['deadline','poll_errors','empty_poll','rejected','missing_result'])
def test_failed_jobs_never_advance_or_resubmit(monkeypatch,failure):
    with fixture(monkeypatch) as (client,headers,_key,registry,collector,consumer):
        task=create(client,headers,flow=True,missing=failure=='missing_result')
        drive(registry,task);approve(client,headers,task);drive(registry,task)
        if failure=='deadline':
            sql("UPDATE chore_steps SET external_job_deadline=now()-interval '1 second' WHERE task_id=%s AND position=1",(task,))
            drive(registry,task)
        elif failure in {'poll_errors','empty_poll'}:
            collector.error=failure=='poll_errors'
            collector.empty_poll=failure=='empty_poll'
            for _ in range(3):drive(registry,task)
        elif failure=='rejected':
            collector.rejected=True;drive(registry,task)
        else:
            collector.ready=True;drive(registry,task)
            for _ in range(3):drive(registry,task)
        row=state(client,headers,task)
        assert row['status'] in {'FAILED','OUTCOME_UNKNOWN'}
        assert collector.submissions==1 and not consumer.calls
        assert all(s['tool']!='fixture.notify' for s in row['steps'])


def test_pause_resume_and_cancel_only_stop_tracking(monkeypatch):
    with fixture(monkeypatch) as (client,headers,_key,registry,collector,_consumer):
        task=create(client,headers);approve(client,headers,task);drive(registry,task)
        assert client.post('/v1/tasks/'+task+'/pause',headers=headers).status_code==200
        assert not drive(registry,task)
        assert client.post('/v1/tasks/'+task+'/resume',headers=headers).status_code==200
        assert drive(registry,task)
        assert collector.submissions==1
        assert client.post('/v1/tasks/'+task+'/cancel',headers=headers).status_code==200
        assert not drive(registry,task)
        row=state(client,headers,task)
        assert row['status']=='CANCELLED' and row['steps'][0]['external_job_state']=='DETACHED'


def test_lost_ack_reconciles_operation_id_without_resubmission(monkeypatch):
    with fixture(monkeypatch) as (client,headers,_key,registry,collector,_consumer):
        collector.lost_ack=True
        task=create(client,headers);approve(client,headers,task);drive(registry,task)
        assert state(client,headers,task)['status']=='OUTCOME_UNKNOWN'
        assert not drive(registry,task)
        assert client.post('/v1/tasks/'+task+'/reconcile',headers=headers).status_code==200
        drive(registry,task)
        collector.ready=True
        drive(registry,task)
        assert state(client,headers,task)['status']=='DONE'
        assert collector.submissions==1


def test_bindings_are_data_only_backward_and_bounded():
    assert select({'a/b':[{'~value':7}]},'/a~1b/0/~0value')==7
    with pytest.raises(ValueError):
        Workflow.model_validate({'steps':[{'tool':'test.tool','version':'1','bindings':{'id':{'step':0,'pointer':'/id'}}}]})
    with pytest.raises(ValueError):
        Workflow.model_validate({'steps':[{'tool':'runtime.next','version':'1','arguments':{}}]})
    with pytest.raises(ValueError):
        DeferredJob({'id':'x'},timeout_seconds=3601)
    with pytest.raises(ValueError):
        select({'price':4},'/missing')


def test_revoked_grant_stops_polling_and_resume_requires_restoration(monkeypatch):
    with fixture(monkeypatch) as (client,headers,key,registry,collector,_consumer):
        task=create(client,headers);approve(client,headers,task);drive(registry,task)
        sql('DELETE FROM platform_tool_grants WHERE app_id=%s AND tool=%s',(key['app_id'],'fixture.collect'))
        assert not drive(registry,task)
        assert state(client,headers,task)['status']=='PAUSED'
        assert collector.polls==0 and collector.submissions==1
        sql('INSERT INTO platform_tool_grants(app_id,tool) VALUES(%s,%s)',(key['app_id'],'fixture.collect'))
        assert client.post('/v1/tasks/'+task+'/resume',headers=headers).status_code==200
        collector.ready=True
        drive(registry,task)
        assert state(client,headers,task)['status']=='DONE' and collector.submissions==1


def test_workflow_rejects_missing_grant_and_enforces_total_step_budget(monkeypatch):
    with fixture(monkeypatch) as (client,headers,key,registry,collector,consumer):
        sql('DELETE FROM platform_tool_grants WHERE app_id=%s AND tool=%s',(key['app_id'],'fixture.notify'))
        body={'idempotency_key':uuid4().hex,'blueprint':{'name':'Forbidden workflow','description':'Fixture',
            'steps':[{'tool':'runtime.workflow','version':'1','arguments':{'steps':[
                {'tool':'fixture.notify','version':'1','arguments':{'product_id':'supplied','price':1.0}}]}}]}}
        assert client.post('/v1/task-agents',headers=headers,json=body).status_code==422
        sql('INSERT INTO platform_tool_grants(app_id,tool) VALUES(%s,%s)',(key['app_id'],'fixture.notify'))
        sql('INSERT INTO platform_app_policies(app_id,max_plan_steps) VALUES(%s,2)',(key['app_id'],))
        try:
            task=create(client,headers,flow=True)
            for _ in range(3):drive(registry,task)
            assert state(client,headers,task)['status']=='FAILED'
            assert collector.submissions==0 and not consumer.calls
        finally:
            sql('DELETE FROM platform_app_policies WHERE app_id=%s',(key['app_id'],))
