"""Signed provider fixtures through HTTP -> durable state -> worker suppression."""
import base64
import hashlib
import hmac
import json
import time
from datetime import UTC, datetime
from uuid import uuid4

import httpx
import pytest
from test_email_notifications import body, drive, get, post, setup
from test_task_agents import sql

from agent_platform.email_delivery import verify

SECRET='whsec_'+base64.b64encode(b'w'*32).decode()


def configure(monkeypatch,app,provider):
    import os
    mapping=json.loads(os.environ.get('AI_EMAIL_WEBHOOKS','{}'))
    mapping[app]={provider:{'key_env':'EMAIL_HOOK_SECRET'}}
    monkeypatch.setenv('AI_EMAIL_WEBHOOKS',json.dumps(mapping))
    monkeypatch.setenv('EMAIL_HOOK_SECRET',SECRET)


def event(provider,identifier,outcome='delivered',recipient='recipient@example.com'):
    if provider=='resend':
        return {'type':'email.'+outcome,'created_at':datetime.now(UTC).isoformat(),
                'data':{'email_id':identifier,'to':[recipient],'subject':'DO NOT STORE OTP 731992'}}
    return {'event':outcome,'message-id':identifier.strip('<>'),'email':recipient,
            'ts_event':int(time.time()),'id':12,'subject':'DO NOT STORE OTP 731992'}


def hook(c,app,provider,payload,*,event_id=None,offset=0,secret=SECRET):
    raw=json.dumps(payload).encode();stamp=str(int(time.time())+offset);id=event_id or 'msg_'+uuid4().hex
    sig=base64.b64encode(hmac.new(base64.b64decode(secret[6:]),id.encode()+b'.'+stamp.encode()+b'.'+raw,hashlib.sha256).digest()).decode()
    headers={'svix-id':id,'svix-timestamp':stamp,'svix-signature':'v1,'+sig} if provider=='resend' else {'Authorization':'Bearer '+secret}
    return c.post('/v1/notifications/email/webhooks/'+app+'/'+provider,headers=headers,content=raw)


def accepted(c,h,provider):
    identifier=str(uuid4()) if provider=='resend' else '<'+uuid4().hex+'@example.invalid>'
    job=post(c,h)
    drive(httpx.MockTransport(lambda _:httpx.Response(200,json={'id' if provider=='resend' else 'messageId':identifier})))
    return job,identifier


@pytest.mark.parametrize('provider',['resend','brevo'])
def test_delivery_dedup_order_suppression_and_isolation(monkeypatch,provider,caplog):
    with setup(monkeypatch,provider) as (c,h,k,_), setup(monkeypatch,provider) as (other,oh,ok,_):
        configure(monkeypatch,k['app_id'],provider);configure(monkeypatch,ok['app_id'],provider)
        job,identifier=accepted(c,h,provider)
        delivered=event(provider,identifier)
        assert hook(c,k['app_id'],provider,delivered,event_id='msg_delivered').status_code==200
        assert hook(c,k['app_id'],provider,delivered,event_id='msg_delivered').status_code==200
        result=get(c,h,job['id']);assert result['status']=='ACCEPTED' and result['delivery_status']=='DELIVERED'
        assert len(result['delivery_events'])==1
        assert 'recipient@example.com' not in json.dumps(result) and '731992' not in json.dumps(result)
        queued=post(c,h)
        complaint=event(provider,identifier,'complained' if provider=='resend' else 'spam')
        assert hook(c,k['app_id'],provider,complaint).status_code==200
        assert get(c,h,queued['id'])['status']=='SUPPRESSED'
        assert get(c,h,queued['id'])['attempts']==0
        assert c.post('/v1/notifications/email',headers=h,json=body()).status_code==409
        # An old delivered event cannot erase a complaint or lift suppression.
        assert hook(c,k['app_id'],provider,delivered).status_code==200
        assert get(c,h,job['id'])['delivery_status']=='COMPLAINED'
        assert c.get('/v1/notifications/email/usage',headers=h).json()['app_suppressed_recipients']==1
        assert post(other,oh)['status']=='QUEUED'
        assert other.get('/v1/notifications/email/'+job['id'],headers=oh).status_code==404
        # Authenticated events on the wrong app do not mutate another app.
        assert hook(c,ok['app_id'],provider,event(provider,identifier,'bounced' if provider=='resend' else 'hard_bounce')).status_code==200
        assert other.get('/v1/notifications/email/usage',headers=oh).json()['app_suppressed_recipients']==0
        assert '731992' not in caplog.text


@pytest.mark.parametrize('provider',['resend','brevo'])
def test_callback_before_send_ack_is_reconciled(monkeypatch,provider):
    with setup(monkeypatch,provider) as (c,h,k,_):
        configure(monkeypatch,k['app_id'],provider)
        job=post(c,h);identifier=str(uuid4())
        def response(_):
            r=hook(c,k['app_id'],provider,event(provider,identifier,'bounced' if provider=='resend' else 'hard_bounce'))
            assert r.status_code==200,r.text
            return httpx.Response(200,json={'id' if provider=='resend' else 'messageId':identifier})
        drive(httpx.MockTransport(response))
        result=get(c,h,job['id'])
        assert result['delivery_status']=='HARD_BOUNCED'
        assert len(result['delivery_events'])==1
        assert c.post('/v1/notifications/email',headers=h,json=body()).status_code==409


