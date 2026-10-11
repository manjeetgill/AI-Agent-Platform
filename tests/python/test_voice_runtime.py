import asyncio
from uuid import uuid4

import pytest
from test_task_agents import environment, sql
from test_web_voice import Media, create

from agent_platform import calling_audio, calling_media, voice_config, web_voice


def test_provider_config_is_task_local_and_does_not_fall_back(monkeypatch):
    monkeypatch.setenv('AI_CALLING_AUDIO_API_KEY', 'private-gemini-fixture')
    monkeypatch.setenv('NVIDIA_API_KEY', 'private-nvidia-fixture')

    async def check(provider):
        with voice_config.using({'reasoning': provider, 'model': provider + '-model', 'asr': 'local', 'tts': 'local'}):
            await asyncio.sleep(0)
            gateway = web_voice.voice_gateway()
            assert gateway.provider.name == provider
            assert gateway.model_for('research') == provider + '-model'
            assert not gateway.allow_fallback
            assert calling_audio.speech_provider() == 'local'
        assert voice_config.active.get() is None

    async def run():
        await asyncio.gather(check('gemini'), check('nvidia'))
    asyncio.run(run())
    monkeypatch.delenv('AI_VOICE_TOGETHER_API_KEY', raising=False)
    with voice_config.using({'reasoning': 'together'}), pytest.raises(ValueError):
        web_voice.voice_gateway()
    monkeypatch.setenv('AI_VOICE_LOCAL_MODEL_URL', 'https://example.com/steal')
    with voice_config.using({'reasoning': 'local'}), pytest.raises(ValueError):
        web_voice.voice_gateway()


def test_versions_sessions_and_transcripts_are_scoped(monkeypatch):
    monkeypatch.setattr(web_voice, 'ready', lambda: True)
    captured = []

    class Capture(Media):
        def __init__(self, *args):
            super().__init__(*args)
            captured.append(self)

    monkeypatch.setattr(web_voice.calling_media, 'Session', Capture)
    with environment(monkeypatch) as (c, h, key, _):
        agent, _ = create(c, h, key)
        path = '/v1/voice/agents/' + agent['id']
        offer = {'sdp': 'v=0\r\n' + 'fixture ' * 5, 'request_key': uuid4().hex,
                 'audio_consent': True, 'transcript_consent': True}
        session = c.post(path + '/sessions', headers=h, json=offer).json()['id']
        config = {'reasoning': 'local', 'model': 'qwen2.5:3b', 'asr': 'local', 'tts': 'local'}
        result = c.post(path + '/configuration', headers=h, json={'expected_version': 1, 'config': config})
        assert result.status_code == 200, result.text
        assert result.json()['version'] == 2
        assert c.post(path + '/configuration', headers=h, json={'expected_version': 1, 'config': config}).status_code == 409
        versions = c.get(path + '/versions', headers=h).json()['versions']
        assert [v['version'] for v in versions] == [2, 1]
        assert versions[0]['snapshot']['config']['reasoning'] == 'local'
        restored = c.post(path + '/restore', headers=h, json={'expected_version': 2, 'source_version': 1})
        assert restored.status_code == 200, restored.text
        assert restored.json()['version'] == 3
        assert restored.json()['config']['reasoning'] == 'gemini'
        assert c.post(path + '/restore', headers=h, json={'expected_version': 2, 'source_version': 1}).status_code == 409
        assert c.post(path + '/restore', headers={**h, 'X-External-User-ID': 'foreign'}, json={'expected_version': 3, 'source_version': 1}).status_code == 404
        assert captured[0].profile['reasoning'] == 'gemini'  # live session is immutable
        saved = c.get('/v1/voice/sessions', headers=h).json()['sessions'][0]
        assert saved['agent_snapshot']['version'] == 1

        async def events():
            await asyncio.gather(*(captured[0].event('asr', {'duration_ms': n, 'secret': 'never-store'}) for n in range(10)))
            await captured[0].event('user', {}, 'Private consenting caller transcript')
        c.portal.call(events)
        timeline = c.get('/v1/voice/sessions/' + session + '/events', headers=h).json()
        assert timeline['estimated_cost'] is None
        assert 'never-store' not in str(timeline)
        sequences = [e['sequence'] for e in timeline['events']]
        assert len(set(sequences)) == len(sequences)
        assert timeline['events'][-1]['content'] == 'Private consenting caller transcript'
        other = {**h, 'X-External-User-ID': 'another-user'}
        assert c.get('/v1/voice/sessions/' + session + '/events', headers=other).status_code == 404
        assert c.get('/v1/voice/sessions', headers=other).json()['sessions'] == []
        assert c.post(path + '/configuration', headers=other, json={'expected_version': 2, 'config': config}).status_code == 404
        with environment(monkeypatch) as (_, foreign, _, _):
            assert c.get(path + '/versions', headers=foreign).status_code == 404
            assert c.get('/v1/voice/sessions/' + session + '/events', headers=foreign).status_code == 404
        assert c.delete('/v1/voice/sessions/' + session + '/transcript', headers=h).json()['deleted']
        c.portal.call(captured[0].event, 'user', {}, 'Do not retain')
        assert sql('SELECT * FROM web_voice_transcripts WHERE session_id=%s', (session,)) == []
        c.portal.call(captured[0].event, 'failed', {'stage': 'connection'})
        c.post('/v1/voice/sessions/' + session + '/end', headers=h, json={})
        assert c.get('/v1/voice/sessions/' + session, headers=h).json()['state']=='FAILED'


