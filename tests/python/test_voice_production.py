import asyncio
import json
from uuid import uuid4

import httpx
import pytest
from test_task_agents import environment, sql
from test_web_voice import Media, create

from agent_platform import voice_config, voice_stream, voice_usage, web_voice
from agent_platform.gateway import Gateway, NVIDIAProvider
from agent_platform.knowledge import Generated
from agent_platform.runtime_errors import RunFailure


def test_paid_usage_reservation_unknown_hold_and_isolation(monkeypatch):
    monkeypatch.setattr(web_voice, 'ready', lambda: True)
    monkeypatch.setattr(web_voice.calling_media, 'Session', Media)
    with environment(monkeypatch) as (client, headers, key, _):
        agent, _ = create(client, headers, key)
        result = client.post('/v1/voice/agents/'+agent['id']+'/sessions',headers=headers,json={
            'sdp':'v=0\r\n'+'fixture '*5,'request_key':uuid4().hex,'audio_consent':True})
        assert result.status_code==201, result.text
        session = result.json()['id']
        operator_sql("INSERT INTO voice_spend_limits(app_id,currency,daily,monthly) VALUES(%s,'INR',2,5)",(key['app_id'],))
        operator_sql("INSERT INTO voice_rate_cards(provider,model,stage,currency,per_unit,reference,expires_at) VALUES('gemini','billing-fixture','asr','INR',1,'synthetic rate, not real pricing',now()+interval '1 day') ON CONFLICT(provider,model,stage) DO UPDATE SET expires_at=excluded.expires_at")

        async def verify():
            from agent_platform.db_scope import BoundPool
            from agent_platform.service_identity import authenticate
            base=client.app.state.repository.pool
            claims=await authenticate(base,headers['Authorization'],headers['X-External-User-ID'],None)
            pool=BoundPool(base,claims)
            with voice_config.using({'asr':'gemini','asr_model':'billing-fixture'}), voice_usage.using(pool, session):
                async def attempt():
                    try:
                        return await voice_usage.reserve('asr',1)
                    except ValueError:
                        return None
                attempts=await asyncio.gather(*(attempt() for _ in range(3)))
                assert sum(x is not None for x in attempts)==2
                first,second=[x for x in attempts if x]
                await voice_usage.settle(first,.5,'fixture-response')
                await voice_usage.settle(first,0)  # immutable settlement
                await voice_usage.settle(second)  # unknown retains 1 INR
                with pytest.raises(ValueError,match='allowance'):
                    await voice_usage.reserve('asr',1)
                with pytest.raises(ValueError,match='rates'):
                    await voice_usage.reserve('tts',1)
                local={'asr':'local','tts':'local','reasoning':'local'}
                with voice_config.using(local):
                    free=await voice_usage.reserve('tts',10)
                    await voice_usage.settle(free,10)
        client.portal.call(verify)
        ledger=sql('SELECT * FROM voice_usage_ledger WHERE session_id=%s ORDER BY created_at',(session,))
        assert [r['state'] for r in ledger]==['DONE','UNKNOWN','DONE']
        assert float(ledger[0]['amount'])==.5
        assert ledger[-1]['amount']==0
        usage=client.get('/v1/voice/usage',headers=headers).json()['accounting']
        assert sum(float(s['held_amount']) for s in usage['stages'])==1
        with environment(monkeypatch) as (_, other, _, _):
            assert client.get('/v1/voice/usage',headers=other).json()['accounting']['stages']==[]
        operator_sql("DELETE FROM voice_rate_cards WHERE model='billing-fixture'")


