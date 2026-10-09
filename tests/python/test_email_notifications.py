"""Real database/worker tests and mocked provider HTTP contracts; no real email sends."""
import asyncio
import base64
import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from uuid import uuid4

import httpx
import pytest
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from test_durable_tasks import database
from test_task_agents import environment, sql

from agent_platform.email_service import claim, tick


@contextmanager
def setup(monkeypatch,provider='mock',daily=100,monthly=1000):
    monkeypatch.setenv('AI_EMAIL_NOTIFICATIONS','1')
    monkeypatch.setenv('AI_NOTIFICATION_ENCRYPTION_KEY_ID','test')
    monkeypatch.setenv('AI_NOTIFICATION_ENCRYPTION_KEYS',json.dumps({'test':base64.b64encode(b'e'*32).decode()}))
    monkeypatch.setenv('AI_NOTIFICATION_HASH_KEY',base64.b64encode(b'h'*32).decode())
    monkeypatch.setenv('EMAIL_FIXTURE_KEY','private-fixture-provider-key')
    monkeypatch.setenv('AI_EMAIL_GLOBAL_LIMITS',json.dumps({'daily':100,'monthly':1000,'per_minute':30}))
    with environment(monkeypatch) as (client,headers,key,_):
        settings={'provider':provider,'from_email':'no-reply@example.com','from_name':'Fixture app',
                  'key_env':'EMAIL_FIXTURE_KEY','allowed_link_hosts':['example.com'],
                  'limits':{'daily':daily,'monthly':monthly,'per_minute':30}}
        mapping=json.loads(__import__('os').environ.get('AI_EMAIL_APPS','{}'))
        mapping[key['app_id']]=settings
        monkeypatch.setenv('AI_EMAIL_APPS',json.dumps(mapping))
        sql("UPDATE platform_service_keys SET scopes=scopes||ARRAY['notifications:read','notifications:send'] WHERE id=%s",(key['key_id'],))
        try:
            yield client,headers,key,settings
        finally:
            for table in ('notification_email_suppressions','notification_email_events','notification_email_attempts','notification_email_jobs'):
                sql(f'DELETE FROM {table} WHERE app_id=%s',(key['app_id'],))


def body(template='otp'):
    variables={'otp':{'code':'731992'},'password_reset':{'url':'https://example.com/reset?token=PRIVATE_RESET'},
               'email_verification':{'url':'https://example.com/verify?token=PRIVATE_VERIFY'},
               'price_drop':{'product':'TV <sale>','price':'₹59,000','url':'https://example.com/product'},
               'system_notification':{'message':'A requested export is ready.'}}[template]
    return {'idempotency_key':str(uuid4()),'to':'recipient@example.com','template':template,'variables':variables}


def post(client,h,data=None):
    response=client.post('/v1/notifications/email',headers=h,json=data or body())
    assert response.status_code==202,response.text
    return response.json()


def drive(transport=None,claim_only=False):
    async def work():
        async with AsyncConnectionPool(database(),open=False,kwargs={'row_factory':dict_row}) as pool:
            return await claim(pool) if claim_only else await tick(pool,transport=transport)
    return asyncio.run(work())


def get(client,h,id):
    response=client.get('/v1/notifications/email/'+id,headers=h)
    assert response.status_code==200,response.text
    return response.json()


@pytest.mark.parametrize('template',['otp','password_reset','email_verification','price_drop','system_notification'])
def test_mock_templates_encrypted_queue_and_no_sensitive_audit(monkeypatch,caplog,template):
    with setup(monkeypatch) as (c,h,_k,_):
        data=body(template);r=post(c,h,data)
        assert r['status']=='QUEUED'
        private=sql('SELECT * FROM notification_email_jobs WHERE id=%s',(r['id'],))[0]
        assert private['payload_cipher'] and data['to'] not in json.dumps(private,default=str)
        for value in data['variables'].values():
            assert value not in json.dumps(private,default=str)
        assert drive()
        result=get(c,h,r['id'])
        assert result['status']=='MOCK_SENT' and result['provider_id'].startswith('mock-')
        assert result['history'][0]['outcome']=='MOCK_SENT'
        assert sql('SELECT payload_cipher FROM notification_email_jobs WHERE id=%s',(r['id'],))[0]['payload_cipher'] is None
        assert data['to'] not in json.dumps(result)
        for value in data['variables'].values():
            assert value not in json.dumps(result) and value not in caplog.text
        usage=c.get('/v1/notifications/email/usage',headers=h).json()
        assert usage['app_requests']['daily']==1 and usage['app_attempts']['daily']==1 and usage['cost'] is None


