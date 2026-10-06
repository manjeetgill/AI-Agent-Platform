"""Real database contracts for customer signup; provider calls explicitly simulated."""
import asyncio
import base64
import hashlib
import hmac
import json
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
import pytest
from cryptography.exceptions import InvalidTag
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from test_task_agents import database, environment, sql
from test_whatsapp import setup

from agent_platform import meta_onboarding, meta_worker
from agent_platform.meta_client import MetaClient, MetaError, config
from agent_platform.meta_crypto import aad, seal, unseal


def settings(monkeypatch):
    for k,v in {'APP_ID':str(uuid4().int)[:20],'CONFIGURATION_ID':'987654321','APP_SECRET':'s'*32,
                'VERIFY_TOKEN':'v'*40,'PUBLIC_URL':'https://signup.example.test',
                'ENCRYPTION_KEYS':json.dumps({'one':base64.b64encode(b'k'*32).decode()}),
                'ENCRYPTION_KEY_ID':'one'}.items():
        monkeypatch.setenv('AI_META_'+k,v)


class Provider:
    def __init__(self):
        self.settings=config();self.registered=True;self.subscription=False;self.callback=False
        self.app_subscription=False;self.revoked=False;self.register_calls=0;self.uncertain=False
    async def exchange(self, code):
        return 'private-business-token'
    async def validate(self, token, waba=None):
        if self.revoked: raise MetaError('REAUTH_REQUIRED')
    async def phone(self, token, waba, phone):
        return {'id':phone,'display_phone_number':'+1234567890','verified_name':'Fixture business',
                'status':'CONNECTED' if self.registered else 'PENDING','platform_type':'CLOUD_API','code_verification_status':'VERIFIED'}
    async def subscribed(self, token, waba):return self.subscription
    async def request(self,path,token=None,method='GET',params=None,payload=None):
        if path.endswith('/subscriptions'):
            if method=='POST':self.app_subscription=True
            return {'data':[{'object':'whatsapp_business_account','active':True,'callback_url':self.settings['public_url']+'/webhooks/meta','fields':['messages','account_update']}] if self.app_subscription else []}
        if path.endswith('/subscribed_apps'):
            self.subscription=method!='DELETE';return {'success':True}
        if path.endswith('/register'):
            self.register_calls+=1
            if self.uncertain:raise MetaError('PROVIDER_UNAVAILABLE',uncertain=True)
            self.registered=True;return {'success':True}
        if method=='POST':self.callback=True
        return {'webhook_configuration':{'phone_number':self.settings['public_url']+'/webhooks/meta' if self.callback else ''}}


def work(provider,health=False):
    async def run():
        async with AsyncConnectionPool(database(),open=False,kwargs={'row_factory':dict_row}) as pool:
            return await (meta_worker.tick_health(pool,provider) if health else meta_worker.tick_onboarding(pool,provider))
    return asyncio.run(run())


def begin(client,headers):
    r=client.post('/v1/whatsapp/onboarding',headers=headers,json={});assert r.status_code==201,r.text
    data=r.json();url=urlsplit(data['connect_url']);root=url.path
    origin={'Origin':'https://signup.example.test'}
    ticket=url.fragment.removeprefix('ticket=')
    result=client.post(root+'/bootstrap',headers={**origin,'X-Signup-Ticket':ticket})
    assert result.status_code==200,result.text
    assert 'HttpOnly' in result.headers['set-cookie'] and 'Secure' in result.headers['set-cookie']
    # TestClient runs on HTTP; explicitly attach the secure cookie to simulate the HTTPS browser.
    return data,root,{**origin,'Cookie':'meta_signup='+ticket}


def assets(client,root,browser,phone,waba):
    response=client.post(root+'/assets',headers=browser,json={'phone_id':phone,'waba_id':waba})
    assert response.status_code==200,response.text


def challenge(client):
    r=client.get('/webhooks/meta',params={'hub.mode':'subscribe','hub.verify_token':'v'*40,'hub.challenge':'challenge-123'})
    assert r.status_code==200 and r.text=='challenge-123'


def test_crypto_owner_binding_rotation(monkeypatch):
    settings(monkeypatch)
    cipher=seal('secret','owner-one')
    assert 'secret' not in cipher and unseal(cipher,'owner-one')=='secret'
    with pytest.raises(InvalidTag):unseal(cipher,'owner-two')
    monkeypatch.setenv('AI_META_ENCRYPTION_KEYS',json.dumps({'one':base64.b64encode(b'k'*32).decode(),'two':base64.b64encode(b'z'*32).decode()}))
    monkeypatch.setenv('AI_META_ENCRYPTION_KEY_ID','two')
    assert unseal(cipher,'owner-one')=='secret'
    assert json.loads(seal('new','owner-one'))['key']=='two'


