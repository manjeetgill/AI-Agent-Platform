"""Real restricted-role queue tests; all SMS provider requests use mock HTTP."""
import asyncio
import base64
import json
import os
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from uuid import uuid4

import httpx
import pytest
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from test_durable_tasks import database
from test_task_agents import environment, sql

from agent_platform.sms_config import Config, SMSRequest, encoding, render
from agent_platform.sms_service import claim, tick


@contextmanager
def setup(monkeypatch, provider='mock', daily=100, max_segments=1):
    monkeypatch.setenv('AI_SMS_NOTIFICATIONS','1')
    monkeypatch.setenv('AI_NOTIFICATION_ENCRYPTION_KEY_ID','test')
    monkeypatch.setenv('AI_NOTIFICATION_ENCRYPTION_KEYS',json.dumps({'test':base64.b64encode(b'e'*32).decode()}))
    monkeypatch.setenv('AI_NOTIFICATION_HASH_KEY',base64.b64encode(b'h'*32).decode())
    monkeypatch.setenv('SMS_FIXTURE_KEY','private-sms-fixture-key')
    monkeypatch.setenv('AI_SMS_GLOBAL_LIMITS',json.dumps({'daily':1000,'monthly':1000,'per_minute':1000}))
    with environment(monkeypatch) as (client,headers,key,_):
        settings={'provider':provider,'key_env':'SMS_FIXTURE_KEY','sender_id':'FIXTUR','pe_id':'123456789',
                  'templates':{'otp':{'text':'Your code is {code}.','template_id':'987654321','approved':True},
                               'system_notification':{'text':'Account update: {message}','template_id':'987654322','approved':True}},
                  'max_segments':max_segments,'limits':{'daily':daily,'monthly':1000,'per_minute':30}}
        mapping=json.loads(os.environ.get('AI_SMS_APPS','{}')); mapping[key['app_id']]=settings
        monkeypatch.setenv('AI_SMS_APPS',json.dumps(mapping))
        sql("UPDATE platform_service_keys SET scopes=scopes||ARRAY['notifications:read','notifications:send'] WHERE id=%s",(key['key_id'],))
        try:
            yield client,headers,key,settings
        finally:
            for table in ('notification_sms_attempts','notification_sms_jobs'):
                sql(f'DELETE FROM {table} WHERE app_id=%s',(key['app_id'],))


def body():
    return {'idempotency_key':str(uuid4()),'to':'+919876543210','template':'otp','variables':{'code':'731992'}}


def post(c,h,data=None):
    r=c.post('/v1/notifications/sms',headers=h,json=data or body())
    assert r.status_code==202,r.text
    return r.json()


def get(c,h,id):
    r=c.get('/v1/notifications/sms/'+id,headers=h)
    assert r.status_code==200,r.text
    return r.json()


def drive(transport=None,claim_only=False):
    async def run():
        async with AsyncConnectionPool(database(),open=False,kwargs={'row_factory':dict_row}) as pool:
            return await claim(pool) if claim_only else await tick(pool,transport=transport)
    return asyncio.run(run())


def accepted(request):
    payload=json.loads(request.content)
    assert str(request.url)=='https://cloud.smsindiahub.in/api/mt/SendSMS'
    assert payload=={'Account.APIKey':'private-sms-fixture-key','SenderId':'FIXTUR','Message':'Your code is 731992.',
        'Channel':'2','DCS':'0','FlashSMS':'0','Number':'919876543210','Route':'1','PEId':'123456789','TemplateId':'987654321'}
    return httpx.Response(200,json={'ErrorCode':'000','JobId':'JOB_TEST','MessageData':[
        {'Number':'919876543210','MessageId':'MSG_TEST','DeliveryDate':'2026-10-10 12:00:00'}]})


