"""Worker/API security and recovery contracts; captures here are fixtures, not live prices."""
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from test_connector_checks import drive, result, run
from test_connector_checks import setup as base_setup
from test_task_agents import sql

from agent_platform.retailer_browser import TOOL, BrowserInput

URL='https://www.croma.com/fixture-product/p/12345'


@contextmanager
def setup(monkeypatch):
    monkeypatch.setenv('AI_RETAILER_BROWSER','1')
    with base_setup(monkeypatch) as env:
        client,headers,key,registry,_=env
        sql('INSERT INTO platform_tool_grants(app_id,tool) VALUES(%s,%s)',(key['app_id'],TOOL))
        try:yield client,headers,key,registry
        finally:
            sql('DELETE FROM retailer_browser_jobs WHERE app_id=%s',(key['app_id'],))
            sql('DELETE FROM retailer_browser_devices WHERE app_id=%s',(key['app_id'],))


def create(client,headers,**arguments):
    response=client.post('/v1/task-agents',headers=headers,json={'idempotency_key':uuid4().hex,
        'blueprint':{'name':'Browser fixture','description':'Observe fixture','steps':[
            {'tool':TOOL,'version':'1','arguments':{'retailer':'croma','url':URL,**arguments}}]}})
    assert response.status_code==201,response.text
    return response.json()['id']


def pair(client,headers,retailers=None):
    response=client.post('/v1/retailer-browser/devices',headers=headers,json={'retailers':retailers or ['croma']})
    assert response.status_code==201,response.text
    return {'Authorization':'Bearer '+response.json()['pairing_code']}


def poll(client,device,retailers=None):
    response=client.post('/v1/retailer-browser/poll',headers=device,json={'retailers':retailers or ['croma']})
    assert response.status_code==200,response.text
    return response.json()['command']


def capture(**kwargs):
    return {'outcome':'OBSERVED','url':URL,'observed_at':datetime.now(UTC).isoformat(),
        'title':'Fixture product','variant':'1 pc','price':123.0,**kwargs}


def finish(client,headers,registry,task):
    sql('UPDATE chore_jobs SET available_at=now() WHERE task_id=%s',(task,))
    assert drive(registry)
    return result(client,headers,task)


def test_durable_browser_worker_report_and_no_duplicate_dispatch(monkeypatch):
    with setup(monkeypatch) as (client,headers,key,registry):
        device=pair(client,headers)
        task=run(client,headers,create(client,headers));assert drive(registry)
        command=poll(client,device);assert command['url']==URL
        assert poll(client,device) is None
        assert client.post(f"/v1/retailer-browser/{command['id']}/authorize",headers=device).status_code==200
        sent=client.post(f"/v1/retailer-browser/{command['id']}/result",headers=device,json=capture())
        assert sent.status_code==200,sent.text
        assert client.post(f"/v1/retailer-browser/{command['id']}/result",headers=device,json=capture()).status_code==409
        report=finish(client,headers,registry,task)
        assert report['usable_evidence'] and report['evidence'][0]['unknown_fees']
        assert report['report_id']==command['id']
        assert len(sql('SELECT * FROM retailer_browser_jobs WHERE app_id=%s',(key['app_id'],)))==1
        assert poll(client,device) is None
        assert 'secret' not in str(client.get('/v1/retailer-browser/devices',headers=headers).json())


def test_owner_app_and_device_isolation(monkeypatch):
    with setup(monkeypatch) as (client,headers,key,registry):
        device=pair(client,headers)
        other={**headers,'X-External-User-ID':'other-user'}
        assert client.get('/v1/retailer-browser/devices',headers=other).json()['devices']==[]
        other_device=pair(client,other)
        task=run(client,headers,create(client,headers));drive(registry)
        assert poll(client,other_device) is None
        command=poll(client,device)
        assert client.post(f"/v1/retailer-browser/{command['id']}/result",headers=other_device,json=capture()).status_code==409
        with setup(monkeypatch) as (client2,headers2,_,_registry2):
            device2=pair(client2,headers2)
            assert poll(client2,device2) is None
            assert client2.post(f"/v1/retailer-browser/{command['id']}/authorize",headers=device2).status_code==409
        # Revoking the app's tool also revokes an already issued device.
        sql('DELETE FROM platform_tool_grants WHERE app_id=%s AND tool=%s',(key['app_id'],TOOL))
        assert client.post('/v1/retailer-browser/poll',headers=device,json={'retailers':['croma']}).status_code==401
        assert client.get('/v1/tasks/'+task,headers=device).status_code in (401,403)


def test_pause_stop_and_repairing_revoke_access(monkeypatch):
    with setup(monkeypatch) as (client,headers,_key,registry):
        old=pair(client,headers);device=pair(client,headers)
        assert client.post('/v1/retailer-browser/poll',headers=old,json={}).status_code==401
        task=run(client,headers,create(client,headers));drive(registry)
        command=poll(client,device)
        response=client.post('/v1/retailer-browser/stop',headers=device)
        assert response.status_code==200,response.text
        assert client.get('/v1/tasks/'+task,headers=headers).json()['status']=='PAUSED'
        assert client.post(f"/v1/retailer-browser/{command['id']}/result",headers=device,json=capture()).status_code==401


def test_offline_job_expires_without_redispatch_and_blocks_source(monkeypatch):
    with setup(monkeypatch) as (client,headers,key,registry):
        agent=create(client,headers);task=run(client,headers,agent);drive(registry)
        sql("UPDATE retailer_browser_jobs SET expires_at=now()-interval '1 second' WHERE task_id=%s",(task,))
        report=finish(client,headers,registry,task)
        assert report['reason_code']=='browser_offline_or_interrupted' and report['needs_attention']
        second=run(client,headers,agent);drive(registry)
        assert result(client,headers,second)['reason_code']=='owner_action_required'
        assert len(sql('SELECT * FROM retailer_browser_jobs WHERE app_id=%s',(key['app_id'],)))==1
        response=client.post(f'/v1/connectors/{TOOL}/croma/resume',headers=headers,json={'report_id':report['report_id']})
        assert response.status_code==200,response.text


