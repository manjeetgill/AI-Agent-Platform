"""Signed inbound flow, isolation, handover and uncertain-send fences against real Postgres."""

import asyncio
import hashlib
import hmac
import json
import time
from uuid import uuid4

import pytest
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from test_knowledge import Model
from test_task_agents import database, environment, sql

from agent_platform.service_identity import provision
from agent_platform.whatsapp_worker import tick


def setup(client, key, monkeypatch):
    # Resolve tenant before entering async code (sql uses asyncio.run).
    tenant = sql("SELECT tenant_id FROM platform_apps WHERE id=%s", (key["app_id"],))[0]["tenant_id"]

    async def credentials():
        async with AsyncConnectionPool(database(), open=False, kwargs={"row_factory": dict_row}) as pool:
            return await provision(
                pool,
                tenant=tenant,
                organisation="test",
                app_name="test",
                app_id=key["app_id"],
                scopes=[
                    "channels:read",
                    "channels:write",
                    "knowledge:read",
                    "knowledge:write",
                    "knowledge:publish",
                ],
            )

    credential = asyncio.run(credentials())
    headers = {"Authorization": "Bearer " + credential["credential"], "X-External-User-ID": "operator"}
    phone = str(uuid4().int)[:20]
    monkeypatch.setenv(
        "AI_WHATSAPP_CONNECTIONS",
        json.dumps(
            {
                phone: {
                    "app_id": key["app_id"],
                    "app_secret": "fixture-secret",
                    "verify_token": "fixture-verify",
                    "access_token": "fixture-access",
                    "graph_version": "v23.0",
                }
            }
        ),
    )
    doc = client.post(
        "/v1/knowledge/documents",
        headers=headers,
        json={
            "external_id": "refunds",
            "title": "Refund policy",
            "text": "Refunds are available within thirty days.",
            "visibility": "app",
        },
    ).json()
    result = client.post(
        "/v1/whatsapp/channels",
        headers=headers,
        json={"name": "Support", "phone_id": phone, "documents": [doc["id"]]},
    )
    assert result.status_code == 201, result.text
    channel = result.json()
    client.get(
        "/webhooks/whatsapp/" + channel["id"],
        params={"hub.mode": "subscribe", "hub.verify_token": "fixture-verify", "hub.challenge": "ok"},
    )
    sql(
        "INSERT INTO whatsapp_worker_health VALUES('worker',now(),true) ON CONFLICT(id) DO UPDATE SET last_seen=now(),model_configured=true"
    )
    assert (
        client.post(
            f"/v1/whatsapp/channels/{channel['id']}/activation", headers=headers, json={"enabled": True}
        ).status_code
        == 200
    )
    return headers, channel, doc


def inbound(client, channel, text="What is the refund policy?", mid=None, age=0, signature=True):
    value = {
        "metadata": {"phone_number_id": channel["phone_id"]},
        "messages": [
            {
                "id": mid or uuid4().hex,
                "from": "919999000001",
                "timestamp": str(int(time.time()) - age),
                "type": "text",
                "text": {"body": text},
            }
        ],
    }
    raw = json.dumps(
        {"object": "whatsapp_business_account", "entry": [{"changes": [{"value": value}]}]}
    ).encode()
    sig = "sha256=" + hmac.new(b"fixture-secret", raw, hashlib.sha256).hexdigest() if signature else "bad"
    return client.post(
        "/webhooks/whatsapp/" + channel["id"], content=raw, headers={"X-Hub-Signature-256": sig}
    )


def work(model=None, sender=None):
    async def run():
        async with AsyncConnectionPool(database(), open=False, kwargs={"row_factory": dict_row}) as pool:
            return await tick(pool, model or Model(), **({"sender": sender} if sender else {}))

    return asyncio.run(run())


def rows(channel):
    return sql(
        "SELECT m.* FROM whatsapp_messages m JOIN whatsapp_conversations c ON c.id=m.conversation_id WHERE c.channel_id=%s",
        (channel["id"],),
    )


def clean(key):
    sql("DELETE FROM knowledge_answers WHERE app_id=%s", (key["app_id"],))
    sql("DELETE FROM knowledge_documents WHERE app_id=%s", (key["app_id"],))
    sql("DELETE FROM whatsapp_dispatches WHERE message_id NOT IN (SELECT id FROM whatsapp_messages)")


def test_inbound_dedup_answer_and_send(monkeypatch):
    with environment(monkeypatch) as (client, _, key, _):
        try:
            headers, channel, _ = setup(client, key, monkeypatch)
            assert inbound(client, channel, signature=False).status_code == 403
            assert rows(channel) == []
            assert inbound(client, channel, mid="same").status_code == 200
            assert inbound(client, channel, mid="same").status_code == 200
            assert len(rows(channel)) == 1
            model = Model()
            assert work(model)
            assert rows(channel)[0]["status"] == "READY"
            sent = []

            async def send(*args):
                sent.append(args)
                return uuid4().hex

            assert work(sender=send)
            assert rows(channel)[0]["status"] == "SENT"
            assert len(sent) == 1 and model.calls == 1
            assert not work(sender=send)
            other = {**headers, "X-External-User-ID": "other"}
            assert (
                client.get(f"/v1/whatsapp/channels/{channel['id']}/conversations", headers=other).status_code
                == 404
            )
        finally:
            clean(key)