def test_graph_rejects_wrong_app_and_leaking_errors(monkeypatch):
    settings(monkeypatch)
    async def run():
        transport=httpx.MockTransport(lambda request:httpx.Response(200,json={'data':{'is_valid':True,'app_id':'999999','scopes':['whatsapp_business_management','whatsapp_business_messaging']}}))
        with pytest.raises(MetaError,match='REAUTH_REQUIRED'):await MetaClient(transport=transport).validate('private-token')
        transport=httpx.MockTransport(lambda request:httpx.Response(500,json={'error':{'message':'private-token','code':2}}))
        with pytest.raises(MetaError) as error:await MetaClient(transport=transport).request('123456/register','private-token','POST')
        assert error.value.uncertain and 'private-token' not in str(error.value)
        with pytest.raises(MetaError,match='INVALID_RESOURCE'):await MetaClient(transport=transport).request('https://attacker.example/')
    asyncio.run(run())


def test_customer_signup_ownership_replay_webhook_and_revocation(monkeypatch):
    settings(monkeypatch);provider=Provider();monkeypatch.setattr(meta_onboarding,'MetaClient',lambda:provider)
    with environment(monkeypatch) as (client,_,key,_):
        headers,_legacy,doc=setup(client,key,monkeypatch)
        phone=str(uuid4().int)[:20];waba=str(uuid4().int)[:20]
        data,root,browser=begin(client,headers)
        other={**headers,'X-External-User-ID':'another-user'}
        assert client.get('/v1/whatsapp/onboarding/'+data['id'],headers=other).status_code==404
        assert client.post(root+'/code',headers={**browser,'Origin':'https://evil.test'},json={'code':'secret-code-123'}).status_code==403
        # Asset callback can precede token callback.
        assets(client,root,browser,phone,waba)
        assert client.post(root+'/code',headers=browser,json={'code':'secret-code-123'}).status_code==200
        row=sql('SELECT * FROM meta_signup_sessions WHERE id=%s',(data['id'],))[0]
        assert row['state']=='QUEUED' and 'private-business-token' not in row['token_cipher']
        assert unseal(row['token_cipher'],aad(row))=='private-business-token'
        assert client.post(root+'/code',headers=browser,json={'code':'different-code'}).status_code==409
        assert work(provider)
        assert sql('SELECT state FROM meta_connections WHERE phone_id=%s',(phone,))[0]['state']=='WEBHOOK_PENDING'
        # Actual challenge received + provider readback are both required.
        challenge(client)
        provider.app_subscription=True  # Platform dashboard prerequisite, never a customer task.
        assert client.post(root+'/retry',headers=browser,json={}).status_code==200
        assert work(provider)
        conn=sql('SELECT * FROM meta_connections WHERE phone_id=%s',(phone,))[0]
        assert conn['state']=='CONNECTED' and provider.subscription and provider.callback
        assert client.get('/v1/whatsapp/onboarding/'+data['id'],headers=headers).json()['state']=='CONNECTED'
        channel_path='/v1/whatsapp/onboarding/'+data['id']+'/channel'
        prepared=client.get(channel_path,headers=headers)
        assert prepared.status_code==200 and prepared.json()['phone_id']==phone
        assert prepared.json()['enabled'] is False
        assert client.get(channel_path,headers=other).status_code==409
        assert phone in client.get('/v1/whatsapp/setup',headers=headers).json()['phone_ids']
        assert phone not in client.get('/v1/whatsapp/setup',headers=other).json()['phone_ids']
        assert client.post('/v1/whatsapp/connections/'+str(conn['id'])+'/disconnect',headers=other,json={'confirm':True}).status_code==404
        # Revocation is reflected in connection state and blocks sending.
        provider.revoked=True;sql("UPDATE meta_connections SET checked_at=now()-interval '6 minutes' WHERE id=%s",(conn['id'],))
        assert work(provider,health=True)
        assert sql('SELECT state FROM meta_connections WHERE id=%s',(conn['id'],))[0]['state']=='REAUTH_REQUIRED'
        assert client.get(channel_path,headers=headers).status_code==409
        assert phone not in client.get('/v1/whatsapp/setup',headers=headers).json()['phone_ids']
        assert client.post('/v1/whatsapp/connections/'+str(conn['id'])+'/disconnect',headers=headers,json={'confirm':True}).status_code==200
        assert work(provider,health=True)
        disconnected=sql('SELECT state,token_cipher FROM meta_connections WHERE id=%s',(conn['id'],))[0]
        assert disconnected=={'state':'DISCONNECTED','token_cipher':None}
        # Reconnect the same number reuses its connection and channel, with a fresh grant.
        provider.revoked=False
        _again,again_root,again_browser=begin(client,headers)
        assets(client,again_root,again_browser,phone,waba)
        client.post(again_root+'/code',headers=again_browser,json={'code':'fresh-code-'+uuid4().hex})
        assert work(provider)
        assert sql('SELECT id,state FROM meta_connections WHERE phone_id=%s',(phone,))[0]=={'id':conn['id'],'state':'CONNECTED'}
        assert len(sql('SELECT id FROM whatsapp_channels WHERE phone_id=%s',(phone,)))==1
        # Another authenticated user cannot claim the same WABA, even with a valid-looking callback.
        denied,denied_root,denied_browser=begin(client,other)
        assets(client,denied_root,denied_browser,phone,waba)
        client.post(denied_root+'/code',headers=denied_browser,json={'code':'other-code-'+uuid4().hex})
        assert work(provider)
        assert client.get('/v1/whatsapp/onboarding/'+denied['id'],headers=other).json()['state']=='ASSET_ALREADY_CONNECTED'
        # Signed inbound events route by BOTH WABA and phone; duplicate delivery inserts only once.
        message_id='wamid.'+uuid4().hex
        event={'object':'whatsapp_business_account','entry':[{'id':waba,'changes':[{'field':'messages','value':{'metadata':{'phone_number_id':phone},'messages':[{'id':message_id,'from':'12345678901','timestamp':'1780000000','type':'text','text':{'body':'Hello'}}]}}]}]}
        def deliver(payload):
            raw=json.dumps(payload).encode()
            signature='sha256='+hmac.new(b's'*32,raw,hashlib.sha256).hexdigest()
            return client.post('/webhooks/meta',content=raw,headers={'X-Hub-Signature-256':signature})
        event['entry'][0]['id']='000000000'
        assert deliver(event).status_code==200
        assert not sql('SELECT id FROM whatsapp_messages WHERE provider_id=%s',(message_id,))
        event['entry'][0]['id']=waba
        assert deliver(event).status_code==200
        assert deliver(event).status_code==200
        assert len(sql('SELECT id FROM whatsapp_messages WHERE provider_id=%s',(message_id,)))==1
        sql('DELETE FROM whatsapp_channels WHERE app_id=%s',(key['app_id'],))
        sql('DELETE FROM knowledge_documents WHERE id=%s',(doc['id'],))


