"""App-owned calling: identity, carrier gating, durable turns, quota races and revocation."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from psycopg.errors import UniqueViolation
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from test_task_agents import database, environment, sql

from agent_platform import telephony
from agent_platform.service_identity import provision


def setup(c, h, key, monkeypatch, *, external="gateway", number="+15551234567"):
    monkeypatch.setattr(telephony, "voice_gateway", lambda: object())
    tenant = sql("SELECT tenant_id FROM platform_apps WHERE id=%s", (key["app_id"],))[0]["tenant_id"]

    async def create_key():
        async with AsyncConnectionPool(database(), open=False, kwargs={"row_factory": dict_row}) as pool:
            return await provision(
                pool,
                tenant=tenant,
                organisation="Fixture",
                app_name="Fixture",
                app_id=key["app_id"],
                scopes=["telephony:read", "telephony:write"],
            )

    k = asyncio.run(create_key())
    headers = {**h, "Authorization": "Bearer " + k["credential"]}
    a = c.post(
        "/v1/voice/agents",
        headers=h,
        json={"external_id": external, "name": "Reception", "greeting": "Hello", "documents": []},
    )
    assert a.status_code == 201, a.text
    agent = a.json()["id"]
    r = c.post(
        "/v1/telephony/connections",
        headers=headers,
        json={"external_id": external, "agent_id": agent, "number": number},
    )
    assert r.status_code == 201, r.text
    row = r.json()
    assert r.headers["cache-control"] == "no-store"
    return headers, row, {"Authorization": "Bearer " + row["gateway_key"]}


def verify(ident, verified=True):
    async def run():
        async with AsyncConnectionPool(database(), open=False, kwargs={"row_factory": dict_row}) as pool:
            await telephony.verify_carrier(
                pool, ident, "fixture-carrier-ticket", actor="test-operator", verified=verified
            )

    asyncio.run(run())


def activate(c, h, r, g):
    verify(r["id"])
    assert c.post(f"/v1/telephony/gateway/{r['id']}/heartbeat", headers=g).status_code == 200
    v = c.patch(f"/v1/telephony/connections/{r['id']}", headers=h, json={"enabled": True})
    assert v.status_code == 200, v.text


def start(c, r, g, call=None):
    call = call or str(uuid4())
    result = c.post(
        f"/v1/telephony/gateway/{r['id']}/calls", headers=g, json={"call_id": call, "number": r["number"]}
    )
    return call, result


def test_number_alone_does_not_activate_and_scopes_isolate(monkeypatch):
    with environment(monkeypatch) as (c, old, key, _):
        h, r, g = setup(c, old, key, monkeypatch)
        base = f"/v1/telephony/connections/{r['id']}"
        assert c.get(base + "/readiness", headers=old).status_code == 403
        ready = c.get(base + "/readiness", headers=h).json()
        assert set(ready["blockers"]) == {"carrier_authorization_required", "gateway_offline"}
        assert not ready["accepting_calls"]
        assert c.patch(base, headers=h, json={"enabled": True}).status_code == 409
        assert c.patch(base, headers=h, json={"enabled": True, "verified": True}).status_code == 422
        assert start(c, r, g)[1].status_code == 409
        c.post(f"/v1/telephony/gateway/{r['id']}/heartbeat", headers=g)
        assert c.patch(base, headers=h, json={"enabled": True}).status_code == 409
        assert "gateway_key" not in c.get("/v1/telephony/connections", headers=h).text
        assert "gateway_hash" not in c.get(base + "/readiness", headers=h).text
        assert c.get(base + "/readiness", headers={**h, "X-External-User-ID": "other"}).status_code == 404
        activate(c, h, r, g)
        assert c.get(base + "/readiness", headers=h).json()["accepting_calls"]
        bad = c.post(
            f"/v1/telephony/gateway/{r['id']}/calls",
            headers=g,
            json={"call_id": str(uuid4()), "number": "+15557654321"},
        )
        assert bad.status_code == 403
        assert c.get(base + "/audit", headers=h).json()["events"][-1]["event"] == "activated"


def test_two_apps_and_owners_cannot_cross_route(monkeypatch):
    with environment(monkeypatch) as (c, old, key, _), environment(monkeypatch) as (_, old2, key2, _):
        h, r, g = setup(c, old, key, monkeypatch)
        h2, r2, g2 = setup(c, old2, key2, monkeypatch, number="+15557654321")
        activate(c, h, r, g)
        activate(c, h2, r2, g2)
        for suffix in ("readiness", "calls", "audit"):
            assert c.get(f"/v1/telephony/connections/{r['id']}/{suffix}", headers=h2).status_code == 404
        assert c.post(f"/v1/telephony/gateway/{r['id']}/heartbeat", headers=g2).status_code == 401
        wrong = c.post(
            "/v1/telephony/connections",
            headers=h2,
            json={"external_id": "steal", "agent_id": r["agent_id"], "number": r["number"]},
        )
        assert wrong.status_code == 404
        a, res = start(c, r, g)
        assert res.status_code == 201
        assert c.get(f"/v1/telephony/gateway/{r2['id']}/calls/{a}", headers=g2).status_code == 404
        _, res2 = start(c, r2, g2)
        assert res2.status_code == 201
        assert len(c.get("/v1/telephony/connections", headers=h2).json()["connections"]) == 1


def test_turn_idempotency_server_history_and_unknown_never_replayed(monkeypatch):
    seen = []

    async def respond(pool, gateway, agent, user, text, request_key, history):
        seen.append((text, history))
        if text == "fail":
            raise TimeoutError()
        return {"action": "continue", "text": "Hours are 9 to 5."}

    monkeypatch.setattr(telephony.voice_conversation, "respond", respond)
    with environment(monkeypatch) as (c, old, key, _):
        h, r, g = setup(c, old, key, monkeypatch)
        activate(c, h, r, g)
        call, res = start(c, r, g)
        assert res.status_code == 201
        assert start(c, r, g, call)[1].status_code == 409
        base = f"/v1/telephony/gateway/{r['id']}/calls/{call}"
        assert c.post(base + "/turns/2", headers=g, json={"text": "hours"}).status_code == 409
        result = c.post(base + "/turns/1", headers=g, json={"text": "hours"})
        assert result.status_code == 200, result.text
        assert c.post(base + "/turns/1", headers=g, json={"text": "hours"}).json() == result.json()
        assert len(seen) == 1
        assert c.post(base + "/turns/1", headers=g, json={"text": "changed"}).status_code == 409
        assert c.post(base + "/turns/2", headers=g, json={"text": "fail"}).status_code == 503
        assert len(seen[1][1]) == 2
        assert c.post(base + "/turns/2", headers=g, json={"text": "fail"}).status_code == 409
        assert c.post(base + "/turns/3", headers=g, json={"text": "new"}).status_code == 409
        assert len(seen) == 2
        assert c.post(base + "/end", headers=g, json={"state": "ENDED"}).json()["state"] == "ENDED"
        assert c.post(base + "/end", headers=g, json={"state": "FAILED"}).json()["state"] == "ENDED"


def test_app_quota_across_connections_and_expiration(monkeypatch):
    with environment(monkeypatch) as (c, old, key, _):
        h, r, g = setup(c, old, key, monkeypatch)
        h2, r2, g2 = setup(c, old, key, monkeypatch, external="second", number="+15557654321")
        activate(c, h, r, g)
        activate(c, h2, r2, g2)
        sql(
            "UPDATE telephony_app_limits SET concurrent_calls=1,daily_calls=2 WHERE app_id=%s",
            (key["app_id"],),
        )
        with ThreadPoolExecutor(2) as pool:
            futures = [pool.submit(start, c, row, cred) for row, cred in ((r, g), (r2, g2))]
            results = [f.result() for f in futures]
        assert sorted(res.status_code for _, res in results) == [201, 429]
        sql(
            "UPDATE telephony_sessions SET expires_at=now()-interval '1 second' WHERE app_id=%s",
            (key["app_id"],),
        )
        assert start(c, r, g)[1].status_code == 201
        sql(
            "UPDATE telephony_sessions SET expires_at=now()-interval '1 second' WHERE app_id=%s",
            (key["app_id"],),
        )
        assert start(c, r, g)[1].status_code == 429


def test_key_rotation_and_carrier_revocation_stop_calls(monkeypatch):
    with environment(monkeypatch) as (c, old, key, _):
        h, r, g = setup(c, old, key, monkeypatch)
        activate(c, h, r, g)
        call, res = start(c, r, g)
        assert res.status_code == 201
        new = c.post(f"/v1/telephony/connections/{r['id']}/rotate-key", headers=h)
        assert new.status_code == 200
        assert c.post(f"/v1/telephony/gateway/{r['id']}/heartbeat", headers=g).status_code == 401
        ng = {"Authorization": "Bearer " + new.json()["gateway_key"]}
        assert c.get(f"/v1/telephony/gateway/{r['id']}/calls/{call}", headers=ng).json()["state"] == "UNKNOWN"
        assert start(c, r, ng)[1].status_code == 409
        activate(c, h, r, ng)
        verify(r["id"], False)
        assert start(c, r, ng)[1].status_code == 409


def test_verified_number_cannot_be_claimed_twice(monkeypatch):
    with environment(monkeypatch) as (c, old, key, _):
        _, r, _ = setup(c, old, key, monkeypatch)
        _, r2, _ = setup(c, old, key, monkeypatch, external="duplicate")
        verify(r["id"])
        with pytest.raises(UniqueViolation):
            verify(r2["id"])


def test_paused_during_model_work_never_releases_reply(monkeypatch):
    async def respond(pool, gateway, agent, user, text, request_key, history):
        async with pool.connection() as conn:
            await conn.execute(
                "UPDATE telephony_connections SET enabled=false WHERE agent_id=%s", (agent["id"],)
            )
        return {"action": "continue", "text": "Should be blocked"}

    monkeypatch.setattr(telephony.voice_conversation, "respond", respond)
    with environment(monkeypatch) as (c, old, key, _):
        h, r, g = setup(c, old, key, monkeypatch)
        activate(c, h, r, g)
        call, _ = start(c, r, g)
        result = c.post(
            f"/v1/telephony/gateway/{r['id']}/calls/{call}/turns/1", headers=g, json={"text": "hi"}
        )
        assert result.status_code == 409
        assert "Should be blocked" not in result.text


def test_bridge_runs_against_platform_api_and_persists_call(monkeypatch):
    import httpx

    from agent_platform.asterisk_bridge import Agent

    async def answer(pool, gateway, agent, user, text, request_key, history):
        return {"action": "continue", "text": "The business opens at 9."}

    monkeypatch.setattr(telephony.voice_conversation, "respond", answer)
    with environment(monkeypatch) as (c, old, key, _):
        h, r, g = setup(c, old, key, monkeypatch)
        activate(c, h, r, g)

        async def run():
            # Route the actual bridge client's HTTP requests through the live test API event loop.
            def send(request):
                return c.request(
                    request.method, request.url.path, headers=dict(request.headers), content=request.content
                )

            async def transport(request):
                response = await asyncio.to_thread(send, request)
                return httpx.Response(
                    response.status_code, content=response.content, headers=response.headers
                )

            agent = Agent(
                {
                    "platform_url": "https://platform.example",
                    "connection_id": r["id"],
                    "gateway_key": r["gateway_key"],
                    "number": r["number"],
                }
            )
            await agent.client.aclose()
            agent.client = httpx.AsyncClient(transport=httpx.MockTransport(transport))
            try:
                call = uuid4()
                assert (await agent.begin(call))["state"] == "ACTIVE"
                assert (await agent.reply(call, 1, "hours?", []))["text"] == "The business opens at 9."
                await agent.end(call, "ENDED")
            finally:
                await agent.client.aclose()

        asyncio.run(run())
        record = c.get(f"/v1/telephony/connections/{r['id']}/calls", headers=h).json()["calls"][0]
        assert record["state"] == "ENDED" and record["turns"] == 1


def test_operator_ui_and_proxy_remain_local_and_gateway_routes_hidden(monkeypatch):
    from fastapi.testclient import TestClient

    from agent_platform.app import create_app
    from agent_platform.console_app import create_console

    monkeypatch.setenv("AI_PLATFORM_DATABASE_URL", database())
    with TestClient(create_console(app=create_app(signing_key="z" * 48))) as c:
        assert c.get("/telephony").status_code == 200
        assert c.get("/console/telephony.js").status_code == 200
        assert c.get("/telephony", headers={"Host": "public.example"}).status_code == 403
        h = {"X-Console-Intent": "user-action"}
        assert c.get("/console/api/telephony/connections", headers=h).status_code == 200
        assert c.get("/console/api/telephony/connections").status_code == 403
        assert c.post(f"/console/api/telephony/gateway/{uuid4()}/heartbeat", headers=h).status_code == 404