def test_isolation_idempotency_and_validation_redaction(monkeypatch):
    with setup(monkeypatch) as (a,ha,ka,_),setup(monkeypatch) as (b,hb,_kb,_):
        data=body();r=post(a,ha,data)
        assert post(a,ha,data)['id']==r['id']
        assert a.post('/v1/notifications/email',headers=ha,json={**data,'to':'changed@example.com'}).status_code==409
        assert b.get('/v1/notifications/email/'+r['id'],headers=hb).status_code==404
        assert a.get('/v1/notifications/email/'+r['id'],headers={**ha,'X-External-User-ID':'other-user'}).status_code==404
        assert b.get('/v1/notifications/email',headers=hb).json()['emails']==[]
        invalid={**data,'variables':{'code':'SECRET-INVALID-CODE','password':'DO-NOT-LOG'}}
        response=a.post('/v1/notifications/email',headers=ha,json=invalid)
        assert response.status_code==422 and 'SECRET-INVALID-CODE' not in response.text and 'DO-NOT-LOG' not in response.text
        assert a.post('/v1/notifications/email',headers=ha,json={**data,'category':'marketing'}).status_code==422
        assert a.post('/v1/notifications/email',headers=ha,json={**body('password_reset'),'variables':{'url':'https://foreign.example/reset'}}).status_code==422
        assert a.post('/v1/notifications/email',headers=ha,json={**data,'to':'a@example.com\r\nBcc: b@example.com'}).status_code==422
        sql("UPDATE platform_service_keys SET scopes=ARRAY['tasks:read'] WHERE id=%s",(ka['key_id'],))
        assert a.post('/v1/notifications/email',headers=ha,json=body()).status_code==403


def test_concurrent_dedup_daily_monthly_and_global_limits(monkeypatch):
    with setup(monkeypatch,daily=2,monthly=2) as (c,h,_,_):
        data=body()
        with ThreadPoolExecutor(max_workers=4) as pool:
            responses=list(pool.map(lambda _:c.post('/v1/notifications/email',headers=h,json=data),range(4)))
        assert all(r.status_code==202 for r in responses)
        assert len({r.json()['id'] for r in responses})==1
        post(c,h)
        assert c.post('/v1/notifications/email',headers=h,json=body()).status_code==429
        assert len(c.get('/v1/notifications/email',headers=h).json()['emails'])==2
        # A depleted global cap also blocks a different app.
        monkeypatch.setenv('AI_EMAIL_GLOBAL_LIMITS',json.dumps({'daily':2,'monthly':2,'per_minute':30}))
        with setup(monkeypatch) as (other,oh,_,_):
            monkeypatch.setenv('AI_EMAIL_GLOBAL_LIMITS',json.dumps({'daily':2,'monthly':2,'per_minute':30}))
            assert other.post('/v1/notifications/email',headers=oh,json=body()).status_code==429


@pytest.mark.parametrize('provider',['resend','brevo'])
def test_provider_contract_and_acceptance_not_delivery(monkeypatch,provider):
    with setup(monkeypatch,provider=provider) as (c,h,_,_):
        r=post(c,h)
        def send(request):
            data=json.loads(request.content)
            assert '731992' in (data.get('text') or data.get('textContent'))
            if provider=='resend':
                assert str(request.url)=='https://api.resend.com/emails'
                assert request.headers['authorization']=='Bearer private-fixture-provider-key'
                assert request.headers['idempotency-key']=='notification/'+r['id']
                assert data['to']==['recipient@example.com']
                return httpx.Response(200,json={'id':str(uuid4())})
            assert str(request.url)=='https://api.brevo.com/v3/smtp/email'
            assert request.headers['api-key']=='private-fixture-provider-key'
            assert data['sender']['email']=='no-reply@example.com'
            return httpx.Response(201,json={'messageId':'<fixture@provider.example>'})
        assert drive(httpx.MockTransport(send))
        result=get(c,h,r['id'])
        assert result['status']=='ACCEPTED' and result['provider_id']
        assert result['status']!='DELIVERED'


def test_resend_retries_same_payload_and_key_after_uncertainty(monkeypatch):
    with setup(monkeypatch,provider='resend') as (c,h,_,_):
        r=post(c,h);requests=[]
        def send(request):
            requests.append((request.content,request.headers['idempotency-key']))
            if len(requests)==1:
                raise httpx.ReadTimeout('secret provider text 731992',request=request)
            return httpx.Response(200,json={'id':str(uuid4())})
        transport=httpx.MockTransport(send)
        drive(transport)
        assert get(c,h,r['id'])['status']=='RETRY'
        assert not drive(transport)  # Backoff has not elapsed.
        sql('UPDATE notification_email_jobs SET next_at=now() WHERE id=%s',(r['id'],))
        drive(transport)
        assert get(c,h,r['id'])['status']=='ACCEPTED'
        assert requests[0]==requests[1]
        assert c.get('/v1/notifications/email/usage',headers=h).json()['app_attempts']['daily']==2


