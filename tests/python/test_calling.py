import asyncio
import hashlib
import hmac
import json
import time
from uuid import uuid4

import httpx
import pytest
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from test_task_agents import database, environment, sql
from test_whatsapp import setup

from agent_platform import calling_provider as provider
from agent_platform.calling_worker import Worker


def draft(c, key, monkeypatch):
    h, channel, _ = setup(c, key, monkeypatch)
    body = {
        "external_id": "business-voice",
        "name": "Business voice",
        "greeting": "How can I help?",
        "handoff_message": "Please contact our team.",
    }
    r = c.post("/v1/calling/agents", headers=h, json=body)
    assert r.status_code == 201, r.text
    return h, channel, r.json(), body


def connect(c, h, a, channel):
    r = c.post(
        f"/v1/calling/agents/{a['id']}/connection",
        headers=h,
        json={"channel_id": channel["id"], "authorized_to_connect": True},
    )
    assert r.status_code == 200, r.text


def enable(c, h, a, monkeypatch):
    async def ready(*args):
        return {"ready": True, "code": "ready", "message": "Ready"}

    monkeypatch.setattr(provider, "readiness", ready)
    sql(
        "INSERT INTO whatsapp_call_worker_health VALUES('voice',now(),true) ON CONFLICT(id) DO UPDATE SET last_seen=now(),media_ready=true"
    )
    r = c.post(f"/v1/calling/agents/{a['id']}/activation", headers=h, json={"enabled": True})
    assert r.status_code == 200, r.text


def webhook(c, channel, event, valid=True):
    raw = json.dumps(
        {
            "object": "whatsapp_business_account",
            "entry": [
                {
                    "changes": [
                        {
                            "field": "calls",
                            "value": {"metadata": {"phone_number_id": channel["phone_id"]}, "calls": [event]},
                        }
                    ]
                }
            ],
        }
    ).encode()
    sig = "sha256=" + hmac.new(b"fixture-secret", raw, hashlib.sha256).hexdigest() if valid else "invalid"
    return c.post("/webhooks/whatsapp/" + channel["id"], content=raw, headers={"X-Hub-Signature-256": sig})


def work(method):
    async def run():
        async with AsyncConnectionPool(database(), open=False, kwargs={"row_factory": dict_row}) as pool:
            return await getattr(Worker(pool), method)()

    return asyncio.run(run())


def test_whatsapp_only_and_ownership(monkeypatch):
    with environment(monkeypatch) as (c, _, key, _):
        h, ch, a, body = draft(c, key, monkeypatch)
        caps = c.get("/v1/calling/capabilities", headers=h).json()
        assert caps["providers"] == ["meta", "chakra"] and not caps["ordinary_phone_calls"]
        assert c.post("/webhooks/calling/" + a["id"] + "/inbound").status_code == 404
        assert (
            c.post(
                "/v1/calling/agents",
                headers=h,
                json={**body, "external_id": "other", "connection_ref": "old-connection"},
            ).status_code
            == 422
        )
        connect(c, h, a, ch)
        other = {**h, "X-External-User-ID": "other-business"}
        assert c.get(f"/v1/calling/agents/{a['id']}/setup", headers=other).status_code == 404
        assert (
            c.post(
                f"/v1/calling/agents/{a['id']}/connection",
                headers=other,
                json={"channel_id": ch["id"], "authorized_to_connect": True},
            ).status_code
            == 404
        )
        second = c.post("/v1/calling/agents", headers=h, json={**body, "external_id": "second"}).json()
        assert (
            c.post(
                f"/v1/calling/agents/{second['id']}/connection",
                headers=h,
                json={"channel_id": ch["id"], "authorized_to_connect": True},
            ).status_code
            == 409
        )

        async def not_set(*args):
            return {"ready": False, "code": "calling_disabled", "message": "Enable WhatsApp calling"}

        monkeypatch.setattr(provider, "readiness", not_set)
        assert (
            c.get(f"/v1/calling/agents/{a['id']}/readiness", headers=h).json()["code"] == "calling_disabled"
        )
        assert (
            c.post(f"/v1/calling/agents/{a['id']}/activation", headers=h, json={"enabled": True}).status_code
            == 409
        )