def test_mock_encryption_usage_and_isolation(monkeypatch,caplog):
    with setup(monkeypatch) as (c,h,k,_), setup(monkeypatch) as (other,oh,_,_):
        data=body(); job=post(c,h,data)
        stored=sql('SELECT * FROM notification_sms_jobs WHERE id=%s',(job['id'],))[0]
        assert stored['payload_cipher']
        assert drive()
        result=get(c,h,job['id'])
        assert result['status']=='MOCK_SENT' and result['history'][0]['outcome']=='MOCK_SENT'
        assert result['segments']==1
        assert sql('SELECT payload_cipher FROM notification_sms_jobs WHERE id=%s',(job['id'],))[0]['payload_cipher'] is None
        for value in ('731992','919876543210','private-sms-fixture-key'):
            assert value not in json.dumps(stored,default=str)+json.dumps(result)+caplog.text
        assert other.get('/v1/notifications/sms/'+job['id'],headers=oh).status_code==404
        assert c.get('/v1/notifications/sms/'+job['id'],headers={**h,'X-External-User-ID':'another'}).status_code==404
        assert other.get('/v1/notifications/sms',headers=oh).json()['messages']==[]
        usage=c.get('/v1/notifications/sms/usage',headers=h).json()
        assert usage['app_requests']['daily']==usage['app_attempts']['daily']==1
        assert c.get('/v1/notifications/sms/configuration',headers=h).json()['mock']
        assert len(c.get('/v1/notifications/sms/templates',headers=h).json()['templates'])==2
        sql("UPDATE platform_service_keys SET scopes=ARRAY['tasks:read'] WHERE id=%s",(k['key_id'],))
        assert c.post('/v1/notifications/sms',headers=h,json=body()).status_code==403


def test_concurrent_idempotency_and_conflict(monkeypatch):
    with setup(monkeypatch) as (c,h,_,_):
        data=body()
        with ThreadPoolExecutor(max_workers=4) as executor:
            results=list(executor.map(lambda _:post(c,h,data),range(4)))
        assert len({r['id'] for r in results})==1
        assert c.post('/v1/notifications/sms',headers=h,json={**data,'variables':{'code':'654321'}}).status_code==409
        first=drive(claim_only=True)
        assert first and not drive(claim_only=True)


def test_provider_acceptance_is_not_delivery(monkeypatch):
    with setup(monkeypatch,'smsindiahub') as (c,h,_,_):
        job=post(c,h)
        drive(httpx.MockTransport(accepted))
        result=get(c,h,job['id'])
        assert result['status']=='ACCEPTED'
        assert result['provider_id']=='MSG_TEST' and result['provider_job_id']=='JOB_TEST'
        assert not c.get('/v1/notifications/sms/configuration',headers=h).json()['delivery_confirmation_supported']


@pytest.mark.parametrize('failure',['timeout','server','malformed','wrong_number','redirect','rejected'])
def test_provider_failures_are_redacted_and_never_blindly_retried(monkeypatch,failure):
    with setup(monkeypatch,'smsindiahub') as (c,h,_,_):
        job=post(c,h)
        def respond(request):
            if failure=='timeout':
                raise httpx.ReadTimeout('secret body 731992',request=request)
            if failure=='server':
                return httpx.Response(503,text='private-sms-fixture-key')
            if failure=='redirect':
                return httpx.Response(302,headers={'Location':'https://untrusted.example'})
            if failure=='rejected':
                return httpx.Response(200,json={'ErrorCode':'101','ErrorMessage':'private-sms-fixture-key'})
            if failure=='wrong_number':
                return httpx.Response(200,json={'ErrorCode':'000','JobId':'JOB','MessageData':[{'Number':'919000000000','MessageId':'MSG'}]})
            return httpx.Response(200,json={'ErrorCode':'000'})
        drive(httpx.MockTransport(respond))
        result=get(c,h,job['id'])
        assert result['status']==('FAILED' if failure in {'redirect','rejected'} else 'OUTCOME_UNKNOWN')
        assert not drive(httpx.MockTransport(respond))
        assert 'private-sms-fixture-key' not in json.dumps(result)


def test_safe_retry_backoff_and_cap(monkeypatch):
    with setup(monkeypatch,'smsindiahub') as (c,h,_,_):
        job=post(c,h); requests=[]
        def respond(request):
            requests.append(request.content)
            if len(requests)==1:
                raise httpx.ConnectTimeout('private failure',request=request)
            return accepted(request)
        drive(httpx.MockTransport(respond))
        assert get(c,h,job['id'])['status']=='RETRY'
        assert not drive(httpx.MockTransport(respond))
        sql('UPDATE notification_sms_jobs SET next_at=now() WHERE id=%s',(job['id'],))
        drive(httpx.MockTransport(respond))
        assert get(c,h,job['id'])['status']=='ACCEPTED' and requests[0]==requests[1]
        second=post(c,h)
        transport=httpx.MockTransport(lambda _:httpx.Response(429,headers={'retry-after':'1'}))
        for _ in range(5):
            sql('UPDATE notification_sms_jobs SET next_at=now() WHERE id=%s',(second['id'],))
            drive(transport)
        assert get(c,h,second['id'])['status']=='FAILED'
        assert get(c,h,second['id'])['attempts']==5


