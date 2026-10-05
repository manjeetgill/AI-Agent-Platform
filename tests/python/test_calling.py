import asyncio
import json
from urllib.parse import urlencode
from uuid import uuid4

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from test_task_agents import database, environment, sql

from agent_platform.calling_provider import signature, xml_reply
from agent_platform.service_identity import provision


def fixture(client, key, monkeypatch):
    tenant = sql("SELECT tenant_id FROM platform_apps WHERE id=%s", (key["app_id"],))[0]["tenant_id"]

    async def make():
        async with AsyncConnectionPool(database(), open=False, kwargs={"row_factory": dict_row}) as pool:
            return await provision(
                pool,
                tenant=tenant,
                organisation="Calls",
                app_name="Calls",
                app_id=key["app_id"],
                scopes=["channels:read", "channels:write"],
            )

    credential = asyncio.run(make())
    h = {"Authorization": "Bearer " + credential["credential"], "X-External-User-ID": "owner"}
    owner = client.get("/v1/calling/capabilities", headers=h).json()["owner_id"]
    cfg = {
        "provider": "twilio",
        "app_id": key["app_id"],
        "owner_id": owner,
        "number": "+15551234567",
        "number_verified": True,
        "account_sid": "AC" + "a" * 32,
        "auth_token": "private-test-token-123456789",
        "handler_secret": "s" * 40,
        "public_url": "https://voice.example",
        "handler_url": "https://app.example/voice",
    }
    ref = uuid4().hex
    monkeypatch.setenv("AI_CALLING_CONNECTIONS", json.dumps({ref: cfg}))
    body = {
        "external_id": "frontdesk",
        "name": "Reception",
        "connection_ref": ref,
        "greeting": "Hello, I am the automated receptionist.",
        "handoff_message": "Please contact reception for help.",
    }
    r = client.post("/v1/calling/agents", headers=h, json=body)
    assert r.status_code == 201, r.text
    agent = r.json()
    return h, agent, cfg, body


def post(client, agent, cfg, suffix, **extra):
    params = {
        "AccountSid": cfg["account_sid"],
        "CallSid": "CA" + "b" * 32,
        "To": cfg["number"],
        "From": "+15557654321",
        "Direction": "inbound",
        **extra,
    }
    path = f"/webhooks/calling/{agent['id']}/{suffix}"
    return client.post(
        path,
        content=urlencode(params),
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "X-Twilio-Signature": signature(cfg["public_url"] + path, params, cfg["auth_token"]),
        },
    )


def enable(client, h, a, monkeypatch):
    async def verified(*args, **kwargs):
        return {"ready": True, "code": "ready", "message": "Ready"}
    monkeypatch.setattr("agent_platform.calling.provider_readiness", verified)

    async def handler(cfg, p):
        if p.get("probe"):
            return {"ready": True, "event_id": p["event_id"]}
        return {"event_id": p["event_id"], "action": "continue", "text": "Which appointment time?"}

    monkeypatch.setattr("agent_platform.calling.invoke", handler)
    assert (
        client.post(f"/v1/calling/agents/{a['id']}/activation", headers=h, json={"enabled": True}).status_code
        == 200
    )
    return handler


def test_owner_isolation_draft_and_setup(monkeypatch):
    with environment(monkeypatch) as (c, _, key, _):
        h, a, _cfg, body = fixture(c, key, monkeypatch)
        assert c.post("/v1/calling/agents", headers=h, json=body).json()["id"] == a["id"]
        wrong = {**h, "X-External-User-ID": "someone-else"}
        assert c.get(f"/v1/calling/agents/{a['id']}/setup", headers=wrong).status_code == 404
        assert (
            c.post("/v1/calling/agents", headers=wrong, json={**body, "external_id": "stolen"}).status_code
            == 409
        )
        preview = c.post(f"/v1/calling/agents/{a['id']}/preview", headers=h).json()
        assert preview["simulation"] and not preview["provider_called"]
        assert (
            c.get(f"/v1/calling/agents/{a['id']}/setup", headers=h)
            .json()["webhooks"]["inbound"]
            .startswith("https://voice.example/")
        )
        # Draft creation does not claim a live connection.
        draft = c.post(
            "/v1/calling/agents", headers=h, json={**body, "external_id": "draft", "connection_ref": None}
        ).json()
        assert (
            c.post(
                f"/v1/calling/agents/{draft['id']}/activation", headers=h, json={"enabled": True}
            ).status_code
            == 409
        )