def test_webhook_signature_duplicate_and_terminal_fence(monkeypatch):
    with environment(monkeypatch) as (c, _, key, _):
        h, ch, a, _ = draft(c, key, monkeypatch)
        connect(c, h, a, ch)
        event = {"id": "wacid." + uuid4().hex, "event": "terminate", "duration": 12}
        assert webhook(c, ch, event, False).status_code == 403
        for _ in range(2):
            assert webhook(c, ch, event).status_code == 200
        assert (
            sql("SELECT count(*) AS n FROM whatsapp_call_events WHERE agent_id=%s", (a["id"],))[0]["n"] == 1
        )
        assert work("event")
        later = {
            **event,
            "event": "connect",
            "direction": "USER_INITIATED",
            "timestamp": str(int(time.time())),
            "session": {"sdp_type": "offer", "sdp": "invalid"},
        }
        assert webhook(c, ch, later).status_code == 200
        assert work("event")
        calls = c.get(f"/v1/calling/agents/{a['id']}/calls", headers=h).json()["calls"]
        assert len(calls) == 1 and calls[0]["state"] == "COMPLETED" and calls[0]["duration_seconds"] == 12
        assert not sql(
            "SELECT 1 FROM whatsapp_call_events WHERE agent_id=%s AND payload ? 'session'", (a["id"],)
        )


def test_exact_test_approval_and_unknown_never_retries(monkeypatch):
    with environment(monkeypatch) as (c, _, key, _):
        h, ch, a, _ = draft(c, key, monkeypatch)
        connect(c, h, a, ch)
        enable(c, h, a, monkeypatch)
        path = f"/v1/calling/agents/{a['id']}/test-calls"
        body = {"recipient": "+919871928555", "idempotency_key": uuid4().hex}
        r = c.post(path, headers=h, json=body).json()
        assert r["state"] == "AWAITING_APPROVAL" and body["recipient"] in r["summary"]
        assert not work("outbound")
        assert c.post(path, headers=h, json=body).json()["id"] == r["id"]
        assert c.post(path, headers=h, json={**body, "recipient": "+919999999999"}).status_code == 409
        assert (
            c.post(
                path + "/" + r["id"] + "/approve",
                headers=h,
                json={"approved": True, "recipient": "+919999999999"},
            ).status_code
            == 422
        )
        for _ in range(2):
            assert (
                c.post(path + "/" + r["id"] + "/approve", headers=h, json={"approved": True}).status_code
                == 202
            )
        sends = []

        class Media:
            pc = None

            def __init__(self, *args):
                self.pc = self

            async def offer(self):
                return "fixture-sdp"

            async def close(self):
                return None

        async def uncertain(*args):
            sends.append(args)
            raise provider.CallingError("UNKNOWN", uncertain=True)

        monkeypatch.setattr("agent_platform.calling_worker.calling_media.Session", Media)
        monkeypatch.setattr(provider, "start_call", uncertain)
        assert work("outbound") and not work("outbound") and len(sends) == 1
        assert c.get(path + "/" + r["id"], headers=h).json()["state"] == "UNKNOWN"
        second = c.post(path, headers=h, json={**body, "idempotency_key": uuid4().hex}).json()
        assert (
            c.post(path + "/" + second["id"] + "/approve", headers=h, json={"approved": True}).status_code
            == 409
        )


