"""Knowledge isolation, indexing, citations, lifecycle and durable agent integration."""

import asyncio
from uuid import uuid4

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from test_task_agents import database, environment, sql

from agent_platform import knowledge
from agent_platform.service_identity import provision
from agent_platform.task_store import TaskStore
from agent_platform.task_worker import tick


class Model:
    calls = 0
    invalid = False
    fail = False

    async def structured(self, task, policy, data, schema, validator):
        self.calls += 1
        assert "untrusted data" in policy
        if self.fail:
            raise TimeoutError()
        source = "invented" if self.invalid else data["passages"][0]["id"]
        return (
            validator(
                schema(
                    claims=[{"text": "Refunds are available within thirty days.", "sources": [source]}],
                    missing=[],
                )
            ),
            20,
            "fixture",
            "fixture",
        )


def credentials(key, publish=True):
    async def create():
        async with AsyncConnectionPool(database(), open=False, kwargs={"row_factory": dict_row}) as pool:
            tenant = (
                await sql_async(pool, "SELECT tenant_id FROM platform_apps WHERE id=%s", (key["app_id"],))
            )[0]["tenant_id"]
            return await provision(
                pool,
                tenant=tenant,
                organisation="Fixture",
                app_name="Fixture",
                app_id=key["app_id"],
                scopes=["knowledge:read", "knowledge:write"] + (["knowledge:publish"] if publish else []),
            )

    credential = asyncio.run(create())
    return {"Authorization": "Bearer " + credential["credential"], "X-External-User-ID": "fixture-user"}


async def sql_async(pool, statement, params):
    async with pool.connection() as conn:
        return await (await conn.execute(statement, params)).fetchall()


def upload(client, headers, visibility="private", text="Refunds are available within thirty days."):
    return client.post(
        "/v1/knowledge/documents",
        headers=headers,
        json={
            "external_id": "refunds",
            "title": "Refund policy",
            "text": text,
            "source_label": "Support manual",
            "visibility": visibility,
        },
    )


def cleanup(key):
    sql("DELETE FROM knowledge_answers WHERE app_id=%s", (key["app_id"],))
    sql("DELETE FROM knowledge_documents WHERE app_id=%s", (key["app_id"],))


def test_document_isolation_publication_and_updates(monkeypatch):
    with environment(monkeypatch) as (a, _, ka, _), environment(monkeypatch) as (b, _, kb, _):
        ha, hb = credentials(ka), credentials(kb)
        try:
            first = upload(a, ha)
            assert first.status_code == 200, first.text
            assert upload(a, ha).json()["version"] == 1
            other = {**ha, "X-External-User-ID": "other-user"}
            query = {"question": "What is the refund policy?"}
            assert a.post("/v1/knowledge/search", headers=other, json=query).json()["passages"] == []
            assert b.post("/v1/knowledge/search", headers=hb, json=query).json()["passages"] == []
            assert upload(a, credentials(ka, publish=False), visibility="app").status_code == 403
            assert upload(a, ha, visibility="app").json()["version"] == 2
            assert a.post("/v1/knowledge/search", headers=other, json=query).json()["passages"]
            assert b.delete("/v1/knowledge/documents/" + first.json()["id"], headers=hb).status_code == 404
            assert a.delete("/v1/knowledge/documents/" + first.json()["id"], headers=ha).status_code == 204
            assert a.post("/v1/knowledge/search", headers=ha, json=query).json()["passages"] == []
        finally:
            cleanup(ka)
            cleanup(kb)


def test_cited_answers_cached_safely_and_abstain(monkeypatch):
    model = Model()
    monkeypatch.setattr(knowledge, "gateway", lambda: model)
    with environment(monkeypatch) as (client, _, key, _):
        headers = credentials(key)
        try:
            no = client.post(
                "/v1/knowledge/ask",
                headers=headers,
                json={"question": "refund policy", "idempotency_key": "empty-answer"},
            )
            assert no.status_code == 200 and not no.json()["claims"] and model.calls == 0
            doc = upload(client, headers).json()
            body = {"question": "refund policy", "idempotency_key": "answer-one"}
            result = client.post("/v1/knowledge/ask", headers=headers, json=body)
            assert result.status_code == 200, result.text
            assert result.json()["citations"][0]["document_id"] == doc["id"]
            assert (
                client.post("/v1/knowledge/ask", headers=headers, json=body).status_code == 200
                and model.calls == 1
            )
            upload(client, headers, text="Refunds are available within ten days.")
            assert client.post("/v1/knowledge/ask", headers=headers, json=body).status_code == 409
            model.invalid = True
            invalid = {"question": "refund policy", "idempotency_key": "bad-citation"}
            assert client.post("/v1/knowledge/ask", headers=headers, json=invalid).status_code == 409
            assert (
                client.post("/v1/knowledge/ask", headers=headers, json=invalid).status_code == 409
                and model.calls == 2
            )
        finally:
            cleanup(key)


def test_knowledge_tool_runs_through_worker(monkeypatch):
    model = Model()
    monkeypatch.setattr(knowledge, "gateway", lambda: model)
    with environment(monkeypatch) as (client, task_headers, key, _):
        headers = credentials(key)
        try:
            upload(client, headers)
            sql(
                "INSERT INTO platform_tool_grants(app_id,tool) VALUES(%s,%s)",
                (key["app_id"], "knowledge.answer"),
            )
            saved = client.post(
                "/v1/task-agents",
                headers=task_headers,
                json={
                    "idempotency_key": uuid4().hex,
                    "blueprint": {
                        "name": "Support knowledge agent",
                        "description": "Cited policy answers",
                        "steps": [
                            {
                                "tool": "knowledge.answer",
                                "version": "1",
                                "arguments": {"question": "refund policy"},
                            }
                        ],
                    },
                },
            )
            assert saved.status_code == 201, saved.text
            run = client.post(
                "/v1/task-agents/" + saved.json()["id"] + "/runs",
                headers=task_headers,
                json={"idempotency_key": uuid4().hex},
            )
            registry = knowledge.KnowledgeAnswer(client.app.state.repository.pool)
            from agent_platform.tasks import Registry

            client.portal.call(
                tick, TaskStore(client.app.state.repository.pool), Registry({"knowledge.answer": registry})
            )
            task = client.get("/v1/tasks/" + run.json()["id"], headers=task_headers).json()
            assert task["status"] == "DONE" and task["steps"][0]["result"]["citations"]
            assert model.calls == 1
        finally:
            cleanup(key)