def test_brevo_ambiguity_and_worker_crash_never_blindly_retry(monkeypatch):
    with setup(monkeypatch,provider='brevo') as (c,h,_,_):
        r=post(c,h)
        def timeout(request):
            raise httpx.ReadTimeout('private provider response',request=request)
        drive(httpx.MockTransport(timeout))
        assert get(c,h,r['id'])['status']=='OUTCOME_UNKNOWN'
        assert not drive(httpx.MockTransport(timeout))
        r2=post(c,h);claimed=drive(claim_only=True)
        assert claimed and claimed[0]['id']
        sql("UPDATE notification_email_jobs SET lease_until=now()-interval '1 second' WHERE id=%s",(r2['id'],))
        assert not drive()
        assert get(c,h,r2['id'])['status']=='OUTCOME_UNKNOWN'


def test_rate_limit_expiry_and_missing_config_no_fallback(monkeypatch):
    with setup(monkeypatch,provider='brevo') as (c,h,_k,_settings):
        r=post(c,h)
        drive(httpx.MockTransport(lambda _:httpx.Response(429,headers={'retry-after':'90'},json={'message':'OTP 731992'})))
        assert get(c,h,r['id'])['status']=='RETRY'
        assert get(c,h,r['id'])['history'][0]['failure_code']=='provider_rate_limited'
        monkeypatch.delenv('EMAIL_FIXTURE_KEY')
        assert c.post('/v1/notifications/email',headers=h,json=body()).status_code==422
        sql('UPDATE notification_email_jobs SET next_at=now() WHERE id=%s',(r['id'],));drive()
        assert get(c,h,r['id'])['status']=='CONFIGURATION_REQUIRED'
        sql("UPDATE notification_email_jobs SET expires_at=now()-interval '1 second' WHERE id=%s",(r['id'],));drive()
        assert get(c,h,r['id'])['status']=='EXPIRED'
        assert sql('SELECT payload_cipher FROM notification_email_jobs WHERE id=%s',(r['id'],))[0]['payload_cipher'] is None


def test_provider_switch_does_not_redirect_queued_work(monkeypatch):
    with setup(monkeypatch) as (c,h,k,settings):
        r=post(c,h)
        monkeypatch.setenv('AI_EMAIL_APPS',json.dumps({k['app_id']:{**settings,'provider':'resend'}}))
        drive()
        assert get(c,h,r['id'])['status']=='CONFIGURATION_REQUIRED'
        assert get(c,h,r['id'])['attempts']==0


def test_leases_attempt_caps_and_encryption_context(monkeypatch):
    from cryptography.exceptions import InvalidTag

    from agent_platform.email_config import unseal
    with setup(monkeypatch,daily=2) as (c,h,k,settings):
        first=post(c,h);second=post(c,h)
        leased=drive(claim_only=True)
        assert str(leased[0]['id'])==first['id']
        private=sql('SELECT * FROM notification_email_jobs WHERE id=%s',(first['id'],))[0]
        with pytest.raises(InvalidTag):
            unseal(private['payload_cipher'],{**private,'app_id':uuid4()})
        settings['limits']['per_minute']=1
        monkeypatch.setenv('AI_EMAIL_APPS',json.dumps({k['app_id']:settings}))
        drive()
        assert get(c,h,second['id'])['attempts']==0
        assert get(c,h,second['id'])['failure_code']=='usage_limit_wait'
        assert get(c,h,first['id'])['attempts']==1
        monkeypatch.delenv('AI_NOTIFICATION_HASH_KEY')
        assert not c.get('/v1/notifications/email/configuration',headers=h).json()['configured']


def test_dashboard_is_read_only_and_scoped(tmp_path,monkeypatch):
    from test_local_monitor import INTENT, configure
    with setup(monkeypatch) as (c,h,k,_):
        configure(tmp_path,monkeypatch,[('email',k)])
        post(c,h)
        assert c.get('/monitor/email').status_code==200
        assert c.get('/monitor/assets/email.js').status_code==200
        base='/monitor/api/email/notifications/email'
        assert c.get(base,headers=INTENT).status_code==200
        assert c.get(base+'/usage',headers=INTENT).json()['app_requests']['daily']==1
        assert c.post(base,headers=INTENT,json=body()).status_code==404
        assert c.get(base,headers={**INTENT,'Origin':'https://foreign.example'}).status_code==403
