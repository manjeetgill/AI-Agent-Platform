import asyncio
from uuid import uuid4

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from test_meta_onboarding import settings as encryption
from test_task_agents import database, environment, sql
from test_whatsapp import setup

from agent_platform import chakra_onboarding
from agent_platform.meta_worker import managed_config
from agent_platform.service_identity import provision


def business(client, headers, name):
    r=client.post('/v1/businesses',headers=headers,json={'external_id':name,'name':name})
    assert r.status_code==201,r.text
    workspace=r.json()
    r=client.post('/v1/businesses/'+workspace['id']+'/credentials',headers=headers,json={})
    assert r.status_code==201,r.text
    return workspace,{'Authorization':'Bearer '+r.json()['credential'],'X-External-User-ID':'business-owner'}


def business_with_features(client, headers, name, features):
    r=client.post('/v1/businesses',headers=headers,json={'external_id':name,'name':name})
    assert r.status_code==201,r.text
    workspace=r.json()
    r=client.post('/v1/businesses/'+workspace['id']+'/credentials',headers=headers,
                  json={'features':features})
    return workspace,r


def test_business_documents_channels_and_keys_are_isolated(monkeypatch):
    with environment(monkeypatch) as (client,_,key,_):
        headers,_,_=setup(client,key,monkeypatch)
        a,ha=business(client,headers,'business-a')
        b,hb=business(client,headers,'business-b')
        assert client.get('/v1/calling/capabilities',headers=ha).status_code==403
        again=client.post('/v1/businesses',headers=headers,json={'external_id':'business-a','name':'a'}).json()
        assert again['id']==a['id']
        doc=client.post('/v1/knowledge/documents',headers=ha,json={'external_id':'faq','title':'A secret FAQ','text':'Business A only','visibility':'app'}).json()
        assert client.get('/v1/knowledge/documents',headers=hb).json()['documents']==[]
        assert client.delete('/v1/knowledge/documents/'+doc['id'],headers=hb).status_code==404
        stranger={**headers,'X-External-User-ID':'another-owner'}
        assert client.post('/v1/businesses/'+a['id']+'/credentials',headers=stranger,json={}).status_code==404
        rotated=client.post('/v1/businesses/'+a['id']+'/credentials',headers=headers,json={})
        assert rotated.status_code==201
        assert client.get('/v1/whatsapp/channels',headers=ha).status_code==401
        assert client.get('/v1/whatsapp/channels',headers=hb).status_code==200
        # environment owns parent; remove delegated apps as well.
        for table in ('platform_service_keys', 'platform_external_users'):
            sql(f'DELETE FROM {table} WHERE app_id=ANY(%s::uuid[])',([a['app_id'],b['app_id']],))
        sql('DELETE FROM platform_apps WHERE id=ANY(%s::uuid[])',([a['app_id'],b['app_id']],))


def test_business_feature_credentials_require_parent_authority(monkeypatch):
    with environment(monkeypatch) as (client,_,key,_):
        headers,_,_=setup(client,key,monkeypatch)
        workspace, denied = business_with_features(
            client, headers, 'notifications-denied', ['whatsapp_support', 'whatsapp_notifications'])
        assert denied.status_code == 403

        tenant = sql('SELECT tenant_id FROM platform_apps WHERE id=%s', (key['app_id'],))[0]['tenant_id']

        async def parent_key():
            async with AsyncConnectionPool(database(),open=False,kwargs={'row_factory':dict_row}) as pool:
                return await provision(pool,tenant=tenant,organisation='Fixture',app_name='Fixture',
                    app_id=key['app_id'],scopes=['channels:read','channels:write','knowledge:read',
                    'knowledge:write','knowledge:publish','alerts:send','agents:read','agents:write',
                    'agents:run','calling:read','calling:write','telephony:read','telephony:write'])

        elevated=asyncio.run(parent_key())
        delegated={'Authorization':'Bearer '+elevated['credential'],'X-External-User-ID':'operator'}
        issued=client.post('/v1/businesses/'+workspace['id']+'/credentials',headers=delegated,
            json={'features':['whatsapp_support','whatsapp_notifications','whatsapp_calling',
                              'telephone_voice']})
        assert issued.status_code==201,issued.text
        assert {'alerts:send','telephony:write','agents:run'} <= set(issued.json()['scopes'])
        status=client.get('/v1/businesses/'+workspace['id']+'/credentials',headers=delegated)
        assert status.status_code==200,status.text
        assert status.json()['active'] and not status.json()['credential_recoverable']
        assert {'whatsapp_notifications','telephone_voice'} <= set(status.json()['features'])
        child={'Authorization':'Bearer '+issued.json()['credential'],
               'X-External-User-ID':issued.json()['external_user_id']}
        assert client.get('/v1/whatsapp/alerts/capabilities',headers=child).status_code==200
        assert client.get('/v1/calling/capabilities',headers=child).status_code==200
        assert client.get('/v1/telephony/capabilities',headers=child).status_code==200
        readiness=client.get('/v1/integrations/readiness',headers=child)
        assert readiness.status_code==200,readiness.text
        assert readiness.json()['app_id']==workspace['app_id']
        assert {'whatsapp_notifications','whatsapp_calling','telephone_voice'} <= set(
            readiness.json()['features_granted'])
        assert readiness.json()['components']['whatsapp_support']['total']==0
        assert not readiness.json()['provider_checks_performed']
        for table in ('platform_service_keys','platform_external_users'):
            sql(f'DELETE FROM {table} WHERE app_id=%s',(workspace['app_id'],))
        sql('DELETE FROM platform_apps WHERE id=%s',(workspace['app_id'],))


