import asyncio
from uuid import uuid4

import httpx
import pytest
from test_task_agents import environment
from test_web_voice import Media, create

from agent_platform import sarvam_speech, voice_events, web_voice
from agent_platform.voice_failures import VoiceUsageError, failure_code


@pytest.mark.parametrize('status,code', [(401,'PROVIDER_AUTH'),(403,'PROVIDER_AUTH'),(429,'PROVIDER_QUOTA'),(500,'PROVIDER_ERROR')])
def test_provider_codes_survive_speech_wrapper(monkeypatch,status,code):
    original=httpx.AsyncClient
    async def response(request):
        return httpx.Response(status,json={'private':'never show this'})
    monkeypatch.setenv('AI_SARVAM_API_KEY','private-test-key')
    monkeypatch.setattr(sarvam_speech.httpx,'AsyncClient',lambda **kwargs:original(transport=httpx.MockTransport(response),**kwargs))
    with pytest.raises(sarvam_speech.SarvamSpeechError) as error:
        asyncio.run(sarvam_speech.request('speech-to-text'))
    assert failure_code(error.value)==code
    assert 'private' not in str(error.value)


def test_unknown_exception_codes_are_not_exposed():
    error=ValueError('secret')
    error.code='secret'
    assert failure_code(error)=='FAILED'
    assert failure_code(VoiceUsageError('SPENDING_LIMIT','allowance'))=='SPENDING_LIMIT'
    assert failure_code(TimeoutError('private'))=='TIMEOUT'


def test_session_status_has_safe_failure_even_after_end(monkeypatch):
    monkeypatch.setattr(web_voice,'ready',lambda:True)
    monkeypatch.setattr(web_voice.calling_media,'Session',Media)
    with environment(monkeypatch) as (client,headers,key,_):
        agent,_=create(client,headers,key)
        response=client.post('/v1/voice/agents/'+agent['id']+'/sessions',headers=headers,json={
            'sdp':'v=0\r\n'+'fixture '*5,'request_key':uuid4().hex,'audio_consent':True})
        assert response.status_code==201,response.text
        session=response.json()['id']
        async def record():
            from agent_platform.db_scope import BoundPool
            from agent_platform.service_identity import authenticate
            base=client.app.state.repository.pool
            claims=await authenticate(base,headers['Authorization'],headers['X-External-User-ID'],None)
            await voice_events.record(BoundPool(base,claims),session,'turn_failed',{
                'stage':'reasoning','outcome':'SPENDING_LIMIT','provider':'private-debug-value'})
        client.portal.call(record)
        client.post('/v1/voice/sessions/'+session+'/end',headers=headers)
        result=client.get('/v1/voice/sessions/'+session,headers=headers).json()
        assert result['last_failure']=={'stage':'reasoning','outcome':'SPENDING_LIMIT'}
        assert 'private-debug' not in str(result)
        stranger={**headers,'X-External-User-ID':'stranger'}
        assert client.get('/v1/voice/sessions/'+session,headers=stranger).status_code==404
