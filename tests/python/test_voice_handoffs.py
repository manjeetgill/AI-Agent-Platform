import asyncio
from uuid import uuid4

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from test_task_agents import database, environment, sql
from test_web_voice import Media
from test_whatsapp import clean, inbound, rows, setup, work

from agent_platform import sahayak_bridge, voice_handoffs, web_voice


def test_whatsapp_link_one_call_and_preserves_bound_context(monkeypatch):
    monkeypatch.setenv("AI_WEB_VOICE_ENABLED", "1")
    monkeypatch.setenv("AI_WEB_VOICE_PUBLIC_ORIGIN", "https://voice.example")
    monkeypatch.setattr(web_voice, "ready", lambda: True)
    monkeypatch.setattr(web_voice.calling_media, "Session", Media)
    calls = []

    async def clinic(channel, event, customer, text, **kwargs):
        calls.append((channel["id"], customer, text, kwargs))
        return {
            "replies": ["Please confirm your appointment time."],
            "handledBy": "agent",
            "redFlag": False,
            **({"voice_requested": True} if kwargs.get("voice_available") else {}),
        }

    monkeypatch.setattr(sahayak_bridge, "request", clinic)
    with environment(monkeypatch) as (client, _, key, _):
        try:
            _, channel, _ = setup(client, key, monkeypatch)
            sql("UPDATE whatsapp_channels SET execution_mode='SAHAYAK' WHERE id=%s", (channel["id"],))
            inbound(client, channel, text="Talk to AI receptionist", mid="voice-" + uuid4().hex)
            work()
            message = rows(channel)[0]
            assert message["status"] == "READY"
            link = message["reply"].splitlines()[-1]
            assert link.startswith("https://voice.example/voice/call#")
            token = link.split("#")[1]
            stored = sql("SELECT * FROM web_voice_handoffs WHERE message_id=%s", (message["id"],))[0]
            assert token not in str(stored)  # Only the hash is in the invitation table.
            h = {"Authorization": "Bearer " + token}
            assert client.get("/public/voice/invitation").status_code == 401
            assert (
                client.get(
                    "/public/voice/invitation", headers={"Authorization": "Bearer " + "x" * 43}
                ).status_code
                == 410
            )
            assert not client.get("/public/voice/invitation", headers=h).json()["used"]
            offer = {"sdp": "v=0\r\n" + "fixture " * 5, "request_key": uuid4().hex, "audio_consent": True}
            assert (
                client.post(
                    "/public/voice/session", headers=h, json={**offer, "audio_consent": False}
                ).status_code
                == 422
            )
            response = client.post("/public/voice/session", headers=h, json=offer)
            assert response.status_code == 201, response.text
            ident = response.json()["id"]
            assert calls[-1][3]["voice_resume"] and calls[-1][:2] == calls[0][:2]
            assert (
                client.post(
                    "/public/voice/session", headers=h, json={**offer, "request_key": uuid4().hex}
                ).status_code
                == 409
            )
            assert client.get("/public/voice/session/" + str(uuid4()), headers=h).status_code == 404
            assert client.get("/public/voice/session/" + ident, headers=h).status_code == 200
            call = sql("SELECT * FROM web_voice_sessions WHERE id=%s", (ident,))[0]
            agent = sql("SELECT * FROM web_voice_agents WHERE id=%s", (stored["agent_id"],))[0]

            async def turn():
                async with AsyncConnectionPool(
                    database(), open=False, kwargs={"row_factory": dict_row}
                ) as pool:
                    return await web_voice.dialogue(pool, agent, call, "Yes")

            assert asyncio.run(turn())["action"] == "continue"
            assert calls[-1][2] == "Yes" and calls[-1][3]["voice_turn"]
            assert calls[-1][:2] == calls[0][:2]
            assert (
                client.post("/public/voice/session/" + ident + "/end", headers=h, json={}).status_code == 200
            )
            sql(
                "UPDATE web_voice_handoffs SET expires_at=now()-interval '1 second' WHERE id=%s",
                (stored["id"],),
            )
            assert client.get("/public/voice/invitation", headers=h).status_code == 410
        finally:
            clean(key)


