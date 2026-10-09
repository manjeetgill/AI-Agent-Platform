"""Mobile/app API -> durable server renderer -> evidence, without browser pairing."""
from uuid import uuid4

from test_connector_checks import drive, run
from test_retailer_browser import URL, capture, finish, pair, poll, setup
from test_task_agents import sql

from agent_platform.retailer_browser import Capture, as_probe
from agent_platform.server_retailer import render, tick


def create(client,headers):
    r=client.post('/v1/task-agents',headers=headers,json={'idempotency_key':uuid4().hex,
        'blueprint':{'name':'Server public observation','description':'No client browser required',
            'steps':[{'tool':'retailer.observe','version':'1','arguments':{'retailer':'croma','url':URL}}]}})
    assert r.status_code==201,r.text
    return r.json()['id']


def grant(key):
    sql('INSERT INTO platform_tool_grants(app_id,tool) VALUES(%s,%s)',(key['app_id'],'retailer.observe'))


def test_mobile_client_completes_without_pairing_and_cannot_use_other_transport(monkeypatch):
    with setup(monkeypatch) as (client,headers,key,registry):
        grant(key)
        inventory=client.get('/v1/connectors',headers=headers).json()['connectors']
        server=next(x for x in inventory if x['tool']=='retailer.observe')
        assert server['execution_location']=='server' and not server['requires_customer_browser']
        task=run(client,headers,create(client,headers));drive(registry)
        assert not sql('SELECT id FROM retailer_browser_devices WHERE app_id=%s',(key['app_id'],))
        calls=[]
        async def renderer(job,authorized):
            assert await authorized();calls.append(str(job['id']))
            return as_probe(job,Capture.model_validate(capture()))
        # A paired browser cannot claim a server job even for this same owner.
        device=pair(client,headers);assert poll(client,device) is None
        assert client.post('/v1/retailer-browser/stop',headers=device).status_code==200
        assert client.get('/v1/tasks/'+task,headers=headers).json()['status']=='READY'
        assert drive(registry,lambda pool:tick(pool,renderer))
        assert not drive(registry,lambda pool:tick(pool,renderer))
        report=finish(client,headers,registry,task)
        assert report['connector']=='retailer.observe' and report['usable_evidence']
        assert len(calls)==1
        job=sql('SELECT executor,device_id,run_token FROM retailer_browser_jobs WHERE task_id=%s',(task,))[0]
        assert job['executor']=='server' and job['device_id'] is None and job['run_token'] is not None


def test_missing_server_runtime_returns_configuration_without_customer_extension(monkeypatch):
    monkeypatch.delenv('AI_RETAILER_NODE',raising=False)
    monkeypatch.delenv('AI_RETAILER_PLAYWRIGHT_MODULE',raising=False)
    with setup(monkeypatch) as (client,headers,key,registry):
        grant(key);task=run(client,headers,create(client,headers));drive(registry)
        assert drive(registry,lambda pool:tick(pool,render))
        report=finish(client,headers,registry,task)
        assert report['reason_code']=='server_browser_runtime_missing' and report['needs_attention']
        assert not report['usable_evidence']


def test_revoked_grant_never_dispatches_and_crashed_server_never_restarts_job(monkeypatch):
    with setup(monkeypatch) as (client,headers,key,registry):
        grant(key);task=run(client,headers,create(client,headers));drive(registry)
        calls=[]
        async def renderer(job,authorized):
            calls.append(1);raise AssertionError('Must not run')
        sql('DELETE FROM platform_tool_grants WHERE app_id=%s AND tool=%s',(key['app_id'],'retailer.observe'))
        assert not drive(registry,lambda pool:tick(pool,renderer))
        grant(key)
        sql("UPDATE retailer_browser_jobs SET state='STARTED',run_token=%s WHERE task_id=%s",(uuid4(),task))
        assert not drive(registry,lambda pool:tick(pool,renderer))
        assert not calls
        sql("UPDATE retailer_browser_jobs SET expires_at=now()-interval '1 second' WHERE task_id=%s",(task,))
        assert finish(client,headers,registry,task)['reason_code']=='server_runner_offline_or_interrupted'


def test_server_report_isolated_from_another_app(monkeypatch):
    with setup(monkeypatch) as (client,headers,key,registry):
        grant(key);task=run(client,headers,create(client,headers));drive(registry)
        async def renderer(job,authorized):
            assert await authorized();return as_probe(job,Capture.model_validate(capture()))
        drive(registry,lambda pool:tick(pool,renderer));finish(client,headers,registry,task)
        with setup(monkeypatch) as (other,other_headers,other_key,other_registry):
            assert not other.get('/v1/connectors/reports',headers=other_headers).json()['reports']
            assert other.get('/v1/tasks/'+task,headers=other_headers).status_code==404
            grant(other_key);task2=run(other,other_headers,create(other,other_headers));drive(other_registry)
            drive(other_registry,lambda pool:tick(pool,renderer));finish(other,other_headers,other_registry,task2)
            assert len(client.get('/v1/connectors/reports',headers=headers).json()['reports'])==1
            assert len(other.get('/v1/connectors/reports',headers=other_headers).json()['reports'])==1


def test_incomplete_render_with_denied_resources_has_operator_diagnostics(monkeypatch):
    with setup(monkeypatch) as (client, headers, key, registry):
        grant(key); task=run(client, headers, create(client, headers)); drive(registry)
        async def renderer(job, authorized):
            assert await authorized()
            value = capture()
            value.update(outcome='PAGE_CHANGED', title=None, price=None, diagnostics={
                'transport': 'server_browser', 'http_status': 200, 'requests': 15,
                'blocked_resources': 3, 'failed_requests': 3})
            return as_probe(job, Capture.model_validate(value))
        drive(registry, lambda pool: tick(pool, renderer))
        report=finish(client, headers, registry, task)
        assert report['reason_code']=='browser_resource_policy'
        assert report['needs_attention'] and not report['usable_evidence']
        assert report['probe']['blocked_resources']==3
        assert report['probe']['transport']=='server_browser'