@pytest.mark.parametrize('change,expected',[
    ({'issues':['variant_conflict']},'variant_conflict'),
    ({'url':'https://www.croma.com/different/p/67890'},'browser_product_changed'),
    ({'outcome':'BLOCKED','price':None},'browser_blocked')])
def test_bad_evidence_never_becomes_usable(monkeypatch,change,expected):
    with setup(monkeypatch) as (client,headers,_,registry):
        device=pair(client,headers);task=run(client,headers,create(client,headers));drive(registry)
        command=poll(client,device)
        response=client.post(f"/v1/retailer-browser/{command['id']}/result",headers=device,json=capture(**change))
        assert response.status_code==200,response.text
        report=finish(client,headers,registry,task)
        assert not report['usable_evidence'] and expected in str(report)


def test_location_permissions_timestamp_and_quota(monkeypatch):
    with setup(monkeypatch) as (client,headers,key,registry):
        url='https://instamart.in/p/fixture-ABC'
        device=pair(client,headers,['instamart'])
        task=run(client,headers,create(client,headers,retailer='instamart',url=url,expected_pincode='201306'));drive(registry)
        assert poll(client,device,['croma']) is None
        command=poll(client,device,['instamart'])
        endpoint=f"/v1/retailer-browser/{command['id']}/result"
        assert client.post(endpoint,headers=device,json=capture(url=url,observed_at=(datetime.now(UTC)-timedelta(days=1)).isoformat())).status_code==422
        assert client.post(endpoint,headers=device,json=capture(url=url,confirmed_pincode='110001')).status_code==200
        report=finish(client,headers,registry,task)
        assert report['needs_attention'] and not report['usable_evidence']
        assert 'location_unconfirmed' in report['evidence_issues']
        sql('INSERT INTO platform_app_policies(app_id,connector_checks_per_hour) VALUES(%s,1)',(key['app_id'],))
        task2=run(client,headers,create(client,headers));drive(registry)
        assert result(client,headers,task2)['reason_code']=='app_hourly_limit'


@pytest.mark.parametrize('url',[URL+'?token=secret','https://www.croma.com.evil.test/a/p/1','http://www.croma.com/a/p/1','https://www.croma.com/cart','https://user:secret@www.croma.com/a/p/1'])
def test_canonical_product_urls_only(url):
    with pytest.raises(ValueError):BrowserInput(retailer='croma',url=url)


def test_device_expiry_and_saved_job_survive_worker_reconstruction(monkeypatch):
    with setup(monkeypatch) as (client,headers,key,registry):
        device=pair(client,headers);task=run(client,headers,create(client,headers));drive(registry)
        # Every drive creates a new worker pool. Polling cannot create another job.
        sql('UPDATE chore_jobs SET available_at=now() WHERE task_id=%s',(task,));drive(registry)
        assert len(sql('SELECT * FROM retailer_browser_jobs WHERE app_id=%s',(key['app_id'],)))==1
        command=poll(client,device);assert command
        sql("UPDATE retailer_browser_devices SET expires_at=now()-interval '1 second' WHERE app_id=%s",(key['app_id'],))
        assert client.post(f"/v1/retailer-browser/{command['id']}/authorize",headers=device).status_code==401
        sql("UPDATE retailer_browser_jobs SET expires_at=now()-interval '1 second' WHERE task_id=%s",(task,))
        assert finish(client,headers,registry,task)['needs_attention']


def test_browser_rls_hides_other_owner_and_app(monkeypatch):
    from psycopg.sql import SQL, Identifier

    from agent_platform.automation import scope
    with setup(monkeypatch) as (client,headers,key,registry):
        pair(client,headers);task=run(client,headers,create(client,headers));drive(registry)
        row=sql('SELECT tenant_id,owner_id FROM chore_tasks WHERE id=%s',(task,))[0]
        async def verify(pool):
            async with pool.connection() as conn,conn.transaction():
                role=Identifier('browser_rls_'+uuid4().hex[:12])
                await conn.execute(SQL('CREATE ROLE {} NOLOGIN NOSUPERUSER NOBYPASSRLS').format(role))
                await conn.execute(SQL('GRANT SELECT ON retailer_browser_devices,retailer_browser_jobs TO {}').format(role))
                try:
                    await conn.execute(SQL('SET LOCAL ROLE {}').format(role))
                    own={'tenant':row['tenant_id'],'app_id':key['app_id'],'user':str(row['owner_id'])}
                    await scope(conn,own)
                    assert len(await (await conn.execute('SELECT id FROM retailer_browser_jobs')).fetchall())==1
                    assert len(await (await conn.execute('SELECT id FROM retailer_browser_devices')).fetchall())==1
                    for changed in ({**own,'user':str(uuid4())},{**own,'app_id':str(uuid4())},{**own,'tenant':'other'}):
                        await scope(conn,changed)
                        assert not await (await conn.execute('SELECT id FROM retailer_browser_jobs')).fetchall()
                        assert not await (await conn.execute('SELECT id FROM retailer_browser_devices')).fetchall()
                finally:
                    await conn.execute('RESET ROLE')
                    await conn.execute(SQL('DROP OWNED BY {}').format(role))
                    await conn.execute(SQL('DROP ROLE {}').format(role))
        drive(registry,verify)
