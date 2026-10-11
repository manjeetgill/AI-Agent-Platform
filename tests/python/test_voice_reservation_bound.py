import asyncio
import json

import httpx
import pytest
from pydantic import BaseModel

from agent_platform import voice_usage
from agent_platform.gateway import Gateway
from agent_platform.runtime_errors import RunFailure
from agent_platform.voice_config import VoiceGeminiProvider


class Answer(BaseModel):
    answer: str


@pytest.mark.parametrize('timeout',[False,True])
def test_voice_reservation_matches_dispatched_cap_and_retains_unknown(monkeypatch,timeout):
    reservations=[]; settlements=[]; requests=[]
    async def reserve(stage,maximum):
        reservations.append((stage,maximum));return 'reservation'
    async def settle(reservation,units=None,response_id=None):
        settlements.append((reservation,units))
    monkeypatch.setattr(voice_usage,'reserve',reserve)
    monkeypatch.setattr(voice_usage,'settle',settle)
    async def respond(request):
        requests.append(json.loads(request.content))
        if timeout:raise httpx.ReadTimeout('private',request=request)
        return httpx.Response(200,json={'choices':[{'message':{'content':'{"answer":"nine to five"}'}}],
                                     'usage':{'total_tokens':123},'id':'fixture'})
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            gateway=Gateway(VoiceGeminiProvider('gemini','fixture'),client=client,max_attempts=1,
                allow_fallback=False,allow_chain=False,model_override='gemini-3.1-flash-lite')
            with voice_usage.using(None,'fixture'):
                try:await gateway.structured('research','Use supplied facts',{'question':'Hours?'},Answer,lambda x:x)
                except Exception:
                    if not timeout:raise
    asyncio.run(run())
    assert len(requests)==1 and requests[0]['max_tokens']==1600
    expected=len(json.dumps(requests[0]).encode())+1600+1024
    assert reservations==[('reasoning',expected)]
    assert settlements==[('reservation',None if timeout else 123)]


@pytest.mark.parametrize('failure,code,diagnostic',[
    (503,'PROVIDER_BACKPRESSURE','category=http status=503'),
    (401,'PROVIDER_AUTH','category=http status=401'),
    (400,'MODEL_UNAVAILABLE','category=http status=400'),
    ('transport','MODEL_UNAVAILABLE','category=transport'),
    ('timeout','MODEL_TIMEOUT','category=timeout'),
])
def test_provider_failure_diagnostics_are_safe_and_single_attempt(caplog,failure,code,diagnostic):
    requests=[]
    secret='private-upstream-value'
    async def respond(request):
        requests.append(request)
        if failure=='transport':raise httpx.ConnectError(secret,request=request)
        if failure=='timeout':raise httpx.ReadTimeout(secret,request=request)
        return httpx.Response(failure,text=secret)
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            gateway=Gateway(VoiceGeminiProvider('gemini',secret),client=client,max_attempts=1,
                allow_fallback=False,allow_chain=False,model_override='gemini-3.1-flash-lite')
            with pytest.raises(RunFailure) as error:
                await gateway.structured('research','Use supplied facts',{'question':'Hours?'},Answer,lambda x:x)
            assert error.value.code==code
    asyncio.run(run())
    assert len(requests)==1
    assert diagnostic in caplog.text
    assert secret not in caplog.text
