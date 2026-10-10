"""Traccar pilot: actual restricted-role queue with simulated phone transport only."""
import asyncio
import json

import httpx
import pytest
from test_sms_notifications import body, drive, get, post, setup

from agent_platform.sms_config import Config, validate_gateway_url
from agent_platform.sms_traccar import check_connection


def traccar(monkeypatch, key, settings):
    settings.update(provider='traccar_local',gateway_url='http://192.168.1.50:8082',allow_lan_http=True,
                    sim_slot=2,test_recipients=['919876543210'])
    monkeypatch.setenv('SMS_FIXTURE_KEY','phone-private')
    monkeypatch.setenv('AI_SMS_APPS',json.dumps({key['app_id']:settings}))
    return Config.model_validate(settings)


def test_traccar_queue_contract_isolation_and_redaction(monkeypatch,caplog):
    with setup(monkeypatch) as (c,h,k,settings), setup(monkeypatch) as (other,oh,_,_):
        traccar(monkeypatch,k,settings)
        data=body(); job=post(c,h,data); seen=[]
        def phone(request):
            seen.append(request)
            assert str(request.url)=='http://192.168.1.50:8082/'
            assert request.headers['authorization']=='phone-private'
            p=json.loads(request.content)
            assert p=={'slot':1, 'to':'+919876543210', 'message':'Your code is 731992.'}
            return httpx.Response(200,content=b'')
        assert drive(httpx.MockTransport(phone))
        result=get(c,h,job['id'])
        assert result['status']=='ACCEPTED' and result['provider']=='traccar_local'
        assert result['provider_id'] is None
        assert post(c,h,data)['id']==job['id'] and not drive() and len(seen)==1
        assert other.get('/v1/notifications/sms/'+job['id'],headers=oh).status_code==404
        assert c.post('/v1/notifications/sms',headers=h,json={**body(),'to':'+919999999999'}).status_code==422
        assert not c.get('/v1/notifications/sms/configuration',headers=h).json()['delivery_confirmation_supported']
        for private in ('phone-private','731992','919876543210'):
            assert private not in json.dumps(result)+caplog.text


@pytest.mark.parametrize('failure',['timeout','connect','conflict','server','unexpected_body','oversized','redirect','bad_auth'])
def test_traccar_failure_handling(monkeypatch,failure):
    with setup(monkeypatch) as (c,h,k,settings):
        traccar(monkeypatch,k,settings);job=post(c,h)
        def phone(request):
            if failure=='timeout':
                raise httpx.ReadTimeout('private failure',request=request)
            if failure=='connect':
                raise httpx.ConnectTimeout('private failure',request=request)
            if failure=='oversized':
                return httpx.Response(202,content=b'x'*64001)
            if failure=='unexpected_body':
                return httpx.Response(200,text='unexpected HTML')
            return httpx.Response({'conflict':409,'server':503,'redirect':302,'bad_auth':401}[failure],
                                  headers={'Location':'https://foreign.example'},text='phone-private')
        drive(httpx.MockTransport(phone))
        result=get(c,h,job['id'])
        expected='RETRY' if failure=='connect' else ('FAILED' if failure in {'redirect','bad_auth'} else 'OUTCOME_UNKNOWN')
        assert result['status']==expected and not drive(httpx.MockTransport(phone))
        assert 'phone-private' not in json.dumps(result)


@pytest.mark.parametrize('url',[
    'http://169.254.169.254:80','http://127.0.0.1:8082','https://public.example:443',
    'http://10.0.0.1:8082/path','http://user:pass@10.0.0.1:8082',
    'http://10.0.0.1:8082?secret=1','http://10.0.0.1:8082#fragment','http://192.168.1.1',
])
def test_traccar_endpoint_restriction(url):
    with pytest.raises(ValueError):
        validate_gateway_url(url,True)


def test_traccar_connection_check_does_not_send(monkeypatch):
    settings=Config(provider='traccar_local',key_env='PHONE_KEY',gateway_url='http://10.0.0.2:8082',allow_lan_http=True,
                    test_recipients=['919876543210'],templates={'test':{'text':'Test'}})
    monkeypatch.setenv('PHONE_KEY','secret')
    def phone(request):
        assert request.method=='POST' and request.url.path=='/'
        assert json.loads(request.content) in ({}, {'slot':0})
        assert 'to' not in json.loads(request.content) and 'message' not in json.loads(request.content)
        assert request.headers['authorization']=='secret'
        return httpx.Response(500,text='Missing phone or message')
    assert asyncio.run(check_connection(settings,transport=httpx.MockTransport(phone)))['connected']
    monkeypatch.delenv('PHONE_KEY')
    assert not asyncio.run(check_connection(settings,transport=httpx.MockTransport(phone)))['connected']
    with pytest.raises(ValueError):
        validate_gateway_url('http://10.0.0.2:8082')


def test_old_sms_fingerprint_stable():
    import hashlib
    settings=Config(templates={'test':{'text':'Test'}})
    old=settings.model_dump(exclude={'limits','testing_no_volume_caps','gateway_url','allow_lan_http','sim_slot','test_recipients'})
    assert settings.fingerprint==hashlib.sha256(json.dumps(old,sort_keys=True).encode()).hexdigest()


def test_retired_smsgate_config_rejected():
    with pytest.raises(ValueError):
        Config(provider='android_local',templates={'test':{'text':'Test'}})