def test_monthly_limit_and_transcript_default(monkeypatch):
    monkeypatch.setattr(web_voice, 'ready', lambda: True)
    monkeypatch.setenv('AI_VOICE_MONTHLY_SESSION_LIMIT', '1')
    captured = []

    class Capture(Media):
        def __init__(self, *args):
            super().__init__(*args)
            captured.append(self)
    monkeypatch.setattr(web_voice.calling_media, 'Session', Capture)
    with environment(monkeypatch) as (c, h, key, _):
        agent, _ = create(c, h, key)
        path = '/v1/voice/agents/' + agent['id'] + '/sessions'
        offer = {'sdp': 'v=0\r\n' + 'fixture ' * 5, 'request_key': uuid4().hex, 'audio_consent': True}
        first = c.post(path, headers=h, json=offer)
        assert first.status_code == 201
        session = first.json()['id']
        c.portal.call(captured[0].event, 'user', {}, 'Not consented')
        assert sql('SELECT * FROM web_voice_transcripts WHERE session_id=%s', (session,)) == []
        c.post('/v1/voice/sessions/' + session + '/end', headers=h, json={})
        assert c.post(path, headers=h, json={**offer, 'request_key': uuid4().hex}).status_code == 429


def test_interruption_fences_uncancellable_late_audio(monkeypatch):
    pytest.importorskip("aiortc")
    async def run():
        release = asyncio.Event()
        started = asyncio.Event()
        events = []

        async def chunks(text):
            started.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()  # deliberately uncooperative provider
            yield b'\x01\x00' * 1600, 16000

        async def ended():
            events.append('closed')

        monkeypatch.setattr(calling_audio, 'speech_chunks', chunks)
        session = calling_media.Session(None, 'Hello', ended)
        session.audio.extend(b'old audio')
        session.turn_task = session.spawn(session.say('late'))
        await started.wait()
        await session.interrupt()
        assert not session.audio
        release.set()
        await session.turn_task
        assert not session.audio  # stale synthesis cannot leak into the next turn
        await session.close()
        assert events == ['closed']
    asyncio.run(run())


def test_media_provider_timeout_closes_and_logs_no_diagnostics(monkeypatch):
    pytest.importorskip("aiortc")
    async def run():
        events = []

        async def fail(pcm):
            raise TimeoutError('private upstream details')

        async def emit(kind, details=None, text=None):
            events.append((kind, details))

        async def ended():
            pass

        monkeypatch.setattr(calling_audio, 'transcribe', fail)
        session = calling_media.Session(None, 'Hi', ended)
        session.event = emit
        await session.speech_turn(b'\x00\x00' * 100)
        assert session.closed
        assert events == [('turn_failed', {'outcome': 'failed', 'stage': 'audio_turn'})]
    asyncio.run(run())


def test_local_speech_clock_pronunciation_preserves_times():
    assert calling_audio.spoken_clock('Open 9 AM to 5 PM.') == "Open nine o'clock in the morning to five o'clock in the evening."
    assert calling_audio.spoken_clock('12 AM and 12 PM') == 'midnight and noon'
    assert calling_audio.spoken_clock('9:30 AM costs 9 rupees') == '9:30 AM costs 9 rupees'


