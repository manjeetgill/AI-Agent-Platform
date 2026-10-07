import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from test_knowledge import Model, cleanup, credentials
from test_task_agents import database, environment, sql

from agent_platform import knowledge
from agent_platform.agent_memory import retrieve


def grant(key):
    sql(
        "UPDATE platform_service_keys SET scopes=scopes || %s::text[] WHERE id=%s",
        (["memory:read", "memory:write", "memory:review"], key["key_id"]),
    )


def candidate(client, headers, **changes):
    body = {
        "request_key": str(uuid4()),
        "kind": "FACT",
        "content": "Refunds are available within thirty days.",
        "source_type": "MODEL",
        "source_ref": "answer:fixture",
        "confidence": 0.9,
        "expires_at": (datetime.now(UTC) + timedelta(days=30)).isoformat(),
        **changes,
    }
    r = client.post("/v1/memory/candidates", headers=headers, json=body)
    assert r.status_code == 201, r.text
    return r.json(), body


def review(client, headers, row, decision="VALIDATED", visibility="private"):
    return client.post(
        "/v1/memory/records/" + row["id"] + "/review",
        headers=headers,
        json={
            "expected_version": row["version"],
            "decision": decision,
            "method": "SOURCE_REVIEW",
            "evidence_ref": "reviewed-business-policy:v1",
            "visibility": visibility,
        },
    )