def test_chakra_callback_cannot_claim_number_without_operator_binding(monkeypatch):
    encryption(monkeypatch)
    monkeypatch.setenv('AI_CHAKRA_PARTNER_TOKEN','private-partner-token')
    monkeypatch.setenv('AI_CHAKRA_WEBHOOK_SECRET','s'*40)
    monkeypatch.setenv('AI_CHAKRA_GRAPH_VERSION','v26.0')
    monkeypatch.setenv('AI_PLATFORM_PUBLIC_URL','https://agents.example.test')
    plugin=uuid4(); phone=str(uuid4().int)[:20];waba=str(uuid4().int)[:20]
    calls=[]
    async def fake(method,path,token,body=None):
        calls.append((method,path,body))
        if method=='POST':return {'connectToken':'private-connect-token-'+'x'*40}
        return {'whatsappPhoneNumbers':[{'id':phone,'status':'CONNECTED'}],
                'whatsappBusinessAccounts':[{'id':waba}]}
    monkeypatch.setattr(chakra_onboarding,'provider',fake)
    with environment(monkeypatch) as (client,_,key,_):
        headers,_,_=setup(client,key,monkeypatch)
        a,ha=business(client,headers,'business-a')
        b,hb=business(client,headers,'business-b')
        r=client.post('/v1/whatsapp/chakra/onboarding',headers=ha)
        assert r.status_code==201,r.text
        session=r.json()['id']
        assert calls[0][2]['clientReferenceId']==session
        assert 'pluginId' not in calls[0][2]
        assert client.get('/v1/whatsapp/chakra/onboarding/'+session,headers=hb).status_code==404
        route='/v1/whatsapp/chakra/onboarding/'+session+'/complete'
        assert client.post(route,headers=hb,json={'plugin_id':str(plugin)}).status_code==409
        assert client.post(route,headers=ha,json={'plugin_id':str(plugin)}).json()['state']=='OWNERSHIP_CHECK_REQUIRED'
        assert not sql('SELECT * FROM chakra_customer_connections WHERE phone_id=%s',(phone,))
        async def approve():
            async with AsyncConnectionPool(database(),open=False,kwargs={'row_factory':dict_row}) as pool:
                await chakra_onboarding.bind(pool,session,phone,waba)
        asyncio.run(approve())
        row=sql('SELECT * FROM chakra_customer_connections WHERE phone_id=%s',(phone,))[0]
        assert 'private-partner-token' not in row['config_cipher']
        assert phone in client.get('/v1/whatsapp/setup',headers=ha).json()['phone_ids']
        assert phone not in client.get('/v1/whatsapp/setup',headers=hb).json()['phone_ids']
        doc=client.post('/v1/knowledge/documents',headers=ha,json={'external_id':'faq','title':'FAQ','text':'Business A only','visibility':'app'}).json()
        assert client.post('/v1/whatsapp/channels',headers=hb,json={'name':'Wrong','phone_id':phone,'documents':[doc['id']]}).status_code==404
        assert client.post('/v1/whatsapp/channels',headers=ha,json={'name':'Right','phone_id':phone,'documents':[doc['id']]}).status_code==201
        async def check():
            async with AsyncConnectionPool(database(),open=False,kwargs={'row_factory':dict_row}) as pool:
                return await managed_config(pool,phone,a['app_id'],row['owner_id'])
        assert asyncio.run(check())['plugin_id']==str(plugin)
        route='/v1/whatsapp/chakra/connections/'+session+'/disconnect'
        assert client.post(route,headers=hb).status_code==404
        assert client.post(route,headers=ha).json()['state']=='DISCONNECTED'
        assert phone not in client.get('/v1/whatsapp/setup',headers=ha).json()['phone_ids']
        for table in ('platform_service_keys', 'platform_external_users'):
            sql(f'DELETE FROM {table} WHERE app_id=ANY(%s::uuid[])',([a['app_id'],b['app_id']],))
        sql('DELETE FROM platform_apps WHERE id=ANY(%s::uuid[])',([a['app_id'],b['app_id']],))