@pytest.mark.parametrize("crash_fence", [False, True])
def test_uncertain_send_never_repeats(monkeypatch, crash_fence):
    with environment(monkeypatch) as (client, _, key, _):
        try:
            _, channel, _ = setup(client, key, monkeypatch)
            inbound(client, channel)
            work()
            msg = rows(channel)[0]
            if crash_fence:
                sql("INSERT INTO whatsapp_dispatches(message_id) VALUES(%s)", (msg["id"],))
            sends = []

            async def lost(*args):
                sends.append(args)
                raise TimeoutError()

            work(sender=lost)
            assert rows(channel)[0]["status"] == "UNKNOWN"
            assert not work(sender=lost)
            assert len(sends) == (0 if crash_fence else 1)
        finally:
            clean(key)


def test_takeover_manual_reply_and_resume(monkeypatch):
    with environment(monkeypatch) as (client, _, key, _):
        try:
            headers, channel, _ = setup(client, key, monkeypatch)
            inbound(client, channel)
            work()
            msg = rows(channel)[0]
            path = f"/v1/whatsapp/channels/{channel['id']}/conversations/{msg['conversation_id']}/mode"
            assert client.post(path, headers=headers, json={"mode": "HUMAN"}).status_code == 200
            assert rows(channel)[0]["status"] == "CANCELLED"
            assert not work()
            assert (
                client.post(
                    f"/v1/whatsapp/channels/{channel['id']}/activation",
                    headers=headers,
                    json={"enabled": False},
                ).status_code
                == 200
            )
            reply = f"/v1/whatsapp/channels/{channel['id']}/messages/{msg['id']}/reply"
            assert client.post(reply, headers=headers, json={"text": "A human response"}).status_code == 200
            assert client.post(reply, headers=headers, json={"text": "A human response"}).status_code == 200

            async def sent(*args):
                assert args[-1] == "A human response"
                return uuid4().hex

            work(sender=sent)
            assert rows(channel)[0]["status"] == "SENT"
            assert client.post(path, headers=headers, json={"mode": "AUTO"}).status_code == 200
            assert not work(sender=sent)
        finally:
            clean(key)


@pytest.mark.parametrize("case", ["expired", "no_evidence", "unpublished"])
def test_no_unsafe_reply(monkeypatch, case):
    with environment(monkeypatch) as (client, _, key, _):
        try:
            _, channel, doc = setup(client, key, monkeypatch)
            inbound(
                client,
                channel,
                text="unmatched zebras" if case == "no_evidence" else "refund policy",
                age=86400 if case == "expired" else 0,
            )
            work()
            if case == "unpublished":
                sql("UPDATE knowledge_documents SET visibility='private' WHERE id=%s", (doc["id"],))
                work()
            assert rows(channel)[0]["status"] in {"HANDOFF", "CANCELLED"}
        finally:
            clean(key)


def test_concurrent_workers_and_document_allowlist(monkeypatch):
    with environment(monkeypatch) as (client, _, key, _):
        try:
            headers, channel, doc = setup(client, key, monkeypatch)
            client.post(
                "/v1/knowledge/documents",
                headers=headers,
                json={
                    "external_id": "internal",
                    "title": "Unselected",
                    "text": "Refund policy confidential unselected details.",
                    "visibility": "app",
                },
            )
            inbound(client, channel)

            class ScopedModel(Model):
                async def structured(self, task, policy, data, schema, validator):
                    assert all(p["document_id"] == doc["id"] for p in data["passages"])
                    await asyncio.sleep(0.02)
                    return await super().structured(task, policy, data, schema, validator)

            model = ScopedModel()
            sends = []

            async def sender(*args):
                sends.append(args)
                return uuid4().hex

            async def concurrent():
                async with AsyncConnectionPool(
                    database(), open=False, kwargs={"row_factory": dict_row}
                ) as pool:
                    await asyncio.gather(tick(pool, model, sender), tick(pool, model, sender))
                    await asyncio.gather(tick(pool, model, sender), tick(pool, model, sender))

            asyncio.run(concurrent())
            assert model.calls == 1 and len(sends) == 1
            assert rows(channel)[0]["status"] == "SENT"
        finally:
            clean(key)


def test_verification_and_delivery_receipts(monkeypatch):
    with environment(monkeypatch) as (client, _, key, _):
        try:
            _, channel, _ = setup(client, key, monkeypatch)
            hook = "/webhooks/whatsapp/" + channel["id"]
            assert (
                client.get(
                    hook, params={"hub.mode": "subscribe", "hub.verify_token": "bad", "hub.challenge": "abc"}
                ).status_code
                == 403
            )
            assert (
                client.get(
                    hook,
                    params={
                        "hub.mode": "subscribe",
                        "hub.verify_token": "fixture-verify",
                        "hub.challenge": "abc",
                    },
                ).text
                == "abc"
            )
            inbound(client, channel)
            work()
            outbound = uuid4().hex

            async def sender(*args):
                return outbound

            work(sender=sender)
            for state in ("delivered", "read", "sent"):
                raw = json.dumps(
                    {
                        "object": "whatsapp_business_account",
                        "entry": [
                            {
                                "changes": [
                                    {
                                        "value": {
                                            "metadata": {"phone_number_id": channel["phone_id"]},
                                            "statuses": [{"id": outbound, "status": state}],
                                        }
                                    }
                                ]
                            }
                        ],
                    }
                ).encode()
                signature = "sha256=" + hmac.new(b"fixture-secret", raw, hashlib.sha256).hexdigest()
                assert (
                    client.post(hook, content=raw, headers={"X-Hub-Signature-256": signature}).status_code
                    == 200
                )
            assert rows(channel)[0]["delivery"] == "read"
        finally:
            clean(key)