def test_sarvam_reservations_use_transport_models_not_generic_defaults(monkeypatch):
    monkeypatch.setattr(web_voice, 'ready', lambda: True)
    monkeypatch.setattr(web_voice.calling_media, 'Session', Media)
    with environment(monkeypatch) as (client, headers, key, _):
        agent, _ = create(client, headers, key)
        session = client.post('/v1/voice/agents/'+agent['id']+'/sessions', headers=headers,
            json={'sdp':'v=0\r\n'+'fixture '*5,'request_key':uuid4().hex,'audio_consent':True}).json()['id']
        operator_sql("INSERT INTO voice_spend_limits(app_id,currency,daily,monthly) VALUES(%s,'INR',2,5)", (key['app_id'],))
        for stage, model in [('asr', 'saaras:v3'), ('tts', 'bulbul:v3')]:
            operator_sql("INSERT INTO voice_rate_cards(provider,model,stage,currency,per_unit,reference,expires_at) VALUES('sarvam',%s,%s,'INR',.01,'fixture',now()+interval '1 day') ON CONFLICT(provider,model,stage) DO UPDATE SET expires_at=excluded.expires_at", (model, stage))
        async def verify():
            from agent_platform.db_scope import BoundPool
            from agent_platform.service_identity import authenticate
            base = client.app.state.repository.pool
            claims = await authenticate(base, headers['Authorization'], headers['X-External-User-ID'], None)
            with voice_config.using({'asr':'sarvam','tts':'sarvam'}), voice_usage.using(BoundPool(base, claims), session):
                for stage in ['asr', 'tts']:
                    await voice_usage.settle(await voice_usage.reserve(stage, 1), 1)
        client.portal.call(verify)
        rows = sql('SELECT provider,model FROM voice_usage_ledger WHERE session_id=%s ORDER BY created_at', (session,))
        assert [(r['provider'], r['model']) for r in rows] == [('sarvam', 'saaras:v3'), ('sarvam', 'bulbul:v3')]


class Stream(httpx.AsyncByteStream):
    def __init__(self, lines):
        self.lines=lines
    async def __aiter__(self):
        for line in self.lines:
            yield line
            await asyncio.sleep(0)


def packets(records, terminate=True):
    chunks=[]
    content=json.dumps({'claims':[r for r in records if 'text' in r], 'done':True})
    for part in [content[:9],content[9:]]:
        chunks.append(('data: '+json.dumps({'choices':[{'index':0,'delta':{'content':part}}]})+'\n\n').encode())
    chunks.append(b'data: {"id":"fixture-id","choices":[],"usage":{"total_tokens":34}}\n\n')
    if terminate:
        chunks.append(b'data: [DONE]\n\n')
    return chunks


def test_streamed_quotations_validate_before_speech_and_close(monkeypatch):
    async def run(records, terminate=True):
        spoken=[]
        async def handler(request):
            assert json.loads(request.content)['stream'] is True
            return httpx.Response(200,headers={'content-type':'text/event-stream'},stream=Stream(packets(records,terminate)))
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            gateway=Gateway(NVIDIAProvider('fixture'),client=client,model_override='fixture',max_attempts=1)
            async def speak(text): spoken.append(text)
            async def current(claim): assert claim.sources==['source-1']
            gateway.on_claim=speak
            gateway.verify_stream_claim=current
            try:
                result=await gateway.structured('research','',{'passages':[{'id':'source-1','text':'Open nine to five.','title':'Internal storage metadata'}]},Generated,lambda x:x)
            except RunFailure:
                result=None
            return result,spoken
    result,spoken=asyncio.run(run([{'text':'Open nine to five.','sources':['source-1']},{'done':True}]))
    assert result[1]==34 and spoken==['Open nine to five.']
    result,spoken=asyncio.run(run([{'text':'Open twenty four hours.','sources':['source-1']},{'done':True}]))
    assert result is None and spoken==[]
    result,spoken=asyncio.run(run([{'text':'Open nine to five.','sources':['foreign']},{'done':True}]))
    assert result is None and spoken==[]
    assert asyncio.run(run([{'done':True}],False))[0] is None


def test_sse_limits_and_tool_calls_fail_closed():
    async def run():
        async def handler(request):
            return httpx.Response(200,headers={'content-type':'text/event-stream'},stream=Stream([
                b'data: {"choices":[{"delta":{"tool_calls":[{}]}}]}\n\n']))
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(ValueError,match='tools'):
                async for _ in voice_stream.content_chunks(client,'https://fixture.invalid',{}, {}, {}):
                    pass
    asyncio.run(run())