def test_authentication_replay_bounds_and_bad_data(monkeypatch):
    with setup(monkeypatch,'resend') as (c,h,k,_):
        configure(monkeypatch,k['app_id'],'resend');job,identifier=accepted(c,h,'resend')
        payload=event('resend',identifier)
        for offset in [-600,600]:
            assert hook(c,k['app_id'],'resend',payload,offset=offset).status_code==401
        bad='whsec_'+base64.b64encode(b'z'*32).decode()
        assert hook(c,k['app_id'],'resend',payload,secret=bad).status_code==401
        assert hook(c,k['app_id'],'brevo',payload).status_code==404
        assert hook(c,k['app_id'],'resend',{'type':'email.delivered'}).status_code==400
        assert c.post('/v1/notifications/email/webhooks/'+k['app_id']+'/resend',content=b'x'*65537).status_code==413
        assert get(c,h,job['id'])['delivery_status']=='UNCONFIRMED'
        # Recipient mismatch cannot suppress or update the matched message.
        assert hook(c,k['app_id'],'resend',event('resend',identifier,'bounced','other@example.com')).status_code==200
        assert get(c,h,job['id'])['delivery_status']=='UNCONFIRMED'


def test_brevo_bearer_batch_and_soft_bounce(monkeypatch):
    with setup(monkeypatch,'brevo') as (c,h,k,_):
        configure(monkeypatch,k['app_id'],'brevo');job,identifier=accepted(c,h,'brevo')
        assert hook(c,k['app_id'],'brevo',event('brevo',identifier),secret='whsec_'+base64.b64encode(b'z'*32).decode()).status_code==401
        soft=event('brevo',identifier,'soft_bounce')
        assert hook(c,k['app_id'],'brevo',[soft,soft]).status_code==200
        assert get(c,h,job['id'])['delivery_status']=='DELAYED'
        assert len(get(c,h,job['id'])['delivery_events'])==1
        assert post(c,h)['status']=='QUEUED'
        assert sql('SELECT count(*) AS n FROM notification_email_suppressions WHERE app_id=%s',(k['app_id'],))[0]['n']==0


def test_svix_published_reference_vector(monkeypatch):
    monkeypatch.setattr(time,'time',lambda:1731705121)
    assert verify('resend','whsec_plJ3nmyCDGBKInavdOK15jsl',
        {'svix-id':'msg_loFOjxBNrRLzqYUf','svix-timestamp':'1731705121',
         'svix-signature':'v1,rAvfW3dJ/X/qxhsaXPOyyCGmRKsaKWcsNccKXlIktD0='},
        b'{"event_type":"ping","data":{"success":true}}')=='msg_loFOjxBNrRLzqYUf'


def test_unsubscribe_stops_uncertain_retry_without_claiming_it_unsent(monkeypatch):
    with setup(monkeypatch,'brevo') as (c,h,k,_):
        configure(monkeypatch,k['app_id'],'brevo');job,identifier=accepted(c,h,'brevo')
        queued=post(c,h)
        sql("UPDATE notification_email_jobs SET may_have_sent=true,status='RETRY' WHERE id=%s",(queued['id'],))
        assert hook(c,k['app_id'],'brevo',event('brevo',identifier,'unsubscribed')).status_code==200
        result=get(c,h,queued['id'])
        assert result['status']=='OUTCOME_UNKNOWN' and result['failure_code']=='recipient_suppressed'
        assert not drive()
        assert get(c,h,job['id'])['delivery_status']=='UNSUBSCRIBED'


def test_worker_checks_suppression_across_providers_and_legacy_jobs(monkeypatch):
    with setup(monkeypatch,'resend') as (c,h,k,settings):
        configure(monkeypatch,k['app_id'],'resend');job,identifier=accepted(c,h,'resend')
        queued=post(c,h)
        sql('UPDATE notification_email_jobs SET recipient_hash=NULL WHERE id=%s',(queued['id'],))
        assert hook(c,k['app_id'],'resend',event('resend',identifier,'bounced')).status_code==200
        assert get(c,h,queued['id'])['status']=='QUEUED'  # Legacy hash must be recovered by worker.
        drive(httpx.MockTransport(lambda _:pytest.fail('Suppressed legacy job reached provider')))
        assert get(c,h,queued['id'])['status']=='SUPPRESSED'
        settings['provider']='brevo'
        monkeypatch.setenv('AI_EMAIL_APPS',json.dumps({k['app_id']:settings}))
        assert c.post('/v1/notifications/email',headers=h,json=body()).status_code==409
        # Existing send request remains idempotently readable after suppression.
        assert get(c,h,job['id'])['delivery_status']=='HARD_BOUNCED'