@pytest.mark.parametrize("mode", ["meta", "chakra"])
def test_provider_settings_and_permission(mode):
    cfg = {"provider": mode, "graph_version": "v23.0", "access_token": "secret"}

    def respond(request):
        assert request.url.host == ("api.chakrahq.com" if mode == "chakra" else "graph.facebook.com")
        assert request.headers["Authorization"] == "Bearer secret"
        value = (
            {"calling": {"status": "ENABLED"}}
            if request.url.path.endswith("/settings")
            else {
                "permission": {"status": "temporary", "expiration_time": int(time.time()) + 100},
                "actions": [{"action_name": "start_call", "can_perform_action": True}],
            }
        )
        return httpx.Response(200, json={"_data": value} if mode == "chakra" else value)

    async def run():
        transport = httpx.MockTransport(respond)
        assert (await provider.readiness(cfg, "123456", transport=transport))["ready"]
        assert (await provider.permission(cfg, "123456", "+919871928555", transport=transport))["can_call"]

    asyncio.run(run())


@pytest.mark.parametrize(
    "value",
    [
        {
            "permission": {"status": "temporary", "expiration_time": 1},
            "actions": [{"action_name": "start_call", "can_perform_action": True}],
        },
        {
            "permission": {"status": "permanent"},
            "actions": [{"action_name": "start_call", "can_perform_action": False}],
        },
        {
            "permission": {"status": "unknown"},
            "actions": [{"action_name": "start_call", "can_perform_action": True}],
        },
    ],
)
def test_permission_expiry_and_provider_limits(value):
    cfg = {"graph_version": "v23.0", "access_token": "secret"}
    result = asyncio.run(
        provider.permission(
            cfg,
            "123456",
            "+919871928555",
            transport=httpx.MockTransport(lambda r: httpx.Response(200, json=value)),
        )
    )
    assert not result["can_call"]


def test_success_with_nested_provider_error_is_not_ready():
    cfg = {"provider": "chakra", "graph_version": "v23.0", "access_token": "secret"}
    result = asyncio.run(
        provider.readiness(
            cfg,
            "123456",
            transport=httpx.MockTransport(
                lambda r: httpx.Response(
                    200, json={"_data": {"error": {"message": "private provider response"}}}
                )
            ),
        )
    )
    assert not result["ready"] and "private" not in json.dumps(result)


def test_local_webrtc_audio_handshake(monkeypatch):
    pytest.importorskip("aiortc")
    from agent_platform.calling_media import Session

    async def quiet(text):
        return b"\0" * 4800, 24000

    monkeypatch.setattr("agent_platform.calling_audio.speak", quiet)

    async def run():
        async def dialogue(text):
            return {"action": "end", "text": "Goodbye"}

        async def ended():
            return None

        first, second = Session(dialogue, "Hello", ended), Session(dialogue, "Hello", ended)
        try:
            offer = await first.offer()
            answer = await second.remote(offer, "offer")
            await first.remote(answer, "answer")
            async with asyncio.timeout(10):
                while first.pc.connectionState != "connected" or second.pc.connectionState != "connected":
                    await asyncio.sleep(0.05)
            assert first.greeted and second.greeted
        finally:
            await first.close()
            await second.close()

    asyncio.run(run())


def test_provider_enable_reconciles_timeout_and_requires_approval(monkeypatch):
    with environment(monkeypatch) as (c, _, key, _):
        h, ch, a, _ = draft(c, key, monkeypatch)
        connect(c, h, a, ch)
        path = f"/v1/calling/agents/{a['id']}/provider-settings"
        assert c.post(path, headers=h, json={"approved": False}).status_code == 422
        reads, sends = [], []

        async def readiness(*args):
            reads.append(True)
            return {
                "ready": len(reads) > 1,
                "code": "ready" if len(reads) > 1 else "calling_disabled",
                "message": "checked",
            }

        async def mutation(*args, **kwargs):
            sends.append(kwargs)
            raise provider.CallingError("timeout", uncertain=True)

        monkeypatch.setattr(provider, "readiness", readiness)
        monkeypatch.setattr(provider, "request", mutation)
        assert c.post(path, headers=h, json={"approved": True}).json()["ready"]
        assert len(reads) == 2 and len(sends) == 1
        assert c.post(path, headers=h, json={"approved": True}).json()["ready"]
        assert len(sends) == 1