def search(client, headers):
    r = client.get("/v1/memory/search?q=Refunds", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()["items"]


def test_review_isolation_expiry_and_deletion(monkeypatch):
    with environment(monkeypatch) as (client, h, key, _):
        assert client.get("/v1/memory/search?q=refunds", headers=h).status_code == 403
        grant(key)
        row, body = candidate(client, h)
        assert not search(client, h)
        assert client.post("/v1/memory/candidates", headers=h, json=body).json()["id"] == row["id"]
        assert (
            client.post("/v1/memory/candidates", headers=h, json={**body, "content": "changed"}).status_code
            == 409
        )
        other = {**h, "X-External-User-ID": "another-user"}
        assert review(client, other, row).status_code == 404
        sql(
            "UPDATE platform_service_keys SET scopes=array_remove(scopes,%s) WHERE id=%s",
            ("memory:review", key["key_id"]),
        )
        assert review(client, h, row).status_code == 403
        sql(
            "UPDATE platform_service_keys SET scopes=array_append(scopes,%s) WHERE id=%s",
            ("memory:review", key["key_id"]),
        )
        approved = review(client, h, row).json()
        assert len(search(client, h)) == 1 and not search(client, other)
        assert review(client, h, row).status_code == 409
        assert review(client, h, approved, "REVOKED").status_code == 200
        assert not search(client, h)
        shared, _ = candidate(client, h)
        approved = review(client, h, shared, visibility="app").json()
        assert len(search(client, other)) == 1

        async def cross():
            async with AsyncConnectionPool(database(), open=False, kwargs={"row_factory": dict_row}) as pool:
                return await retrieve(pool, uuid4(), uuid4(), "Refunds")

        assert asyncio.run(cross()) == []
        sql("UPDATE platform_memories SET expires_at=now()-interval '1 second' WHERE id=%s", (shared["id"],))
        assert not search(client, h)
        assert client.delete("/v1/memory/records/" + shared["id"], headers=other).status_code == 404
        assert client.delete("/v1/memory/records/" + shared["id"], headers=h).status_code == 204
        assert not sql("SELECT 1 FROM platform_memories WHERE id=%s", (shared["id"],))
        events = client.get("/v1/memory/events", headers=h).json()["items"]
        assert any(e["event"] == "DELETED" for e in events)
        assert all("content" not in e["details"] for e in events)
        assert client.get("/v1/memory/records", headers=other).json()["items"] == []


def test_memory_grounding_and_cache_revocation(monkeypatch):
    with environment(monkeypatch) as (client, h, key, _):
        grant(key)
        kh = credentials(key)
        model = Model()
        monkeypatch.setattr(knowledge, "gateway", lambda: model)
        try:
            row, _ = candidate(client, h)
            approved = review(client, h, row).json()
            body = {"question": "Refunds", "idempotency_key": "memory-grounding-test"}
            r = client.post("/v1/knowledge/ask", headers=kh, json=body)
            assert r.status_code == 200, r.text
            assert r.json()["citations"][0]["memory_id"] == row["id"]
            assert model.calls == 1
            assert client.post("/v1/knowledge/ask", headers=kh, json=body).status_code == 200
            assert model.calls == 1
            assert review(client, h, approved, "REVOKED").status_code == 200
            assert client.post("/v1/knowledge/ask", headers=kh, json=body).status_code == 409
            assert model.calls == 1
        finally:
            cleanup(key)


def test_shared_channel_context_excludes_private_memories(monkeypatch):
    with environment(monkeypatch) as (client, h, key, _):
        grant(key)
        row, _ = candidate(client, h)
        review(client, h, row)
        owner = sql(
            "SELECT owner_id FROM platform_external_users WHERE app_id=%s AND external_id=%s",
            (key["app_id"], "fixture-user"),
        )[0]["owner_id"]

        async def check():
            async with AsyncConnectionPool(database(), open=False, kwargs={"row_factory": dict_row}) as pool:
                assert len(await retrieve(pool, key["app_id"], owner, "Refunds")) == 1
                assert await retrieve(pool, key["app_id"], owner, "Refunds", shared_only=True) == []

        asyncio.run(check())


def test_checkpoint_conflicts_feedback_and_automatic_handoff(monkeypatch):
    with environment(monkeypatch) as (client, h, key, _):
        grant(key)
        r = client.post(
            "/v1/tasks",
            headers=h,
            json={"request": "Research the refund policy", "idempotency_key": "checkpoint-task-key"},
        )
        assert r.status_code == 201, r.text
        task = r.json()
        tid = task["id"]
        read = client.get("/v1/memory/tasks/" + tid + "/checkpoint", headers=h)
        assert read.status_code == 200, read.text
        assert read.json()["execution"]["event"] == "CREATED"
        body = {
            "expected_revision": 0,
            "expected_task_revision": task["revision"],
            "expected_event_id": task["events"][0]["id"],
            "goal": "Explain refunds",
            "decisions": [{"decision": "Use the policy", "reason": "Authoritative source"}],
            "open_questions": ["Has the policy changed?"],
            "next_action": "Check current policy",
        }
        r = client.put("/v1/memory/tasks/" + tid + "/checkpoint", headers=h, json=body)
        assert r.status_code == 200, r.text
        assert client.put("/v1/memory/tasks/" + tid + "/checkpoint", headers=h, json=body).status_code == 409
        other = {**h, "X-External-User-ID": "another-user"}
        assert client.get("/v1/memory/tasks/" + tid + "/checkpoint", headers=other).status_code == 404
        sql("INSERT INTO chore_events(task_id,event) VALUES(%s,'RUNNING')", (tid,))
        assert client.get("/v1/memory/tasks/" + tid + "/checkpoint", headers=h).json()["stale"]
        fb = {
            "request_key": "feedback-request",
            "target_type": "task",
            "target_id": tid,
            "outcome": "UNKNOWN",
            "evidence_ref": "manual-review:pending",
            "note": "Awaiting confirmation",
        }
        a = client.post("/v1/memory/feedback", headers=h, json=fb)
        assert a.status_code == 201, a.text
        assert a.json()["training_allowed"] is False
        assert client.post("/v1/memory/feedback", headers=h, json=fb).json()["id"] == a.json()["id"]
        assert client.post("/v1/memory/feedback", headers=other, json=fb).status_code == 404
        assert (
            client.post(
                "/v1/memory/feedback", headers=h, json={**fb, "outcome": "CONFIRMED_SUCCESS"}
            ).status_code
            == 409
        )
        listing = client.get("/v1/memory/feedback", headers=h).json()
        assert [item["id"] for item in listing["items"]] == [a.json()["id"]]
        assert listing["training_allowed"] is False
        assert client.get("/v1/memory/feedback", headers=other).json()["items"] == []
        events = client.get("/v1/memory/events", headers=h).json()["items"]
        assert len([e for e in events if e["event"] == "FEEDBACK_RECORDED"]) == 1


@pytest.mark.parametrize("revoke", [False, True])
def test_whatsapp_rechecks_memory_before_dispatch(monkeypatch, revoke):
    from test_whatsapp import clean, inbound, rows, setup, work

    class MemoryModel:
        async def structured(self, task, policy, data, schema, validator):
            source = next(x for x in data["passages"] if "memory_id" in x)
            return (
                validator(schema(claims=[{"text": source["text"], "sources": [source["id"]]}], missing=[])),
                10,
                "fixture",
                "fixture",
            )

    with environment(monkeypatch) as (client, h, key, _):
        try:
            grant(key)
            row, _ = candidate(client, h)
            approved = review(client, h, row, visibility="app").json()
            _, channel, _ = setup(client, key, monkeypatch)
            assert inbound(client, channel, text="Refunds").status_code == 200
            assert work(MemoryModel())
            assert rows(channel)[0]["status"] == "READY"
            if revoke:
                assert review(client, h, approved, "REVOKED").status_code == 200
            sent = []

            async def send(*args):
                sent.append(args)
                return uuid4().hex

            assert work(sender=send)
            assert rows(channel)[0]["status"] == ("HANDOFF" if revoke else "SENT")
            assert len(sent) == (0 if revoke else 1)
        finally:
            clean(key)
