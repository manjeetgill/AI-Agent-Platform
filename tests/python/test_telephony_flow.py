"""Incoming audio -> platform speech/agent -> signed app tool -> exact consent -> booking."""

import base64
import importlib.util
import json
from pathlib import Path

import httpx
from fastapi.testclient import TestClient
from test_task_agents import environment, sql
from test_telephony import activate, setup, start

from agent_platform import telephony, telephony_speech, telephony_tools

ROOT = Path(__file__).resolve().parents[2]
TOOLS = json.loads((ROOT / "examples/telephony-integration/booking-tools.json").read_text())
SECRET = "fixture-private-secret-" + "x" * 40


class Planner:
    async def structured(self, task, policy, data, schema, validator):
        assert "untrusted data" in policy
        text = data["input"].lower()
        tool = "booking.slots" if "slots" in text else "booking.create"
        args = {} if tool == "booking.slots" else {"slot_id": "demo-0900", "patient_name": "Test Patient"}
        return schema(kind="tool", tool=tool, arguments=args), 22, "fixture", "fixture"


def configured(c, h, r, monkeypatch, key):
    monkeypatch.setenv("AI_META_ENCRYPTION_KEYS", json.dumps({"test": base64.b64encode(b"t" * 32).decode()}))
    monkeypatch.setenv("AI_META_ENCRYPTION_KEY_ID", "test")
    monkeypatch.setenv("AI_TELEPHONY_TOOL_ORIGINS", json.dumps({key["app_id"]: ["https://booking.example"]}))
    result = c.put(
        f"/v1/telephony/connections/{r['id']}/tools",
        headers=h,
        json={"endpoint": "https://booking.example/agent-tools", "secret": SECRET, "tools": TOOLS},
    )
    assert result.status_code == 200, result.text
    assert SECRET not in result.text
    monkeypatch.setattr(telephony, "voice_gateway", lambda: Planner())


