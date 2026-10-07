"""Opt-in candidate capture, reviewable datasets and measured evaluation.

Capture never validates facts. Dataset export never starts provider training.
"""

import asyncio
import re
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from psycopg.types.json import Jsonb
from pydantic import Field

from .agent_memory import Text, event
from .runtime_errors import RunFailure
from .service_identity import authenticate
from .task_agents import digest
from .task_api import credential_contract
from .tasks import Strict, TaskError


class Settings(Strict):
    expected_revision: int = Field(ge=0)
    capture: bool
    retention_days: int = Field(default=30, ge=1, le=366)
    daily_candidates: int = Field(default=25, ge=0, le=100)


class Example(Strict):
    memory_id: UUID
    memory_version: int = Field(ge=1)
    question: Text
    answer: Text
    consent_ref: str = Field(min_length=8, max_length=300)


class ExampleReview(Strict):
    expected_version: int = Field(ge=1)
    approve: bool
    evidence_ref: str = Field(min_length=8, max_length=300)
    sensitive_data_removed: bool
    consent_verified: bool


class Evaluate(Strict):
    example_id: UUID
    request_key: str = Field(min_length=8, max_length=100)


# Additional screening only; reviewer attestation remains mandatory. Never claim perfect anonymization.
SENSITIVE = re.compile(
    r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b|(?:\+?\d[\d ()-]{8,}\d)|"
    r"\b(?:Bearer\s+\S+|sk-[A-Za-z0-9_-]{12,}|AIza[A-Za-z0-9_-]+|EA[A-Za-z0-9]{30,})\b",
    re.IGNORECASE,
)


async def capture_answer(conn, app, owner, answer_id, result):
    """Store bounded model assertions + immutable evidence pointers; no extra LLM call.

    Runs atomically with answer completion. Shared channels retain the operator's
    private candidate queue and never publish extracted customer facts automatically.
    """
    setting = await (
        await conn.execute("SELECT * FROM memory_settings WHERE app_id=%s AND owner_id=%s", (app, owner))
    ).fetchone()
    if not setting or not setting["capture"] or not setting["daily_candidates"]:
        return
    await conn.execute("SELECT id FROM platform_apps WHERE id=%s FOR UPDATE", (app,))
    setting = await (
        await conn.execute(
            "SELECT * FROM memory_settings WHERE app_id=%s AND owner_id=%s FOR UPDATE", (app, owner)
        )
    ).fetchone()
    if not setting or not setting["capture"]:
        return
    counts = await (
        await conn.execute(
            """SELECT count(*) total,
        count(*) FILTER(WHERE owner_id=%s AND origin_answer_id IS NOT NULL
          AND created_at >= date_trunc('day',now())) today FROM platform_memories WHERE app_id=%s""",
            (owner, app),
        )
    ).fetchone()
    remaining = min(3, setting["daily_candidates"] - counts["today"], 10000 - counts["total"])
    known = {s["id"] for s in result.get("citations", [])}
    for index, claim in enumerate(result.get("claims", [])[: max(0, remaining)]):
        if not claim["sources"] or not set(claim["sources"]) <= known:
            continue
        body = {"content": claim["text"], "answer": str(answer_id), "sources": claim["sources"]}
        row = await (
            await conn.execute(
                """INSERT INTO platform_memories
            (id,app_id,owner_id,request_key,fingerprint,kind,content,source_type,source_ref,
             confidence,expires_at,origin_answer_id)
            VALUES(%s,%s,%s,%s,%s,'FACT',%s,'MODEL',%s,0,%s,%s)
            ON CONFLICT(app_id,owner_id,request_key) DO NOTHING RETURNING id""",
                (
                    uuid4(),
                    app,
                    owner,
                    f"answer:{answer_id}:{index}",
                    digest(body),
                    claim["text"],
                    f"answer:{answer_id}:claim:{index}",
                    datetime.now(UTC) + timedelta(days=setting["retention_days"]),
                    answer_id,
                ),
            )
        ).fetchone()
        if row:
            await event(
                conn,
                {"app_id": app, "user": owner, "principal": "runtime:capture"},
                row["id"],
                "CANDIDATE_CAPTURED",
                {"answer_id": str(answer_id), "source_ids": claim["sources"]},
            )