def test_call_turn_replay_status_and_pause(monkeypatch):
    with environment(monkeypatch) as (c, _, key, _):
        h, a, cfg, _ = fixture(c, key, monkeypatch)
        handler = enable(c, h, a, monkeypatch)
        calls = []

        async def counted(cfg, p):
            calls.append(p)
            return await handler(cfg, p)

        monkeypatch.setattr("agent_platform.calling.invoke", counted)
        assert "<Gather" in post(c, a, cfg, "inbound").text
        first = post(c, a, cfg, "turn/1", SpeechResult="I want an appointment")
        assert first.status_code == 200 and "Which appointment time?" in first.text
        assert post(c, a, cfg, "turn/1", SpeechResult="I want an appointment").text == first.text
        assert len(calls) == 1 and calls[0]["input_trust"] == "untrusted_caller_data"
        assert post(c, a, cfg, "turn/1", SpeechResult="changed").status_code == 409
        c.post(f"/v1/calling/agents/{a['id']}/activation", headers=h, json={"enabled": False})
        assert "<Hangup" in post(c, a, cfg, "turn/2", Digits="1").text
        assert len(calls) == 1
        assert (
            post(
                c, a, cfg, "status", SequenceNumber="2", CallDuration="23", CallStatus="completed"
            ).status_code
            == 200
        )
        post(c, a, cfg, "status", SequenceNumber="1", CallDuration="2", CallStatus="failed")
        history = c.get(f"/v1/calling/agents/{a['id']}/calls", headers=h).json()["calls"]
        assert len(history) == 1 and history[0]["duration_seconds"] == 23
        assert "caller" not in history[0]


def test_uncertain_handler_is_never_reinvoked(monkeypatch):
    with environment(monkeypatch) as (c, _, key, _):
        h, a, cfg, _ = fixture(c, key, monkeypatch)
        enable(c, h, a, monkeypatch)
        post(c, a, cfg, "inbound")
        calls = []

        async def fail(cfg, p):
            calls.append(p)
            raise TimeoutError("uncertain side effect")

        monkeypatch.setattr("agent_platform.calling.invoke", fail)
        first = post(c, a, cfg, "turn/1", SpeechResult="yes")
        assert "<Hangup" in first.text
        assert post(c, a, cfg, "turn/1", SpeechResult="yes").text == first.text
        assert len(calls) == 1
        assert (
            sql(
                "SELECT state FROM calling_turns WHERE call_id IN (SELECT id FROM calling_calls WHERE agent_id=%s)",
                (a["id"],),
            )[0]["state"]
            == "UNKNOWN"
        )


def test_forgery_wrong_account_and_xml_escape(monkeypatch):
    with environment(monkeypatch) as (c, _, key, _):
        h, a, cfg, _ = fixture(c, key, monkeypatch)
        enable(c, h, a, monkeypatch)
        assert c.post(f"/webhooks/calling/{a['id']}/inbound", content="CallSid=fake").status_code == 403
        assert post(c, a, cfg, "inbound", AccountSid="AC" + "c" * 32).status_code == 403
        assert post(c, a, cfg, "inbound", To="+15559999999").status_code == 403
        assert post(c, a, cfg, "inbound", Direction="outbound-api").status_code == 403
        assert not c.get(f"/v1/calling/agents/{a['id']}/calls", headers=h).json()["calls"]
    xml = xml_reply("<Dial>attacker</Dial>", "en-IN")
    assert "&lt;Dial&gt;" in xml and "<Dial>" not in xml


