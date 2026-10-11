import json
from uuid import uuid4

from fastapi.testclient import TestClient
from test_task_agents import environment, sql
from test_web_voice import Media, create

from agent_platform import media_client, media_contract, media_service, web_voice


def test_private_media_contract_and_control_isolation(monkeypatch):
    monkeypatch.setenv('AI_VOICE_MEDIA_SECRET', 'test-only-media-secret-'*4)
    monkeypatch.setenv('AI_VOICE_MEDIA_URL', 'http://127.0.0.1:4162')
    monkeypatch.setattr(web_voice, 'ready', lambda: True)
    monkeypatch.setattr(media_service, 'audio_ready', lambda: True)
    monkeypatch.setattr(web_voice.calling_media, 'Session', Media)
    with environment(monkeypatch) as (api, headers, key, _), TestClient(media_service.create_media_app()) as media:
        seen=[]
        async def send(path,payload):
            raw=json.dumps(payload,default=str,separators=(',',':')).encode()
            auth=media_contract.headers('POST',path,raw)
            seen.append((path,raw,auth))
            response=media.post(path,content=raw,headers=auth)
            response.raise_for_status()
            return response.json()
        monkeypatch.setattr(media_client,'send',send)
        agent,_=create(api,headers,key)
        offer={'sdp':'v=0\r\n'+'fixture '*5,'request_key':uuid4().hex,'audio_consent':True}
        result=api.post('/v1/voice/agents/'+agent['id']+'/sessions',headers=headers,json=offer)
        assert result.status_code==201,result.text
        ident=result.json()['id']
        row=sql('SELECT * FROM web_voice_sessions WHERE id=%s',(ident,))[0]
        assert row['media_remote'] and row['media_claimed_at']
        assert str(row['runtime_id'])==str(media.app.state.runtime.id)
        path,raw,auth=seen[-1]
        assert media.post(path,content=raw).status_code==401
        assert media.post(path,content=raw,headers=auth).status_code==409  # exact replay
        assert media.post(path,content=raw+b' ',headers=auth).status_code==401  # changed body
        stale={**auth,'X-Media-Time':'1'}
        assert media.post(path,content=raw,headers=stale).status_code==401
        assert media.post(path,content=raw,headers=media_contract.headers('POST',path,raw)).status_code==404 # no longer claimable
        foreign=json.loads(raw);foreign['app_id']=str(uuid4());bad=json.dumps(foreign).encode()
        assert media.post(path,content=bad,headers=media_contract.headers('POST',path,bad)).status_code==404
        assert media.post(path,content=b'x'*70001).status_code==413
        status=api.get('/v1/voice/sessions/'+ident,headers=headers)
        assert status.status_code==200 and status.json()['state']=='ACTIVE'
        assert api.post('/v1/voice/sessions/'+ident+'/end',headers={**headers,'X-External-User-ID':'foreign'},json={}).status_code==404
        assert api.post('/v1/voice/sessions/'+ident+'/end',headers=headers,json={}).json()['media_stop_requested']
        assert sql('SELECT state FROM web_voice_sessions WHERE id=%s',(ident,))[0]['state']=='ENDED'
        drain_path='/internal/v1/drain'
        drain=media.post(drain_path,content=b'{}',headers=media_contract.headers('POST',drain_path,b'{}'))
        assert drain.json()['draining']
        readiness='/internal/v1/readiness'
        assert media.post(readiness,content=b'{}',headers=media_contract.headers('POST',readiness,b'{}')).status_code==503


def test_media_outage_does_not_reserve_a_call_or_break_api(monkeypatch):
    import httpx
    monkeypatch.setenv('AI_VOICE_MEDIA_URL','http://127.0.0.1:4162')
    monkeypatch.setenv('AI_VOICE_MEDIA_SECRET','fixture-'*8)
    async def outage(*args): raise httpx.ConnectError('fixture outage')
    monkeypatch.setattr(media_client,'send',outage)
    with environment(monkeypatch) as (api,headers,key,_):
        agent,_=create(api,headers,key)
        result=api.post('/v1/voice/agents/'+agent['id']+'/sessions',headers=headers,json={
            'sdp':'v=0\r\n'+'fixture '*5,'request_key':uuid4().hex,'audio_consent':True})
        assert result.status_code==503
        assert api.get('/v1/voice/agents',headers=headers).status_code==200
        assert sql('SELECT * FROM web_voice_sessions WHERE app_id=%s',(key['app_id'],))==[]


def test_media_origin_and_key_fail_closed(monkeypatch):
    monkeypatch.setenv('AI_VOICE_MEDIA_URL','http://media.example.com')
    monkeypatch.setenv('AI_VOICE_MEDIA_SECRET','fixture-'*8)
    assert not media_client.configured()
    monkeypatch.setenv('AI_VOICE_MEDIA_URL','https://media.example.com')
    assert media_client.configured()
    monkeypatch.setenv('AI_VOICE_MEDIA_SECRET','short')
    assert not media_client.configured()


def test_uncertain_dispatch_and_stale_media_are_not_replayed(monkeypatch):
    import httpx
    monkeypatch.setenv('AI_VOICE_MEDIA_URL','http://127.0.0.1:4162')
    monkeypatch.setenv('AI_VOICE_MEDIA_SECRET','fixture-'*8)
    monkeypatch.setattr(web_voice,'ready',lambda:True)
    dispatched=[]
    async def send(path,payload):
        if path.endswith('/readiness'):
            return {'status':'ready'}
        dispatched.append(payload['session_id'])
        raise httpx.ReadTimeout('unknown')
    monkeypatch.setattr(media_client,'send',send)
    with environment(monkeypatch) as (api,headers,key,_):
        agent,_=create(api,headers,key)
        body={'sdp':'v=0\r\n'+'fixture '*5,'request_key':uuid4().hex,'audio_consent':True}
        path='/v1/voice/agents/'+agent['id']+'/sessions'
        assert api.post(path,headers=headers,json=body).status_code==502
        assert api.post(path,headers=headers,json=body).status_code==409
        assert len(dispatched)==1
        ident=str(dispatched[0])
        assert sql('SELECT state FROM web_voice_sessions WHERE id=%s',(ident,))[0]['state']=='UNKNOWN'
        sql("UPDATE web_voice_sessions SET state='ACTIVE',media_heartbeat=now()-interval '11 seconds',media_connected=true WHERE id=%s",(ident,))
        result=api.get('/v1/voice/sessions/'+ident,headers=headers).json()
        assert result['state']=='UNKNOWN' and not result['audio_connected']
