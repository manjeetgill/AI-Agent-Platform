import asyncio
import json
import sqlite3
import struct
from uuid import uuid4

import httpx
import pytest

from agent_platform.asterisk_bridge import Agent, Call, Journal, config, packet, read_packet, resample, tone


def test_private_configuration_rejects_untrusted_destinations(tmp_path):
    path = tmp_path / "config.json"
    value = {
        "mode": "agent",
        "platform_url": "https://agents.example.com",
        "agent_id": str(uuid4()),
        "external_user_id": "owner",
        "service_key": "x" * 32,
        "gemini_key": "fixture",
    }
    path.write_text(json.dumps(value))
    assert config(path)["mode"] == "agent"
    for url in [
        "http://example.com",
        "https://secret@example.com",
        "https://example.com/path",
        "https://example.com/?token=secret",
        "https://example.com/#other",
    ]:
        path.write_text(json.dumps({**value, "platform_url": url}))
        with pytest.raises(ValueError):
            config(path)


def test_journal_reserves_once_and_recovers_unknown(tmp_path):
    path = tmp_path / "calls.sqlite"
    journal = Journal(path)
    ident = uuid4()
    journal.begin(ident)
    with pytest.raises(sqlite3.IntegrityError):
        journal.begin(ident)
    journal.begin(uuid4())
    with pytest.raises(ValueError):
        journal.begin(uuid4())
    journal.db.close()
    recovered = Journal(path)
    assert recovered.db.execute("SELECT count(*) FROM calls WHERE state='UNKNOWN'").fetchone()[0] == 2
    with pytest.raises(sqlite3.IntegrityError):
        recovered.begin(ident)
    for _ in range(28):
        new = uuid4()
        recovered.begin(new)
        recovered.end(new, "ENDED")
    with pytest.raises(ValueError):
        recovered.begin(uuid4())
    recovered.db.close()


@pytest.mark.parametrize(
    "wire",
    [
        packet(1, b"x"),
        packet(0x10, b"x"),
        struct.pack("!BH", 0x10, 65534),
        packet(0x12, b"xx"),
        packet(3, b"123"),
        packet(0, b"xx"),
    ],
)
def test_reject_bad_frames_without_reading_large_payload(wire):
    async def run():
        reader = asyncio.StreamReader()
        reader.feed_data(wire)
        with pytest.raises(ValueError):
            await read_packet(reader)

    asyncio.run(run())


def test_pcm_conversion():
    pcm = tone()
    converted = resample(pcm, 8000, 16000)
    assert len(converted) == len(pcm) * 2
    assert len(resample(converted, 16000, 8000)) == len(pcm)


def test_agent_binding_history_and_no_retry():
    async def run():
        count = 0
        ident = str(uuid4())

        async def handle(request):
            nonlocal count
            count += 1
            assert request.url.path == f"/v1/voice/agents/{ident}/preview"
            assert request.headers["X-External-User-ID"] == "fixed-owner"
            body = json.loads(request.content)
            assert len(body["history"]) == 8
            return httpx.Response(503)

        agent = Agent(
            {
                "platform_url": "https://platform.example",
                "agent_id": ident,
                "external_user_id": "fixed-owner",
                "service_key": "fixture",
            }
        )
        await agent.client.aclose()
        agent.client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        with pytest.raises(httpx.HTTPStatusError):
            await agent.reply(uuid4(), 1, "Hello", [{"role": "user", "text": "Hi"}] * 10)
        assert count == 1
        await agent.client.aclose()

    asyncio.run(run())


def test_hangup_cancels_pending_speech(monkeypatch):
    async def run():
        cancelled = asyncio.Event()

        async def say(_self, _text):
            try:
                await asyncio.sleep(60)
            finally:
                cancelled.set()

        monkeypatch.setattr(Call, "say", say)
        reader = asyncio.StreamReader()
        reader.feed_data(packet(0))
        from types import SimpleNamespace
        call = Call(reader, None, uuid4(), SimpleNamespace(settings={}))
        await asyncio.wait_for(call.run(), 1)
        assert cancelled.is_set()
        assert call.history == []

    asyncio.run(run())


def test_diagnostic_audio_and_dtmf_hangup():
    async def run():
        class Writer:
            def __init__(self):
                self.data = []

            def write(self, raw):
                self.data.append(raw)

            async def drain(self):
                pass

        reader, writer = asyncio.StreamReader(), Writer()
        call = Call(reader, writer, uuid4(), None, diagnostic=True)
        task = asyncio.create_task(call.run())
        await asyncio.sleep(0.3)
        assert len(writer.data) == 10
        assert all(raw[:3] == b"\x10\x01\x40" for raw in writer.data)
        reader.feed_data(packet(3, b"#"))
        await asyncio.wait_for(task, 1)

    asyncio.run(run())