def test_worker_crash_expiry_and_changed_configuration(monkeypatch):
    with setup(monkeypatch) as (c,h,k,settings):
        first=post(c,h); drive(claim_only=True)
        sql("UPDATE notification_sms_jobs SET lease_until=now()-interval '1 second' WHERE id=%s",(first['id'],))
        assert not drive()
        assert get(c,h,first['id'])['status']=='OUTCOME_UNKNOWN'
        second=post(c,h)
        monkeypatch.setenv('AI_SMS_APPS',json.dumps({k['app_id']:{**settings,'provider':'smsindiahub'}}))
        drive()
        assert get(c,h,second['id'])['status']=='CONFIGURATION_REQUIRED'
        assert get(c,h,second['id'])['attempts']==0
        sql("UPDATE notification_sms_jobs SET expires_at=now()-interval '1 second' WHERE id=%s",(second['id'],))
        drive()
        assert get(c,h,second['id'])['status']=='EXPIRED'


def test_segment_budgets_span_owners_and_apps(monkeypatch):
    with setup(monkeypatch,daily=2,max_segments=3) as (c,h,k,settings):
        data={**body(),'template':'system_notification','variables':{'message':'ह'*71}}
        first=post(c,h,data)
        assert first['segments']==2
        assert c.post('/v1/notifications/sms',headers={**h,'X-External-User-ID':'other'},json=body()).status_code==429
        settings['limits']['per_minute']=1
        monkeypatch.setenv('AI_SMS_APPS',json.dumps({k['app_id']:settings}))
        drive()
        assert get(c,h,first['id'])['attempts']==0
        settings['limits']['per_minute']=3
        monkeypatch.setenv('AI_SMS_APPS',json.dumps({k['app_id']:settings}))
        sql('UPDATE notification_sms_jobs SET next_at=now() WHERE id=%s',(first['id'],))
        drive()
        assert get(c,h,first['id'])['status']=='MOCK_SENT'
        with setup(monkeypatch) as (b,bh,_,_):
            monkeypatch.setenv('AI_SMS_GLOBAL_LIMITS','{"daily":2,"monthly":1000,"per_minute":10}')
            assert b.post('/v1/notifications/sms',headers=bh,json=body()).status_code==429


def test_no_key_no_fallback_and_input_redaction(monkeypatch):
    with setup(monkeypatch,'smsindiahub') as (c,h,_,_):
        monkeypatch.delenv('SMS_FIXTURE_KEY')
        assert not c.get('/v1/notifications/sms/configuration',headers=h).json()['configured']
        assert c.post('/v1/notifications/sms',headers=h,json=body()).status_code==422
        for data in [dict(body(),to='invalid-private-number'),dict(body(),category='marketing'),
                     dict(body(),variables={'code':'PRIVATE-INPUT','password':'SECRET'}),dict(body(),template='unknown')]:
            r=c.post('/v1/notifications/sms',headers=h,json=data)
            assert r.status_code==422 and 'PRIVATE-INPUT' not in r.text and 'SECRET' not in r.text
        assert c.get('/v1/notifications/sms',headers=h).json()['messages']==[]


def test_encoding_templates_and_live_configuration():
    assert encoding('a'*160)==('0',1) and encoding('a'*161)==('0',2)
    assert encoding('^'*80)==('0',1) and encoding('^'*81)==('0',2)
    assert encoding('ह'*70)==('8',1) and encoding('ह'*71)==('8',2)
    assert encoding('😀'*35)==('8',1) and encoding('😀'*36)==('8',2)
    for template in ['{x.__class__}','{x[foo]}','{code!r}','{code:10000}']:
        with pytest.raises(ValueError):
            Config(templates={'otp':{'text':template}})
    with pytest.raises(ValueError):
        Config(provider='smsindiahub',templates={'otp':{'text':'Code {code}'}})
    config=Config(templates={'otp':{'text':'Code {code}'}})
    with pytest.raises(ValueError):
        render(SMSRequest(**{**body(),'variables':{'code':'x'*200}}),config)


def test_monitor_is_scoped_and_read_only(tmp_path,monkeypatch):
    from test_local_monitor import INTENT, configure
    with setup(monkeypatch) as (c,h,k,_):
        configure(tmp_path,monkeypatch,[('sms',k)])
        post(c,h)
        assert c.get('/monitor/sms').status_code==200
        assert c.get('/monitor/assets/sms.js').status_code==200
        base='/monitor/api/sms/notifications/sms'
        assert c.get(base+'/usage',headers=INTENT).json()['app_requests']['daily']==1
        assert c.post(base,headers=INTENT,json=body()).status_code==404
        assert c.get(base,headers={**INTENT,'Origin':'https://foreign.example'}).status_code==403
