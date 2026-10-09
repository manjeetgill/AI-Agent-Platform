import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest
from test_connector_checks import drive, result, setup
from test_task_agents import sql

from agent_platform.dealagent_live import DealAgentLiveProbe


def test_live_probe_uses_worker_circuit_and_reports_network_details(monkeypatch):
    from uuid import uuid4
    with setup(monkeypatch) as (client, headers, key, registry, _):
        adapter=DealAgentLiveProbe(registry)
        registry.adapters['dealagent.live_probe']=adapter
        sql('INSERT INTO platform_tool_grants(app_id,tool) VALUES(%s,%s)',(key['app_id'],'dealagent.live_probe'))
        calls=[]
        async def health(action, *, authorized, **context):
            assert await authorized();calls.append(action.arguments)
            return {'status':'BLOCKED','reason_code':'http_403','verification_mode':'live_source',
                'probe':{'source_url':'https://www.bigbasket.com/pd/104860/', 'checked_at':datetime.now(UTC).isoformat(),
                         'parser_version':'1','http_status':403,'http_requests':2,'product_requests':1}}
        monkeypatch.setattr(adapter,'health_check',health)
        agent=client.post('/v1/task-agents',headers=headers,json={'idempotency_key':uuid4().hex,
            'blueprint':{'name':'Live retailer check','description':'One configured source',
                'steps':[{'tool':'dealagent.live_probe','version':'1','arguments':{'retailer':'bigbasket'}}]}})
        assert agent.status_code==201,agent.text
        for _ in range(2):
            task=client.post('/v1/task-agents/'+agent.json()['id']+'/runs',headers=headers,json={'idempotency_key':uuid4().hex})
            assert task.status_code==202,task.text
            drive(registry)
            report=result(client,headers,task.json()['id'])
            if len(calls)==1 and report['execution']=='EXECUTED':
                assert report['probe']['http_status']==403 and report['verification_mode']=='live_source'
            else: assert report['execution']=='DEFERRED'
        assert len(calls)==1
        assert adapter.health_scope(adapter.prepare({'retailer':'bigbasket','pincode':'560001'}))=='bigbasket'
        with pytest.raises(ValueError): adapter.prepare({'retailer':'evil','url':'https://evil.example'})


def test_missing_probe_configuration_never_starts_a_process(monkeypatch):
    monkeypatch.delenv('DEALAGENT_PROBE_ROOT',raising=False)
    monkeypatch.delenv('DEALAGENT_PROBE_NODE',raising=False)
    adapter=DealAgentLiveProbe(None)
    async def authorized():return True
    answer=asyncio.run(adapter.health_check(adapter.prepare({'retailer':'croma'}),task_id='fixture',authorized=authorized))
    assert answer['status']=='CONFIGURATION_REQUIRED'


def test_revocation_kills_probe_process(monkeypatch,tmp_path):
    root=tmp_path/'dealagent';loader=root/'engine/node_modules/tsx/dist/loader.mjs';loader.parent.mkdir(parents=True);loader.touch()
    monkeypatch.setenv('DEALAGENT_PROBE_ROOT',str(root));monkeypatch.setenv('DEALAGENT_PROBE_NODE',str(Path('/bin/sh')))
    class Process:
        returncode=None
        killed=False
        stdout=None
        async def read(self,_): await asyncio.sleep(30)
        def kill(self):self.killed=True;self.returncode=-9
        async def wait(self):return self.returncode
    process=Process();process.stdout=process
    async def spawn(*args,**kwargs):
        assert 'NVIDIA_API_KEY' not in kwargs['env']
        return process
    monkeypatch.setattr(asyncio,'create_subprocess_exec',spawn)
    checks=[]
    async def authorized():checks.append(1);return len(checks)==1
    adapter=DealAgentLiveProbe(None)
    with pytest.raises(ValueError):
        asyncio.run(adapter.health_check(adapter.prepare({'retailer':'croma'}),task_id='fixture',authorized=authorized))
    assert process.killed