def test_local_voice_selects_source_text_and_rejects_unknown_or_incomplete_choices():
    async def run(content, complete=True, revoked=False):
        spoken=[]
        async def handler(request):
            payload=json.loads(request.content)
            assert payload['temperature']==0
            assert json.loads(payload['messages'][1]['content'])['quotations']==[
                {'id':'quote_1','text':'Open nine to five.'}]
            lines=[('data: '+json.dumps({'choices':[{'index':0,'delta':{'content':content}}]})+'\n\n').encode()]
            if complete:
                lines += [b'data: {"usage":{"total_tokens":12},"choices":[]}\n\n', b'data: [DONE]\n\n']
            return httpx.Response(200,headers={'content-type':'text/event-stream'},stream=Stream(lines))
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider=NVIDIAProvider('fixture')
            provider.name='local'
            gateway=Gateway(provider,client=client,model_override='fixture',max_attempts=1)
            async def speak(text): spoken.append(text)
            async def current(claim):
                assert claim.sources==['permitted-source']
                if revoked: raise ValueError('Source no longer published')
            gateway.on_claim=speak
            gateway.verify_stream_claim=current
            try:
                result=await gateway.structured('research','',{'question':'Hours?', 'passages':[
                    {'id':'permitted-source','text':'Open nine to five.','title':'Not sent to model'}]},Generated,lambda x:x)
            except RunFailure:
                result=None
            return result,spoken
    result,spoken=asyncio.run(run('{"selection":"quote_1"}'))
    assert result[1]==12 and spoken==['Open nine to five.']
    result,spoken=asyncio.run(run('{"selection":"none"}'))
    assert result[0].claims==[] and result[0].missing and spoken==[]
    for raw in ['{"selection":"foreign"}', '{"selection":"quote_1","selection":"none"}',
                '{"selection":"quote_1","text":"Invented fact"}', '[]']:
        assert asyncio.run(run(raw))==(None,[])
    assert asyncio.run(run('{"selection":"quote_1"}',complete=False))==(None,[])
    assert asyncio.run(run('{"selection":"quote_1"}',revoked=True))==(None,[])


def test_clock_pronunciation_preserves_values():
    from agent_platform.calling_audio import spoken_clock
    assert spoken_clock('nine in the morning to five in the evening') == "nine o'clock in the morning to five o'clock in the evening"
    assert spoken_clock('9:30 AM') == '9:30 AM'


def test_vad_handles_fragmented_pcm_and_rejects_steady_noise():
    import random
    from array import array

    pytest.importorskip('webrtcvad')
    from agent_platform.voice_vad import SpeechDetector
    detector=SpeechDetector(300)
    assert not any(detector.feed(b'\0\0'*157) for _ in range(30))
    randomizer=random.Random(1234)
    decisions=[]
    for _ in range(100):
        frame=array('h',[randomizer.randint(-600,600) for _ in range(320)]).tobytes()
        decisions.append(detector.feed(frame))
    assert sum(decisions[-50:]) < 5
    assert len(detector.pending)<640


def operator_sql(query, params=()):
    import os

    import psycopg
    with psycopg.connect(os.environ['AGENT_TEST_MIGRATION_URL']) as conn:
        conn.execute(query, params)


def test_runtime_cannot_raise_voice_prices_or_spending_limits(monkeypatch):
    import os

    import psycopg

    from agent_platform.db_scope import bound_identity
    from agent_platform.service_identity import authenticate
    with environment(monkeypatch) as (client, headers, key, _):
        async def verify():
            pool=client.app.state.repository.pool
            claims=await authenticate(pool,headers['Authorization'],headers['X-External-User-ID'],None)
            with bound_identity(claims):
                async with pool.connection() as conn:
                    with pytest.raises(psycopg.errors.InsufficientPrivilege):
                        async with conn.transaction():
                            await conn.execute("INSERT INTO voice_spend_limits(app_id,currency,daily,monthly) VALUES(%s,'INR',100,100)",(key['app_id'],))
        client.portal.call(verify)
        with psycopg.connect(os.environ['AGENT_TEST_DATABASE_URL']) as conn, pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("INSERT INTO voice_rate_cards(provider,model,stage,currency,per_unit,reference,expires_at) VALUES('fixture','unauthorized','asr','INR',0,'invalid',now()+interval '1 day')")