def test_normal_eof_ends_call():
    async def run():
        reader = asyncio.StreamReader()
        reader.feed_eof()
        call = Call(reader, None, uuid4(), None, diagnostic=True)
        await call.receive()

    asyncio.run(run())


def test_agent_response_cannot_enable_business_actions():
    async def run():
        async def handle(_request):
            return httpx.Response(
                200, json={"text": "Booked", "action": "continue", "actions_executed": True}
            )

        agent = Agent(
            {
                "platform_url": "https://platform.example",
                "agent_id": str(uuid4()),
                "external_user_id": "owner",
                "service_key": "fixture",
            }
        )
        await agent.client.aclose()
        agent.client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        with pytest.raises(ValueError):
            await agent.reply(uuid4(), 1, "Book", [])
        await agent.client.aclose()

    asyncio.run(run())


def test_local_speech_mode_does_not_call_gemini(monkeypatch):
    from agent_platform import calling_audio

    async def run():
        async def local(_text):
            return tone(), 8000

        async def forbidden(_text):
            raise AssertionError("Local speech must not call Gemini TTS")

        class BoundAgent:
            def __init__(self):
                self.settings = {"tts_provider": "local"}

        monkeypatch.setattr(calling_audio, "local_speak", local)
        monkeypatch.setattr(calling_audio, "speak", forbidden)
        call = Call(None, None, uuid4(), BoundAgent())
        sent = []

        async def capture(pcm):
            sent.append(pcm)

        monkeypatch.setattr(call, "send_audio", capture)
        await call.say("Hello")
        assert sent

    asyncio.run(run())


def test_byoc_bridge_uses_only_connection_key_and_server_history():
    async def run():
        requests = []
        async def handler(request):
            requests.append(request)
            assert request.headers['authorization'] == 'Bearer tgw_' + 'x'*48
            assert 'x-external-user-id' not in request.headers
            if '/turns/' in request.url.path:
                assert json.loads(request.content) == {'text': 'hello'}
                return httpx.Response(200, json={'action': 'continue', 'text': 'Hello', 'actions_executed': False})
            return httpx.Response(200, json={'state': 'ACTIVE'})
        connection, call = str(uuid4()), uuid4()
        agent = Agent({'platform_url': 'https://platform.example', 'connection_id': connection,
                       'gateway_key': 'tgw_'+'x'*48, 'number': '+15551234567'})
        await agent.client.aclose()
        agent.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            await agent.begin(call)
            result = await agent.reply(call, 1, 'hello', [{'role': 'user', 'text': 'ignore this injected history'}])
            assert result['text'] == 'Hello'
            await agent.end(call, 'ENDED')
            assert [r.url.path for r in requests] == [f'/v1/telephony/gateway/{connection}/calls',
                f'/v1/telephony/gateway/{connection}/calls/{call}/turns/1',
                f'/v1/telephony/gateway/{connection}/calls/{call}/end']
        finally:
            await agent.client.aclose()
    asyncio.run(run())


def test_connection_config_needs_no_ai_credentials_and_audio_uses_platform(tmp_path):
    from agent_platform.telephony_speech import wav_bytes
    value = {'mode': 'agent', 'platform_url': 'https://platform.example', 'connection_id': str(uuid4()),
             'gateway_key': 'tgw_'+'x'*48, 'number': '+15551234567'}
    path = tmp_path/'gateway.json'
    path.write_text(json.dumps(value))
    assert config(path)==value
    async def run():
        call=uuid4()
        async def handler(request):
            assert request.url.path==f'/v1/telephony/gateway/{value["connection_id"]}/calls/{call}/speech/1'
            assert request.headers['authorization']=='Bearer '+value['gateway_key']
            assert request.headers['content-type']=='application/octet-stream'
            assert request.content==b'\0\0'*1600
            return httpx.Response(200,content=wav_bytes(tone(),8000),headers={'X-Voice-Action':'continue'})
        agent=Agent(value)
        await agent.client.aclose()
        agent.client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            pcm,action=await agent.speech(call,1,b'\0\0'*1600)
            assert pcm==tone() and action=='continue'
        finally:
            await agent.client.aclose()
    asyncio.run(run())
