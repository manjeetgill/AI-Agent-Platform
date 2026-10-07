"""HTTP-to-database lifecycle across two independent app credentials."""

from test_agent_memory import candidate, grant, review
from test_knowledge import Model, cleanup, credentials, upload
from test_task_agents import environment, sql

from agent_platform import knowledge, memory_embeddings


def enable(client, h, daily=2):
    r = client.put(
        "/v1/memory/settings",
        headers=h,
        json={"expected_revision": 0, "capture": True, "retention_days": 7, "daily_candidates": daily},
    )
    assert r.status_code == 200, r.text


def records(client, h):
    return client.get("/v1/memory/records", headers=h).json()["items"]


def example(client, h, row):
    body = {
        "memory_id": row["id"],
        "memory_version": row["version"],
        "question": "What are the refund terms?",
        "answer": "Refunds are available within thirty days.",
        "consent_ref": "consent:synthetic-business-example",
    }
    r = client.post("/v1/memory/examples", headers=h, json=body)
    assert r.status_code == 201, r.text
    return r.json(), body


def approve_example(client, h, row, approve=True):
    return client.post(
        "/v1/memory/examples/" + row["id"] + "/review",
        headers=h,
        json={
            "expected_version": row["version"],
            "approve": approve,
            "evidence_ref": "review:synthetic-fixture",
            "sensitive_data_removed": True,
            "consent_verified": True,
        },
    )


def test_capture_review_two_apps_and_daily_limit(monkeypatch):
    with environment(monkeypatch) as (a, ha, ka, _), environment(monkeypatch) as (b, hb, kb, _):
        grant(ka)
        grant(kb)
        kh = credentials(ka)
        model = Model()
        monkeypatch.setattr(knowledge, "gateway", lambda: model)
        try:
            assert a.get("/v1/memory/settings", headers=ha).json()["settings"]["capture"] is False
            enable(a, ha, daily=1)
            assert (
                a.put(
                    "/v1/memory/settings", headers=ha, json={"expected_revision": 0, "capture": False}
                ).status_code
                == 409
            )
            assert upload(a, kh).status_code == 200
            body = {"question": "Refunds", "idempotency_key": "auto-capture-test"}
            assert a.post("/v1/knowledge/ask", headers=kh, json=body).status_code == 200
            row = records(a, ha)[0]
            assert row["status"] == "CANDIDATE" and row["confidence"] == 0 and row["origin_answer_id"]
            assert a.get("/v1/memory/search?q=Refunds", headers=ha).json()["items"] == []
            assert a.post("/v1/knowledge/ask", headers=kh, json=body).status_code == 200
            assert (
                a.post(
                    "/v1/knowledge/ask", headers=kh, json={**body, "idempotency_key": "second-capture-test"}
                ).status_code
                == 200
            )
            assert len(records(a, ha)) == 1
            evidence = a.get("/v1/memory/records/" + row["id"] + "/evidence", headers=ha).json()
            assert evidence["result"]["provider"] == "fixture"
            assert b.get("/v1/memory/records/" + row["id"] + "/evidence", headers=hb).status_code == 404
            assert records(b, hb) == []
            assert review(a, ha, row, visibility="app").status_code == 200
            assert a.get("/v1/memory/search?q=Refunds", headers=ha).json()["items"]
            assert b.get("/v1/memory/search?q=Refunds", headers=hb).json()["items"] == []
            # Changing the source makes original evidence stale instead of showing cached claims.
            assert upload(a, kh, text="Refund terms have changed.").status_code == 200
            assert a.get("/v1/memory/records/" + row["id"] + "/evidence", headers=ha).json()["stale"]
        finally:
            cleanup(ka)
            cleanup(kb)


def test_dataset_consent_evaluation_and_revocation(monkeypatch):
    with environment(monkeypatch) as (client, h, key, _):
        grant(key)
        try:
            row, _ = candidate(client, h)
            row = review(client, h, row).json()
            ex, body = example(client, h, row)
            assert client.post("/v1/memory/dataset/export", headers=h).json()["examples"] == []
            assert (
                client.post(
                    "/v1/memory/examples", headers=h, json={**body, "question": "Email patient@example.org"}
                ).status_code
                == 422
            )
            invalid = client.post(
                "/v1/memory/examples/" + ex["id"] + "/review",
                headers=h,
                json={
                    "expected_version": 1,
                    "approve": True,
                    "evidence_ref": "review:fixture",
                    "sensitive_data_removed": False,
                    "consent_verified": False,
                },
            )
            assert invalid.status_code == 422
            ex = approve_example(client, h, ex).json()
            assert len(client.post("/v1/memory/dataset/export", headers=h).json()["examples"]) == 1
            assert (
                client.get("/v1/memory/examples", headers={**h, "X-External-User-ID": "other"}).json()[
                    "items"
                ]
                == []
            )
            model = Model()
            monkeypatch.setattr(knowledge, "gateway", lambda: model)
            payload = {"example_id": ex["id"], "request_key": "evaluation-once"}
            r = client.post("/v1/memory/evaluations", headers=h, json=payload)
            assert r.status_code == 201, r.text
            assert r.json()["result"]["exact_match"] and r.json()["result"]["cited_expected_memory"]
            assert client.post("/v1/memory/evaluations", headers=h, json=payload).status_code == 201
            assert model.calls == 1
            assert client.get("/v1/memory/metrics", headers=h).json()["evaluations"][0]["state"] == "DONE"
            assert review(client, h, row, "REVOKED").status_code == 200
            assert client.post("/v1/memory/dataset/export", headers=h).json()["examples"] == []
            assert client.post("/v1/memory/evaluations", headers=h, json=payload).status_code == 409
            # Consent can still be withdrawn when the source is no longer valid.
            assert approve_example(client, h, ex, False).status_code == 200
        finally:
            cleanup(key)