def test_saved_signup_resumes_and_prepares_one_clinic_channel(monkeypatch):
    encryption(monkeypatch)
    monkeypatch.setenv('AI_CHAKRA_PARTNER_TOKEN','private-partner-token')
    monkeypatch.setenv('AI_CHAKRA_WEBHOOK_SECRET','s'*40)
    monkeypatch.setenv('AI_CHAKRA_GRAPH_VERSION','v26.0')
    monkeypatch.setenv('AI_PLATFORM_PUBLIC_URL','https://agents.example.test')
    plugin=uuid4();phone=str(uuid4().int)[:20];waba=str(uuid4().int)[:20]
    async def fake(method,path,token,body=None):
        if method=='POST':return {'connectToken':'private-connect-token-'+'x'*40}
        return {'whatsappPhoneNumbers':[{'id':phone,'status':'CONNECTED'}],'whatsappBusinessAccounts':[{'id':waba}]}
    monkeypatch.setattr(chakra_onboarding,'provider',fake)
    with environment(monkeypatch) as (client,_,key,_):
        headers,_,_=setup(client,key,monkeypatch)
        workspace,ha=business(client,headers,'clinic-flow')
        session=client.post('/v1/whatsapp/chakra/onboarding',headers=ha).json()
        route='/v1/whatsapp/chakra/onboarding/'+session['id']
        resumed=client.post(route+'/resume',headers=ha)
        assert resumed.json()['connect_token']==session['connect_token']
        assert resumed.headers['cache-control']=='no-store'
        assert client.post(route+'/channel',headers=ha).status_code==409
        client.post(route+'/complete',headers=ha,json={'plugin_id':str(plugin)})
        assert client.post(route+'/resume',headers=ha).status_code==409
        async def approve():
            async with AsyncConnectionPool(database(),open=False,kwargs={'row_factory':dict_row}) as pool:
                await chakra_onboarding.bind(pool,session['id'],phone,waba)
        asyncio.run(approve())
        requested={'name':'Reach customer assistant','execution_mode':'SAHAYAK','documents':[]}
        channel=client.post(route+'/channel',headers=ha,json=requested)
        assert channel.status_code==200,channel.text
        assert channel.json()['execution_mode']=='SAHAYAK' and channel.json()['enabled'] is False
        assert client.post(route+'/channel',headers=ha,json=requested).json()['id']==channel.json()['id']
        assert sql('SELECT name FROM whatsapp_channels WHERE id=%s',(channel.json()['id'],))[0]['name']=='Reach customer assistant'
        assert client.post(route+'/channel',headers=ha,json={**requested,'name':'Changed'}).status_code==409
        assert client.post(route+'/channel',headers=ha,json={'name':'Other','execution_mode':'RAG','documents':[str(uuid4())]}).status_code==422
        assert client.post(route+'/channel',headers=headers).status_code==409
        for table in ('platform_service_keys','platform_external_users'):
            sql(f'DELETE FROM {table} WHERE app_id=%s',(workspace['app_id'],))
        sql('DELETE FROM platform_apps WHERE id=%s',(workspace['app_id'],))