def test_public_origin_fail_closed(monkeypatch):
    monkeypatch.setenv("AI_WEB_VOICE_ENABLED", "1")
    for origin in [
        "http://public.example",
        "https://user:pass@example.com",
        "https://example.com/private",
        "https://example.com?secret=x",
    ]:
        monkeypatch.setenv("AI_WEB_VOICE_PUBLIC_ORIGIN", origin)
        assert voice_handoffs.public_origin() is None
    monkeypatch.setenv("AI_WEB_VOICE_PUBLIC_ORIGIN", "http://127.0.0.1:4061")
    assert voice_handoffs.public_origin() == "http://127.0.0.1:4061"


def test_human_takeover_stops_voice_before_clinic_request(monkeypatch):
    monkeypatch.setenv("AI_WEB_VOICE_ENABLED", "1")
    monkeypatch.setenv("AI_WEB_VOICE_PUBLIC_ORIGIN", "https://voice.example")
    with environment(monkeypatch) as (client, _, key, _):
        try:
            _, channel, _ = setup(client, key, monkeypatch)
            sql("UPDATE whatsapp_channels SET execution_mode='SAHAYAK' WHERE id=%s", (channel["id"],))
            inbound(client, channel, mid="human-voice-" + uuid4().hex)
            message = rows(channel)[0]
            conversation = sql(
                "SELECT * FROM whatsapp_conversations WHERE id=%s", (message["conversation_id"],)
            )[0]

            async def issue():
                async with AsyncConnectionPool(
                    database(), open=False, kwargs={"row_factory": dict_row}
                ) as pool:
                    return await voice_handoffs.issue(
                        pool, {**channel, "execution_mode": "SAHAYAK"}, conversation, message
                    )

            link = asyncio.run(issue())
            handoff = sql("SELECT id FROM web_voice_handoffs WHERE message_id=%s", (message["id"],))[0]
            sql("UPDATE whatsapp_conversations SET mode='HUMAN' WHERE id=%s", (conversation["id"],))
            assert (
                client.get(
                    "/public/voice/invitation", headers={"Authorization": "Bearer " + link.split("#")[1]}
                ).status_code
                == 410
            )

            async def turn():
                async with AsyncConnectionPool(
                    database(), open=False, kwargs={"row_factory": dict_row}
                ) as pool:
                    return await voice_handoffs.clinic_turn(pool, handoff["id"], uuid4(), "Book it")

            assert asyncio.run(turn())["action"] == "handoff"
        finally:
            clean(key)


def test_turn_credentials_are_short_lived_and_never_expose_shared_secret(monkeypatch):
    import base64
    import hashlib
    import hmac

    from agent_platform import voice_ice

    secret = "private-turn-fixture-" * 3
    monkeypatch.setenv("AI_VOICE_TURN_SECRET", secret)
    monkeypatch.setenv("AI_VOICE_TURN_URLS", '["turn:relay.example:3478?transport=tcp"]')
    monkeypatch.setattr(voice_ice.time, "time", lambda: 1000)
    result = voice_ice.servers("invitation-id")[0]
    assert result["username"] == "1300:invitation-id"
    assert secret not in str(result)
    assert (
        result["credential"]
        == base64.b64encode(hmac.new(secret.encode(), b"1300:invitation-id", hashlib.sha1).digest()).decode()
    )


def test_local_speech_fallback_requires_opt_in_and_only_transient_tts_failure(monkeypatch):
    import httpx
    import pytest

    from agent_platform import calling_audio

    calls = []

    async def local(text):
        calls.append(text)
        return b"pcm", 22050

    code = 429

    async def limited(text):
        response = httpx.Response(code, request=httpx.Request("POST", "https://example.test/speech"))
        response.raise_for_status()

    monkeypatch.setattr(calling_audio, "gemini_speak", limited)
    monkeypatch.setattr(calling_audio, "local_speak", local)
    monkeypatch.setenv("AI_VOICE_LOCAL_TTS_FALLBACK", "0")
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(calling_audio.speak("test"))
    monkeypatch.setenv("AI_VOICE_LOCAL_TTS_FALLBACK", "1")
    assert asyncio.run(calling_audio.speak("test")) == (b"pcm", 22050)
    assert calls == ["test"]
    code = 401
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(calling_audio.speak("test"))
    assert calls == ["test"]