def test_unknown_registration_never_repeats_and_webhook_auth(monkeypatch):
    settings(monkeypatch);provider=Provider();provider.registered=False;provider.uncertain=True
    monkeypatch.setattr(meta_onboarding,'MetaClient',lambda:provider)
    with environment(monkeypatch) as (client,_,key,_):
        headers,_legacy,doc=setup(client,key,monkeypatch)
        data,root,browser=begin(client,headers);phone=str(uuid4().int)[:20];waba=str(uuid4().int)[:20]
        client.post(root+'/code',headers=browser,json={'code':'unique-secret-'+uuid4().hex})
        assets(client,root,browser,phone,waba);assert work(provider)
        assert provider.register_calls==1
        client.post(root+'/retry',headers=browser,json={});assert work(provider)
        assert provider.register_calls==1
        assert client.get('/v1/whatsapp/onboarding/'+data['id'],headers=headers).json()['state']=='REGISTRATION_UNCERTAIN'
        assert client.get('/webhooks/meta',params={'hub.mode':'subscribe','hub.verify_token':'bad','hub.challenge':'one'}).status_code==403
        raw=json.dumps({'object':'whatsapp_business_account','entry':[{'id':waba,'changes':[{'field':'messages','value':{'metadata':[]}}]}]}).encode()
        assert client.post('/webhooks/meta',content=raw).status_code==403
        signature='sha256='+hmac.new(b's'*32,raw,hashlib.sha256).hexdigest()
        assert client.post('/webhooks/meta',content=raw,headers={'X-Hub-Signature-256':signature}).status_code==400
        sql('DELETE FROM whatsapp_channels WHERE app_id=%s',(key['app_id'],))
        sql('DELETE FROM knowledge_documents WHERE id=%s',(doc['id'],))


def test_connected_channel_requires_owner_and_live_connection(monkeypatch):
    settings(monkeypatch)
    with environment(monkeypatch) as (client, _, key, _):
        headers, _channel, _ = setup(client, key, monkeypatch)
        data, _, _ = begin(client, headers)
        path = '/v1/whatsapp/onboarding/' + data['id'] + '/channel'
        assert client.get(path, headers=headers).status_code == 409
        # An unverified browser completion can never attach an existing channel.
        other = {**headers, 'X-External-User-ID': 'another-owner'}
        assert client.get(path, headers=other).status_code == 409
        result = client.get('/v1/whatsapp/onboarding/config', headers=headers)
        assert result.json()['provider'] == 'meta'
        assert result.json()['public_customer_approval'] == 'not_verified'


def test_configuration_diagnostics_require_read_scope_and_hide_secrets(monkeypatch):
    settings(monkeypatch)
    monkeypatch.delenv('AI_META_CONFIGURATION_ID')
    with environment(monkeypatch) as (client, _, key, _):
        headers = {'Authorization': 'Bearer ' + key['credential'], 'X-External-User-ID': 'integration-check'}
        sql('UPDATE platform_service_keys SET scopes=%s WHERE id=%s', (['channels:read'], key['key_id']))
        result = client.get('/v1/whatsapp/onboarding/config', headers=headers)
        assert result.status_code == 200
        assert result.json()['missing'] == ['configuration_id']
        assert 's'*32 not in result.text and 'v'*40 not in result.text
        assert client.get('/v1/whatsapp/onboarding/config').status_code == 401
        assert client.post('/v1/whatsapp/onboarding', headers=headers, json={}).status_code == 403
        sql('UPDATE platform_service_keys SET scopes=%s WHERE id=%s', (['tasks:read'], key['key_id']))
        assert client.get('/v1/whatsapp/onboarding/config', headers=headers).status_code == 403