def test_twilio_documented_signature_vector():
    params = {
        "CallSid": "CA1234567890ABCDE",
        "Caller": "+14158675310",
        "Digits": "1234",
        "From": "+14158675310",
        "To": "+18005551212",
    }
    # https://www.twilio.com/docs/usage/security: standard HMAC-SHA1 validation.
    assert (
        signature("https://example.com/myapp.php?foo=1&bar=2", params, "12345")
        == "L/OH5YylLD5NRKLltdqwSvS0BnU="
    )


def test_terminal_event_before_inbound_cannot_reopen_call(monkeypatch):
    with environment(monkeypatch) as (c, _, key, _):
        h, a, cfg, _ = fixture(c, key, monkeypatch)
        enable(c, h, a, monkeypatch)
        assert (
            post(
                c, a, cfg, "status", SequenceNumber="1", CallDuration="3", CallStatus="completed"
            ).status_code
            == 200
        )
        assert "<Hangup" in post(c, a, cfg, "inbound").text
        assert "<Hangup" in post(c, a, cfg, "turn/1", SpeechResult="yes").text
        assert not sql(
            "SELECT * FROM calling_turns WHERE call_id IN (SELECT id FROM calling_calls WHERE agent_id=%s)",
            (a["id"],),
        )


def test_interrupted_turn_is_fenced_after_restart(monkeypatch):
    with environment(monkeypatch) as (c, _, key, _):
        h, a, cfg, _ = fixture(c, key, monkeypatch)
        enable(c, h, a, monkeypatch)
        post(c, a, cfg, "inbound")
        call = sql("SELECT id FROM calling_calls WHERE agent_id=%s", (a["id"],))[0]
        import hashlib

        sql(
            "INSERT INTO calling_turns(id,call_id,turn,request_hash,state) VALUES(%s,%s,1,%s,'PROCESSING')",
            (uuid4(), call["id"], hashlib.sha256(b"yes").hexdigest()),
        )
        sql("UPDATE calling_calls SET last_turn=1 WHERE id=%s", (call["id"],))

        async def must_not_run(*_):
            raise AssertionError("Interrupted business action must never be retried")

        monkeypatch.setattr("agent_platform.calling.invoke", must_not_run)
        assert "<Hangup" in post(c, a, cfg, "turn/1", SpeechResult="yes").text
        assert "<Hangup" in post(c, a, cfg, "turn/2", SpeechResult="next").text


def test_paused_draft_can_attach_verified_connection(monkeypatch):
    with environment(monkeypatch) as (c, _, key, _):
        h, a, _cfg, body = fixture(c, key, monkeypatch)
        changed = {**body, "greeting": "Welcome to our automated reception."}
        r = c.put(f"/v1/calling/agents/{a['id']}", headers=h, json=changed)
        assert r.status_code == 200 and r.json()["greeting"] == changed["greeting"]
        enable(c, h, a, monkeypatch)
        assert c.put(f"/v1/calling/agents/{a['id']}", headers=h, json=body).status_code == 409


def test_activation_checks_real_provider_routing(monkeypatch):
    with environment(monkeypatch) as (c, _, key, _):
        h, a, _, _ = fixture(c, key, monkeypatch)
        async def no_number(*args, **kwargs):
            return {"ready": False, "code": "number_missing", "message": "Incoming number not connected"}
        monkeypatch.setattr("agent_platform.calling.provider_readiness", no_number)
        ready = c.get(f"/v1/calling/agents/{a['id']}/readiness", headers=h)
        assert ready.status_code == 200 and ready.json()["code"] == "number_missing"
        assert c.post(f"/v1/calling/agents/{a['id']}/activation", headers=h, json={"enabled": True}).status_code == 409
        assert c.get(f"/v1/calling/agents/{a['id']}/setup", headers=h).json()["enabled"] is False
        assert c.post(f"/v1/calling/agents/{a['id']}/activation", headers=h, json={"enabled": False}).status_code == 200
