import asyncio
import base64
import io
import json
import wave

import httpx
import pytest

from agent_platform import calling_audio, sarvam_speech


def test_sarvam_speech_contract_and_explicit_selection(monkeypatch):
    monkeypatch.setenv('AI_CALLING_SPEECH_PROVIDER', 'sarvam')
    monkeypatch.setenv('AI_SARVAM_API_KEY', 'private-fixture')
    raw = io.BytesIO()
    pcm = b'\0\0' * 1600
    with wave.open(raw, 'wb') as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(pcm)
    requests = []
    def respond(request):
        requests.append(request)
        assert request.url.host == 'api.sarvam.ai'
        assert request.headers['api-subscription-key'] == 'private-fixture'
        if request.url.path == '/speech-to-text':
            assert b'turn.wav' in request.content and b'codemix' in request.content
            return httpx.Response(200, json={'transcript':'कल appointment चाहिए'})
        data = json.loads(request.content)
        assert data['language_code'] == 'hi-IN' and data['model'] == 'bulbul:v3'
        return httpx.Response(200, json={'audios':[base64.b64encode(raw.getvalue()).decode()]})
    original = httpx.AsyncClient
    monkeypatch.setattr(sarvam_speech.httpx, 'AsyncClient', lambda **kw: original(transport=httpx.MockTransport(respond), **kw))
    assert calling_audio.speech_key() == 'private-fixture'
    monkeypatch.setenv('AI_CALLING_AUDIO_API_KEY','gemini-only-fixture')
    assert calling_audio.key() == 'gemini-only-fixture'
    assert asyncio.run(calling_audio.transcribe(pcm)) == 'कल appointment चाहिए'
    assert asyncio.run(calling_audio.speak('कल appointment है')) == (pcm, 16000)
    assert len(requests) == 2


def test_invalid_audio_and_provider_error_never_retry(monkeypatch):
    monkeypatch.setenv('AI_CALLING_SPEECH_PROVIDER', 'sarvam')
    monkeypatch.setenv('AI_SARVAM_API_KEY', 'private-fixture')
    monkeypatch.setenv('AI_VOICE_LOCAL_TTS_FALLBACK', '1')
    calls = []
    def fail(request):
        calls.append(request)
        return httpx.Response(429, json={'detail':'private-fixture'})
    original = httpx.AsyncClient
    monkeypatch.setattr(sarvam_speech.httpx, 'AsyncClient', lambda **kw: original(transport=httpx.MockTransport(fail), **kw))
    for pcm in (b'', b'x', b'\0'*384002):
        with pytest.raises(sarvam_speech.SarvamSpeechError):
            asyncio.run(calling_audio.transcribe(pcm))
    assert not calls
    with pytest.raises(sarvam_speech.SarvamSpeechError) as error:
        asyncio.run(calling_audio.speak('Hello'))
    assert 'private-fixture' not in str(error.value)
    assert len(calls) == 1