def test_voice_crud_and_content_retention(monkeypatch):
    from test_knowledge import Model

    monkeypatch.setattr(web_voice, 'ready', lambda: True)
    monkeypatch.setattr(web_voice, 'voice_gateway', lambda: Model())
    captured = []

    class Capture(Media):
        def __init__(self, *args):
            super().__init__(*args)
            captured.append(self)
    monkeypatch.setattr(web_voice.calling_media, 'Session', Capture)
    with environment(monkeypatch) as (c, h, key, _):
        agent, body = create(c, h, key)
        path = '/v1/voice/agents/' + agent['id']
        changed = {**body, 'name': 'Updated agent', 'greeting': 'New greeting', 'expected_version': 1}
        edited = c.put(path, headers=h, json=changed)
        assert edited.status_code == 200, edited.text
        assert edited.json()['version'] == 2
        assert c.put(path, headers=h, json=changed).status_code == 409
        call = c.post(path+'/sessions', headers=h, json={'sdp':'fixture sdp '*8,
            'request_key':uuid4().hex, 'audio_consent':True}).json()['id']
        c.portal.call(captured[0].respond, 'What is the refund policy?')
        rows = sql('SELECT result FROM knowledge_answers WHERE app_id=%s', (agent['app_id'],))
        assert rows and rows[-1]['result']['content_retained'] is False
        assert 'claims' not in rows[-1]['result']
        assert 'text' not in sql('SELECT reply FROM web_voice_turns WHERE session_id=%s', (call,))[0]['reply']
        assert c.delete(path, headers=h).json()['deleted']
        assert c.get('/v1/voice/agents', headers=h).json()['agents'] == []
        assert c.get(path+'/readiness', headers=h).status_code == 404
        assert c.get('/v1/voice/sessions/'+call, headers=h).json()['state'] == 'ENDED'
        assert c.post('/v1/voice/agents', headers=h, json=body).status_code == 409


def test_stale_partial_transcript_cannot_publish_after_new_utterance(monkeypatch):
    pytest.importorskip('aiortc')

    async def run():
        entered, release = asyncio.Event(), asyncio.Event()
        events = []

        async def transcribe(pcm):
            entered.set()
            await release.wait()
            return 'old partial'

        async def emit(*args):
            events.append(args)

        async def ended():
            pass

        monkeypatch.setattr(calling_audio, 'transcribe', transcribe)
        session = calling_media.Session(None, 'Hi', ended)
        session.event = emit
        task = asyncio.create_task(session.partial(b'\x00\x00', 0))
        await entered.wait()
        session.utterance += 1
        release.set()
        await task
        assert events == []
        await session.close()
    asyncio.run(run())


def test_barge_in_allows_next_faq_but_never_replays_uncertain_action(monkeypatch):
    monkeypatch.setattr(web_voice, 'ready', lambda: True)
    monkeypatch.setattr(web_voice, 'voice_gateway', lambda: object())
    captured = []

    class Capture(Media):
        def __init__(self, *args):
            super().__init__(*args)
            captured.append(self)
    monkeypatch.setattr(web_voice.calling_media, 'Session', Capture)

    async def cancelled(*args, **kwargs):
        raise asyncio.CancelledError()

    async def answered(*args, **kwargs):
        return {'action': 'continue', 'text': 'The next answer'}

    with environment(monkeypatch) as (c, h, key, _):
        agent, _ = create(c, h, key)
        call = c.post('/v1/voice/agents/'+agent['id']+'/sessions', headers=h,
            json={'sdp':'fixture sdp '*8,'request_key':uuid4().hex,'audio_consent':True}).json()['id']

        async def cancel_turn():
            with pytest.raises(asyncio.CancelledError):
                await captured[0].respond('First question')
        monkeypatch.setattr(web_voice.voice_conversation, 'respond', cancelled)
        c.portal.call(cancel_turn)
        assert sql('SELECT state FROM web_voice_turns WHERE session_id=%s', (call,))[0]['state']=='CANCELLED'
        monkeypatch.setattr(web_voice.voice_conversation, 'respond', answered)
        assert c.portal.call(captured[0].respond, 'Next question')['text']=='The next answer'
        monkeypatch.setattr(web_voice, 'action_handler', lambda agent: {'url': 'https://unused.example'})
        monkeypatch.setattr(web_voice, 'invoke', cancelled)
        c.portal.call(cancel_turn)
        assert sql('SELECT state FROM web_voice_turns WHERE session_id=%s ORDER BY turn DESC', (call,))[0]['state']=='UNKNOWN'
        before = len(sql('SELECT id FROM web_voice_turns WHERE session_id=%s', (call,)))
        assert c.portal.call(captured[0].respond, 'Try again')['action']=='handoff'
        assert len(sql('SELECT id FROM web_voice_turns WHERE session_id=%s', (call,)))==before