async def usable_example(conn, c, ident, approved=False):
    row = await (
        await conn.execute(
            """SELECT e.*,m.status memory_status,m.version current_memory_version,
        m.expires_at FROM memory_examples e JOIN platform_memories m ON m.id=e.memory_id
        WHERE e.id=%s AND e.app_id=%s AND e.owner_id=%s FOR SHARE OF e,m""",
            (ident, c["app_id"], c["user"]),
        )
    ).fetchone()
    if not row:
        raise HTTPException(404, "Example not found")
    if (
        row["memory_status"] != "VALIDATED"
        or row["memory_version"] != row["current_memory_version"]
        or row["expires_at"] <= datetime.now(UTC)
    ):
        raise HTTPException(409, "Example source is no longer valid")
    if approved and row["status"] != "APPROVED":
        raise HTTPException(409, "Example has not been approved")
    return row


def install_workflow(app):
    router = APIRouter(prefix="/v1/memory", dependencies=[Depends(credential_contract)])

    async def auth(request, scope):
        pool = request.app.state.repository.pool
        c = await authenticate(
            pool, request.headers.get("authorization", ""), request.headers.get("X-External-User-ID"), scope
        )
        return pool, c

    @router.get("/settings")
    async def settings(request: Request):
        pool, c = await auth(request, "memory:read")
        async with pool.connection() as conn:
            row = await (
                await conn.execute(
                    "SELECT * FROM memory_settings WHERE app_id=%s AND owner_id=%s", (c["app_id"], c["user"])
                )
            ).fetchone()
        from .memory_embeddings import configuration

        return {
            "settings": row
            or {"revision": 0, "capture": False, "retention_days": 30, "daily_candidates": 25},
            "semantic_configured": bool(configuration()),
            "automatic_training": False,
        }

    @router.put("/settings")
    async def configure(body: Settings, request: Request):
        pool, c = await auth(request, "memory:review")
        async with pool.connection() as conn, conn.transaction():
            await conn.execute("SELECT id FROM platform_apps WHERE id=%s FOR UPDATE", (c["app_id"],))
            prior = await (
                await conn.execute(
                    "SELECT revision FROM memory_settings WHERE app_id=%s AND owner_id=%s",
                    (c["app_id"], c["user"]),
                )
            ).fetchone()
            revision = prior["revision"] if prior else 0
            if revision != body.expected_revision:
                raise HTTPException(409, "Settings changed; reload")
            await conn.execute(
                """INSERT INTO memory_settings VALUES(%s,%s,%s,%s,%s,%s,now())
                ON CONFLICT(app_id,owner_id) DO UPDATE SET capture=EXCLUDED.capture,
                retention_days=EXCLUDED.retention_days,daily_candidates=EXCLUDED.daily_candidates,
                revision=EXCLUDED.revision,updated_at=now()""",
                (
                    c["app_id"],
                    c["user"],
                    body.capture,
                    body.retention_days,
                    body.daily_candidates,
                    revision + 1,
                ),
            )
            await event(conn, c, None, "SETTINGS_CHANGED", body.model_dump())
        return {"revision": revision + 1}

    @router.get("/records/{ident}/evidence")
    async def evidence(ident: UUID, request: Request):
        pool, c = await auth(request, "memory:read")
        async with pool.connection() as conn:
            row = await (
                await conn.execute(
                    """SELECT m.source_ref,a.result FROM platform_memories m
                LEFT JOIN knowledge_answers a ON a.id=m.origin_answer_id AND a.app_id=m.app_id AND a.owner_id=m.owner_id
                WHERE m.id=%s AND m.app_id=%s AND m.owner_id=%s""",
                    (ident, c["app_id"], c["user"]),
                )
            ).fetchone()
            if not row:
                raise HTTPException(404, "Memory not found")
            if row["result"]:
                from .knowledge import source_current

                for source in row["result"]["citations"]:
                    if not await source_current(conn, source, c["app_id"], c["user"]):
                        return {"source_ref": row["source_ref"], "result": None, "stale": True}
        return {**row, "stale": False}

    @router.post("/index")
    async def index(request: Request):
        pool, c = await auth(request, "memory:review")
        from .memory_embeddings import index_owned

        try:
            count = await index_owned(pool, c["app_id"], c["user"])
        except (ValueError, ImportError, OSError):
            raise HTTPException(
                503, "Local embedding model unavailable; lexical retrieval remains available"
            ) from None
        async with pool.connection() as conn:
            await event(conn, c, None, "SEMANTIC_INDEXED", {"count": count})
        return {"indexed": count, "batch_limit": 32}

    @router.get("/metrics")
    async def metrics(request: Request):
        pool, c = await auth(request, "memory:read")
        async with pool.connection() as conn:
            memories = await (
                await conn.execute(
                    """SELECT status,count(*) count FROM platform_memories
                WHERE app_id=%s AND owner_id=%s GROUP BY status""",
                    (c["app_id"], c["user"]),
                )
            ).fetchall()
            feedback = await (
                await conn.execute(
                    """SELECT outcome,count(*) count FROM agent_outcome_feedback
                WHERE app_id=%s AND owner_id=%s GROUP BY outcome""",
                    (c["app_id"], c["user"]),
                )
            ).fetchall()
            evaluations = await (
                await conn.execute(
                    """SELECT id,example_id,state,result,created_at FROM memory_evaluations
                WHERE app_id=%s AND owner_id=%s ORDER BY created_at DESC LIMIT 30""",
                    (c["app_id"], c["user"]),
                )
            ).fetchall()
        return {
            "memories": memories,
            "feedback": feedback,
            "evaluations": evaluations,
            "outcome_source": "app_reported",
            "automatic_training": False,
        }

    @router.post("/examples", status_code=201)
    async def example(body: Example, request: Request):
        pool, c = await auth(request, "memory:write")
        if SENSITIVE.search(body.question + " " + body.answer):
            raise HTTPException(
                422, "Remove detected personal identifiers or secrets before saving an example"
            )
        async with pool.connection() as conn, conn.transaction():
            await conn.execute("SELECT id FROM platform_apps WHERE id=%s FOR UPDATE", (c["app_id"],))
            memory = await (
                await conn.execute(
                    """SELECT 1 FROM platform_memories WHERE id=%s AND app_id=%s
                AND owner_id=%s AND status='VALIDATED' AND version=%s AND expires_at>now() FOR SHARE""",
                    (body.memory_id, c["app_id"], c["user"], body.memory_version),
                )
            ).fetchone()
            if not memory:
                raise HTTPException(409, "An owned, current validated memory is required")
            count = await (
                await conn.execute("SELECT count(*) n FROM memory_examples WHERE app_id=%s", (c["app_id"],))
            ).fetchone()
            if count["n"] >= 1000:
                raise HTTPException(429, "Example capacity reached")
            row = await (
                await conn.execute(
                    """INSERT INTO memory_examples
                (id,app_id,owner_id,memory_id,memory_version,question,answer,consent_ref)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
                    (
                        uuid4(),
                        c["app_id"],
                        c["user"],
                        body.memory_id,
                        body.memory_version,
                        body.question,
                        body.answer,
                        body.consent_ref,
                    ),
                )
            ).fetchone()
            await event(conn, c, body.memory_id, "EXAMPLE_CREATED", {"example_id": str(row["id"])})
        return row

    @router.get("/examples")
    async def examples(request: Request):
        pool, c = await auth(request, "memory:read")
        async with pool.connection() as conn:
            rows = await (
                await conn.execute(
                    "SELECT * FROM memory_examples WHERE app_id=%s AND owner_id=%s ORDER BY id LIMIT 1000",
                    (c["app_id"], c["user"]),
                )
            ).fetchall()
        return {"items": rows}

    @router.post("/examples/{ident}/review")
    async def review_example(ident: UUID, body: ExampleReview, request: Request):
        pool, c = await auth(request, "memory:review")
        async with pool.connection() as conn, conn.transaction():
            # Lock before validation to prevent two reviewers racing.
            row = await (
                await conn.execute(
                    "SELECT * FROM memory_examples WHERE id=%s AND app_id=%s AND owner_id=%s FOR UPDATE",
                    (ident, c["app_id"], c["user"]),
                )
            ).fetchone()
            if not row:
                raise HTTPException(404, "Example not found")
            if row["version"] != body.expected_version:
                raise HTTPException(409, "Example changed; reload")
            if body.approve:
                if row["status"] != "DRAFT":
                    raise HTTPException(409, "Only draft examples can be approved")
                await usable_example(conn, c, ident)
                if not body.sensitive_data_removed or not body.consent_verified:
                    raise HTTPException(422, "Verify consent and removal of sensitive data before approval")
            updated = await (
                await conn.execute(
                    """UPDATE memory_examples SET status=%s,reviewer=%s,evidence_ref=%s,
                version=version+1,updated_at=now() WHERE id=%s RETURNING *""",
                    ("APPROVED" if body.approve else "REVOKED", c["principal"], body.evidence_ref, ident),
                )
            ).fetchone()
            await event(
                conn,
                c,
                row["memory_id"],
                "EXAMPLE_REVIEWED",
                {"example_id": str(ident), "status": updated["status"]},
            )
        return updated

    @router.post("/dataset/export")
    async def export(request: Request):
        pool, c = await auth(request, "memory:review")
        async with pool.connection() as conn, conn.transaction():
            rows = await (
                await conn.execute(
                    """SELECT e.id,e.question,e.answer,e.consent_ref FROM memory_examples e
                JOIN platform_memories m ON m.id=e.memory_id AND m.version=e.memory_version
                WHERE e.app_id=%s AND e.owner_id=%s AND e.status='APPROVED'
                AND m.status='VALIDATED' AND m.expires_at>now() ORDER BY e.id LIMIT 1000 FOR SHARE OF e,m""",
                    (c["app_id"], c["user"]),
                )
            ).fetchall()
            rows = [r for r in rows if not SENSITIVE.search(r["question"] + " " + r["answer"])]
            await event(conn, c, None, "DATASET_EXPORTED", {"count": len(rows)})
        return {
            "format": "provider-neutral-messages-v1",
            "examples": [
                {
                    "id": r["id"],
                    "messages": [
                        {"role": "user", "content": r["question"]},
                        {"role": "assistant", "content": r["answer"]},
                    ],
                    "consent_ref": r["consent_ref"],
                }
                for r in rows
            ],
            "provider_upload_started": False,
        }

    @router.post("/evaluations", status_code=201)
    async def evaluate(body: Evaluate, request: Request):
        pool, c = await auth(request, "memory:review")
        fp = digest(body.model_dump(mode="json"))
        async with pool.connection() as conn, conn.transaction():
            example = await usable_example(conn, c, body.example_id, approved=True)
            previous = await (
                await conn.execute(
                    """SELECT * FROM memory_evaluations
                WHERE app_id=%s AND owner_id=%s AND request_key=%s""",
                    (c["app_id"], c["user"], body.request_key),
                )
            ).fetchone()
            if previous:
                if previous["fingerprint"] != fp:
                    raise HTTPException(409, "Request key reused")
                if previous["state"] != "DONE":
                    raise HTTPException(409, "Evaluation outcome uncertain; no automatic retry")
                if previous["example_version"] != example["version"]:
                    raise HTTPException(409, "Example changed")
                return previous
            ident = uuid4()
            inserted = await (
                await conn.execute(
                    """INSERT INTO memory_evaluations
                (id,app_id,owner_id,request_key,fingerprint,example_id,example_version,state)
                VALUES(%s,%s,%s,%s,%s,%s,%s,'STARTED') ON CONFLICT DO NOTHING RETURNING id""",
                    (ident, c["app_id"], c["user"], body.request_key, fp, example["id"], example["version"]),
                )
            ).fetchone()
            if not inserted:
                raise HTTPException(409, "Evaluation is already running")
        try:
            from .knowledge import answer, gateway

            result = await answer(
                pool, gateway(), c["app_id"], c["user"], example["question"], "eval:" + str(ident)
            )
            actual = " ".join(x["text"] for x in result["claims"])
            normalize = lambda s: " ".join(s.casefold().split())
            metrics = {
                "exact_match": normalize(actual) == normalize(example["answer"]),
                "cited_expected_memory": any(
                    s.get("memory_id") == str(example["memory_id"]) for s in result["citations"]
                ),
                "answer": actual,
                "provider": result["provider"],
                "model": result["model"],
                "tokens": result["tokens"],
                "scope": "exact text and citation checks; not a factual accuracy assessment",
            }
            async with pool.connection() as conn, conn.transaction():
                current = await usable_example(conn, c, example["id"], approved=True)
                if current["version"] != example["version"]:
                    raise HTTPException(409, "Example changed during evaluation")
                await conn.execute(
                    "UPDATE memory_evaluations SET state='DONE',result=%s WHERE id=%s",
                    (Jsonb(metrics), ident),
                )
                await event(
                    conn, c, example["memory_id"], "EVALUATION_COMPLETED", {"evaluation_id": str(ident)}
                )
            return {"id": ident, "state": "DONE", "result": metrics}
        except (Exception, asyncio.CancelledError) as exc:
            async with pool.connection() as conn:
                await conn.execute("UPDATE memory_evaluations SET state='UNKNOWN' WHERE id=%s", (ident,))
            if isinstance(exc, (RunFailure, TimeoutError)):
                raise HTTPException(503, "Model unavailable; evaluation was not retried") from None
            if isinstance(exc, TaskError):
                raise HTTPException(409, str(exc)) from None
            raise

    app.include_router(router)