def test_model_failure_is_not_retried_or_marked_learning(monkeypatch):
    with environment(monkeypatch) as (client, h, key, _):
        grant(key)
        try:
            row, _ = candidate(client, h)
            row = review(client, h, row).json()
            ex, _ = example(client, h, row)
            ex = approve_example(client, h, ex).json()
            model = Model()
            model.fail = True
            monkeypatch.setattr(knowledge, "gateway", lambda: model)
            body = {"example_id": ex["id"], "request_key": "failure-no-retry"}
            assert client.post("/v1/memory/evaluations", headers=h, json=body).status_code == 503
            assert client.post("/v1/memory/evaluations", headers=h, json=body).status_code == 409
            assert model.calls == 1
            assert client.get("/v1/memory/metrics", headers=h).json()["evaluations"][0]["state"] == "UNKNOWN"
        finally:
            cleanup(key)


def test_hybrid_index_permission_version_fences(monkeypatch):
    # Fake encoder isolates the security/index contract from heavyweight model installation.
    monkeypatch.setattr(memory_embeddings, "configuration", lambda: ("fixture", "fixture-v1"))

    async def encode(texts):
        return "fixture-v1", [[1.0, 0.0] for text in texts]

    monkeypatch.setattr(memory_embeddings, "encode", encode)
    with environment(monkeypatch) as (a, ha, ka, _), environment(monkeypatch) as (b, hb, kb, _):
        grant(ka)
        grant(kb)
        row, _ = candidate(a, ha, content="The clinic opens at nine.")
        approved = review(a, ha, row).json()
        assert a.post("/v1/memory/index", headers=ha).json()["indexed"] == 1
        assert a.post("/v1/memory/index", headers=ha).json()["indexed"] == 0
        assert a.get("/v1/memory/search?q=opening%20time", headers=ha).json()["items"]
        assert (
            a.get("/v1/memory/search?q=opening%20time", headers={**ha, "X-External-User-ID": "other"}).json()[
                "items"
            ]
            == []
        )
        assert b.get("/v1/memory/search?q=opening%20time", headers=hb).json()["items"] == []
        assert review(a, ha, approved, "REVOKED").status_code == 200
        assert a.get("/v1/memory/search?q=opening%20time", headers=ha).json()["items"] == []
        assert a.delete("/v1/memory/records/" + row["id"], headers=ha).status_code == 204
        assert sql("SELECT 1 FROM memory_vectors WHERE memory_id=%s", (row["id"],)) == []


def test_continuity_when_provider_changes(monkeypatch):
    class Provider(Model):
        def __init__(self, name):
            self.name = name
            self.sources = []

        async def structured(self, *args):
            self.sources.append(args[2]["passages"])
            result, tokens, _, _ = await super().structured(*args)
            return result, tokens, self.name, "model-" + self.name

    with environment(monkeypatch) as (client, h, key, _):
        grant(key)
        kh = credentials(key)
        try:
            row, _ = candidate(client, h)
            review(client, h, row)
            first = Provider("first")
            second = Provider("second")
            for provider in (first, second):
                monkeypatch.setattr(knowledge, "gateway", lambda provider=provider: provider)
                r = client.post(
                    "/v1/knowledge/ask",
                    headers=kh,
                    json={"question": "Refunds", "idempotency_key": "provider-" + provider.name},
                )
                assert r.status_code == 200, r.text
                assert r.json()["provider"] == provider.name
            assert first.sources == second.sources
            assert first.sources[0][0]["memory_id"] == row["id"]
        finally:
            cleanup(key)


def test_semantic_worker_indexes_validated_records(monkeypatch):
    import asyncio

    from psycopg.rows import dict_row
    from psycopg_pool import AsyncConnectionPool
    from test_task_agents import database

    from agent_platform import memory_worker
    from agent_platform.memory_worker import tick

    monkeypatch.setattr(memory_embeddings, "configuration", lambda: ("fixture", "fixture-worker"))
    monkeypatch.setattr(memory_worker, "configuration", lambda: ("fixture", "fixture-worker"))

    async def encode(texts):
        return "fixture-worker", [[1.0, 0.0] for _ in texts]

    monkeypatch.setattr(memory_embeddings, "encode", encode)
    with environment(monkeypatch) as (client, h, key, _):
        grant(key)
        row, _ = candidate(client, h)

        async def work():
            async with AsyncConnectionPool(database(), open=False, kwargs={"row_factory": dict_row}) as pool:
                return await tick(pool)

        assert asyncio.run(work()) == 0
        review(client, h, row)
        assert asyncio.run(work()) == 1
        assert asyncio.run(work()) == 0


def test_console_memory_page_and_origin_protection(monkeypatch):
    from fastapi.testclient import TestClient
    from test_task_agents import database

    from agent_platform.app import create_app
    from agent_platform.console_app import create_console

    monkeypatch.setenv("AI_PLATFORM_DATABASE_URL", database())
    app = create_console(app=create_app(signing_key="f" * 48))
    with TestClient(app) as client:
        assert client.get("/memory").status_code == 200
        assert client.get("/console/memory.js").status_code == 200
        assert client.get("/memory", headers={"Host": "attacker.example"}).status_code == 403
        assert client.get("/console/api/memory/settings").status_code == 403
        h = {"X-Console-Intent": "user-action"}
        response = client.get("/console/api/memory/settings", headers=h)
        assert response.status_code == 200, response.text
        assert (
            client.put(
                "/console/api/memory/settings",
                headers={**h, "Origin": "https://attacker.example"},
                json={"expected_revision": 0, "capture": True},
            ).status_code
            == 403
        )