def app_server(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "booking_demo", ROOT / "examples/telephony-integration/booking_app.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    demo = module.create_app(SECRET, tmp_path / "demo.sqlite")
    original = httpx.AsyncClient

    def client(*args, **kwargs):
        return original(*args, **kwargs, transport=httpx.ASGITransport(app=demo))

    monkeypatch.setattr(telephony_tools.httpx, "AsyncClient", client)
    return demo


def say(c, r, g, call, turn, text):
    return c.post(
        f"/v1/telephony/gateway/{r['id']}/calls/{call}/turns/{turn}", headers=g, json={"text": text}
    )


def actions(c, h, r, call):
    return c.get(f"/v1/telephony/connections/{r['id']}/calls/{call}/actions", headers=h).json()["actions"]


def authorize(c, h, r, call, tools=None):
    return c.post(
        f"/v1/telephony/connections/{r['id']}/calls/{call}/authorize",
        headers=h,
        json={"subject": "demo-patient", "tools": tools if tools is not None else ["booking.create"]},
    )


def test_signed_booking_flow_requires_identity_and_exact_confirmation(monkeypatch, tmp_path):
    app_server(tmp_path, monkeypatch)
    with environment(monkeypatch) as (c, old, key, _):
        h, r, g = setup(c, old, key, monkeypatch)
        configured(c, h, r, monkeypatch, key)
        activate(c, h, r, g)
        call, _ = start(c, r, g)
        slots = say(c, r, g, call, 1, "available slots")
        assert slots.status_code == 200, slots.text
        assert "demo-0900" in slots.json()["text"]
        refused = say(c, r, g, call, 2, "book an appointment")
        assert "verify" in refused.json()["text"]
        assert len(actions(c, h, r, call)) == 1
        assert authorize(c, g, r, call).status_code == 401
        assert authorize(c, h, r, call).status_code == 200
        proposed = say(c, r, g, call, 3, "book demo-0900 for Test Patient")
        assert proposed.status_code == 200, proposed.text
        assert "Test Patient" in proposed.json()["text"] and "demo-0900" in proposed.json()["text"]
        assert actions(c, h, r, call)[-1]["state"] == "PENDING"
        unclear = say(c, r, g, call, 4, "yes but a different time")
        assert "yes to confirm" in unclear.json()["text"]
        confirmed = say(c, r, g, call, 5, "yes")
        assert confirmed.status_code == 200, confirmed.text
        assert confirmed.json()["actions_executed"] is True
        assert "confirmed" in confirmed.json()["text"]
        assert say(c, r, g, call, 5, "yes").json() == confirmed.json()
        assert actions(c, h, r, call)[-1]["approved_turn"] == 5
        stored = sql("SELECT * FROM telephony_tool_configs WHERE connection_id=%s", (r["id"],))[0]
        assert SECRET not in stored["secret_cipher"]
        assert SECRET not in c.get(f"/v1/telephony/connections/{r['id']}/tools", headers=h).text


def test_ambiguous_action_never_retries_and_reconciles_after_hangup(monkeypatch, tmp_path):
    app_server(tmp_path, monkeypatch)
    real = telephony_tools.invoke
    requests = []

    async def uncertain(row, cfg, payload):
        requests.append(payload["operation"])
        result = await real(row, cfg, payload)
        if payload["operation"] == "execute":
            raise httpx.ReadTimeout("Response lost after booking")
        return result

    monkeypatch.setattr(telephony_tools, "invoke", uncertain)
    with environment(monkeypatch) as (c, old, key, _):
        h, r, g = setup(c, old, key, monkeypatch)
        configured(c, h, r, monkeypatch, key)
        activate(c, h, r, g)
        call, _ = start(c, r, g)
        authorize(c, h, r, call)
        say(c, r, g, call, 1, "book")
        result = say(c, r, g, call, 2, "yes")
        assert result.json()["action"] == "handoff" and result.json()["actions_executed"] is False
        item = actions(c, h, r, call)[0]
        assert item["state"] == "UNKNOWN"
        say(c, r, g, call, 3, "yes")
        assert requests == ["execute"]
        c.post(f"/v1/telephony/gateway/{r['id']}/calls/{call}/end", headers=g, json={"state": "ENDED"})
        recovered = c.post(
            f"/v1/telephony/connections/{r['id']}/calls/{call}/actions/{item['id']}/reconcile", headers=h
        )
        assert recovered.status_code == 200, recovered.text
        assert recovered.json()["state"] == "DONE"
        assert requests == ["execute", "reconcile"]


def test_revocation_financial_steps_and_cross_app_configuration(monkeypatch, tmp_path):
    app_server(tmp_path, monkeypatch)
    with environment(monkeypatch) as (c, old, key, _):
        h, r, g = setup(c, old, key, monkeypatch)
        configured(c, h, r, monkeypatch, key)
        activate(c, h, r, g)
        call, _ = start(c, r, g)
        authorize(c, h, r, call)
        say(c, r, g, call, 1, "book")
        authorize(c, h, r, call, [])
        result = say(c, r, g, call, 2, "yes")
        assert not result.json()["actions_executed"]
        assert actions(c, h, r, call)[0]["state"] == "CANCELLED"
        money = [{**TOOLS[1], "risk": "USER_ONLY"}]
        c.put(
            f"/v1/telephony/connections/{r['id']}/tools",
            headers=h,
            json={"endpoint": "https://booking.example/agent-tools", "secret": SECRET, "tools": money},
        )
        result = say(c, r, g, call, 3, "book")
        assert "securely" in result.json()["text"] and result.json()["action"] == "handoff"
        bad = c.put(
            f"/v1/telephony/connections/{r['id']}/tools",
            headers=h,
            json={"endpoint": "https://127.0.0.1/admin", "secret": SECRET, "tools": TOOLS},
        )
        assert bad.status_code == 422


def test_platform_owns_speech_and_reserved_audio_cannot_replay(monkeypatch, tmp_path):
    app_server(tmp_path, monkeypatch)
    spoken = []
    inputs = iter(["available slots", "book demo-0900 for Test Patient", "haan"])

    async def stt(pcm):
        assert len(pcm) == 3200
        return next(inputs)

    async def tts(text):
        spoken.append(text)
        return b"\x00\x00" * 800, 8000

    monkeypatch.setattr(telephony_speech.calling_audio, "transcribe", stt)
    monkeypatch.setattr(telephony_speech, "synthesize", tts)
    with environment(monkeypatch) as (c, old, key, _):
        h, r, g = setup(c, old, key, monkeypatch)
        configured(c, h, r, monkeypatch, key)
        activate(c, h, r, g)
        call, _ = start(c, r, g)
        authorize(c, h, r, call)
        base = f"/v1/telephony/gateway/{r['id']}/calls/{call}/speech/"
        headers = {**g, "Content-Type": "application/octet-stream"}
        for turn in range(4):
            result = c.post(base + str(turn), headers=headers, content=b"\x00\x00" * 1600 if turn else b"")
            assert result.status_code == 200, result.text
            assert result.headers["content-type"] == "audio/wav"
            assert result.content.startswith(b"RIFF")
        assert "AI assistant" in spoken[0]
        assert "confirm" in spoken[2]
        assert "confirmed" in spoken[3]
        assert c.post(base + "3", headers=headers, content=b"\x00\x00" * 1600).status_code == 409
        assert len(spoken) == 4
        assert actions(c, h, r, call)[-1]["state"] == "DONE"
        assert (
            len(
                sql(
                    "SELECT * FROM telephony_speech_usage WHERE session_id IN (SELECT id FROM telephony_sessions WHERE connection_id=%s)",
                    (r["id"],),
                )
            )
            == 4
        )


def test_example_adapter_authentication_and_reconciliation_tombstone(tmp_path):
    import hashlib
    import hmac
    import time

    spec = importlib.util.spec_from_file_location(
        "booking_demo", ROOT / "examples/telephony-integration/booking_app.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with TestClient(module.create_app(SECRET, tmp_path / "isolated.sqlite")) as c:
        body = {
            "operation": "reconcile",
            "app_id": "app-a",
            "owner_id": "owner",
            "action_id": "test-key",
            "action_hash": "abc",
        }

        def send(value):
            raw = telephony_tools.canonical(value).encode()
            stamp = str(int(time.time()))
            return c.post(
                "/agent-tools",
                content=raw,
                headers={
                    "X-Agent-Timestamp": stamp,
                    "X-Agent-Signature": hmac.new(
                        SECRET.encode(), stamp.encode() + b"." + raw, hashlib.sha256
                    ).hexdigest(),
                },
            )

        assert c.post("/agent-tools", json=body).status_code == 401
        assert send(body).json()["fenced"]
        result = send({**body, "operation": "execute", "tool": "booking.create"})
        assert result.json()["outcome"] == "not_applied"
        assert send({**body, "action_hash": "different"}).status_code == 409