@pytest.mark.parametrize('status,content',[(401,b''),(200,b''),(500,b'Other failure'),(302,b'')])
def test_traccar_probe_does_not_claim_success_for_other_responses(monkeypatch,status,content):
    settings=Config(provider='traccar_local',key_env='PHONE_KEY',gateway_url='http://10.0.0.2:8082',allow_lan_http=True,
                    test_recipients=['919876543210'],templates={'test':{'text':'Test'}})
    monkeypatch.setenv('PHONE_KEY','test-private-key')
    result=asyncio.run(check_connection(settings,transport=httpx.MockTransport(lambda _:httpx.Response(status,content=content))))
    assert not result['connected'] and not result['sms_sent']


@pytest.mark.parametrize('raw,code',[
    ('Permission Denial: android.permission.SEND_SMS','traccar_permission_error'),
    ('getSubscriptionId on null SubscriptionInfo','traccar_sim_error'),
    ('JsonReader expected a name at $.slot','traccar_request_format_error'),
    ('Unrecognized phone failure','traccar_server_error'),
])
def test_diagnostics_persist_without_sensitive_response(monkeypatch,caplog,raw,code):
    with setup(monkeypatch) as (c,h,k,settings):
        traccar(monkeypatch,k,settings);job=post(c,h)
        response=raw+' phone-private 731992 919876543210'
        drive(httpx.MockTransport(lambda _:httpx.Response(500,text=response)))
        result=get(c,h,job['id'])
        assert result['status']=='OUTCOME_UNKNOWN'
        assert result['failure_code']==result['history'][0]['failure_code']==code
        assert result['diagnostic'] and result['history'][0]['diagnostic']
        listing=c.get('/v1/notifications/sms',headers=h).json()
        assert listing['messages'][0]['diagnostic']
        for private in ('phone-private','731992','919876543210',response):
            assert private not in json.dumps(result)+json.dumps(listing)+caplog.text
        assert not drive()


def test_uncapped_pilot_bypasses_volume_only(monkeypatch):
    with setup(monkeypatch,daily=0) as (c,h,k,settings):
        settings['testing_no_volume_caps']=True
        settings['limits'].update(monthly=0,per_minute=1)
        route=traccar(monkeypatch,k,settings)
        assert route.fingerprint==route.model_copy(update={'testing_no_volume_caps':False}).fingerprint
        monkeypatch.setenv('AI_SMS_GLOBAL_LIMITS',json.dumps({'daily':0,'monthly':0,'per_minute':1}))
        first=post(c,h); second=post(c,h)
        assert drive(httpx.MockTransport(lambda _:httpx.Response(200)))
        assert get(c,h,first['id'])['status']=='ACCEPTED'
        assert drive(claim_only=True) is False
        assert get(c,h,second['id'])['failure_code']=='usage_limit_wait'
        cfg=c.get('/v1/notifications/sms/configuration',headers=h).json()
        assert cfg['testing_no_volume_caps']
        assert cfg['limits']==cfg['global_limits']=={'daily':None,'monthly':None,'per_minute':1}
        assert c.get('/v1/notifications/sms/usage',headers=h).json()['limits']['daily'] is None
        # Another app cannot inherit this pilot's exception to platform caps.
        with setup(monkeypatch) as (other,oh,_,_):
            monkeypatch.setenv('AI_SMS_GLOBAL_LIMITS',json.dumps({'daily':0,'monthly':0,'per_minute':1}))
            assert other.post('/v1/notifications/sms',headers=oh,json=body()).status_code==429


def test_uncapped_mode_not_available_to_other_providers():
    for provider in ('mock','smsindiahub'):
        with pytest.raises(ValueError):
            Config(provider=provider,testing_no_volume_caps=True,templates={'test':{'text':'Test'}})


def test_no_send_probe_detects_slot_parser_problem(monkeypatch):
    settings=Config(provider='traccar_local',key_env='PHONE_KEY',gateway_url='http://10.0.0.2:8082',allow_lan_http=True,
                    test_recipients=['919876543210'],templates={'test':{'text':'Test'}})
    monkeypatch.setenv('PHONE_KEY','test-private-key')
    seen=[]
    def phone(request):
        payload=json.loads(request.content);seen.append(payload)
        assert 'to' not in payload and 'message' not in payload
        return httpx.Response(500,text='JsonReader expected a name' if payload else 'Missing phone or message')
    result=asyncio.run(check_connection(settings,transport=httpx.MockTransport(phone)))
    assert result['connected'] and not result['sms_sent'] and not result['slot_parser_supported']
    assert result['code']=='traccar_request_format_error'
    assert seen==[{}, {'slot':0}]


def test_prelogin_challenge_can_queue_pilot_otp_without_exposing_code(monkeypatch):
    from pathlib import Path
    from uuid import uuid4
    with setup(monkeypatch) as (c,h,k,settings):
        settings['templates']=json.loads((Path(__file__).resolve().parents[2]/'examples/sms-templates.android-pilot.json').read_text())
        traccar(monkeypatch,k,settings)
        pending={**h,'X-External-User-ID':'pending-login:'+str(uuid4())}
        request=body();job=post(c,pending,request)
        def phone(req):
            p=json.loads(req.content)
            assert p['message']=='Your verification code is 731992. Do not share it.'
            return httpx.Response(200)
        drive(httpx.MockTransport(phone))
        result=get(c,pending,job['id'])
        assert result['status']=='ACCEPTED'
        assert '731992' not in json.dumps(result)
        assert c.get('/v1/notifications/sms/'+job['id'],headers=h).status_code==404
        assert post(c,pending,request)['id']==job['id']
