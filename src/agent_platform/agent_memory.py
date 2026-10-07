"""Scoped, reviewed memory and provider-neutral handoffs. Never trains a model.

Review records an authorized application's attestation, not an LLM truth score.
Content is immutable: revoke/delete and submit a corrected candidate.
"""

from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from psycopg.types.json import Jsonb
from pydantic import AwareDatetime, Field

from .service_identity import authenticate
from .task_agents import digest
from .task_api import credential_contract
from .tasks import Strict

Text = Annotated[str, Field(min_length=1, max_length=600)]
Namespace = Annotated[str, Field(min_length=1, max_length=80, pattern=r"^[a-zA-Z0-9_.:-]+$")]


class Candidate(Strict):
    request_key: str = Field(min_length=8, max_length=100)
    namespace: Namespace = "general"
    kind: Literal["FACT", "PREFERENCE", "PATTERN", "DECISION"]
    content: Text
    source_type: Literal["USER", "TOOL", "TRUSTED_SOURCE", "MODEL"]
    source_ref: str = Field(min_length=1, max_length=300)
    confidence: float = Field(ge=0, le=1, allow_inf_nan=False)
    expires_at: AwareDatetime


class Review(Strict):
    expected_version: int = Field(ge=1)
    decision: Literal["VALIDATED", "REJECTED", "REVOKED"]
    method: Literal["USER_CONFIRMATION", "TOOL_VERIFICATION", "SOURCE_REVIEW"]
    evidence_ref: str = Field(min_length=1, max_length=300)
    visibility: Literal["private", "app"] = "private"


class Decision(Strict):
    decision: Text
    reason: Text


class Checkpoint(Strict):
    expected_revision: int = Field(ge=0)
    expected_task_revision: int = Field(ge=0)
    expected_event_id: int = Field(ge=1)
    goal: Text
    completed: list[Text] = Field(default_factory=list, max_length=10)
    decisions: list[Decision] = Field(default_factory=list, max_length=10)
    open_questions: list[Text] = Field(default_factory=list, max_length=10)
    next_action: Text
    artifacts: list[Annotated[str, Field(max_length=300)]] = Field(default_factory=list, max_length=10)


class Feedback(Strict):
    request_key: str = Field(min_length=8, max_length=100)
    target_type: Literal["task", "answer"]
    target_id: UUID
    outcome: Literal["CONFIRMED_SUCCESS", "CORRECTION", "FAILED", "UNKNOWN"]
    evidence_ref: str = Field(min_length=1, max_length=300)
    note: str = Field(default="", max_length=600)


async def event(conn, claims, ident, action, details=None):
    await conn.execute(
        """INSERT INTO platform_memory_events(app_id,owner_id,memory_id,event,actor,details)
        VALUES(%s,%s,%s,%s,%s,%s)""",
        (claims["app_id"], claims["user"], ident, action, claims["principal"], Jsonb(details or {})),
    )


async def retrieve(pool, app, owner, query, *, namespace="general", shared_only=False, limit=6):
    """Identity filters precede ranking. Nothing unreviewed or expired reaches context."""
    from .flags import enabled

    if not enabled("AI_MEMORY", default=True):
        return []
    async with pool.connection() as conn:
        rows = await (
            await conn.execute(
                """SELECT id,content,kind,source_ref,confidence,version,expires_at
            FROM platform_memories, to_tsquery('simple',replace(plainto_tsquery('simple',%s)::text,' & ',' | ')) q
            WHERE app_id=%s AND namespace=%s AND status='VALIDATED' AND expires_at>now()
            AND (visibility='app' OR (owner_id=%s AND NOT %s))
            AND to_tsvector('simple',content) @@ q
            ORDER BY ts_rank_cd(to_tsvector('simple',content),q) DESC,id LIMIT %s""",
                (query[:4000], app, namespace, owner, shared_only, max(1, min(limit, 8))),
            )
        ).fetchall()
    from .memory_embeddings import semantic

    try:
        semantic_rows = await semantic(pool, app, owner, query, namespace, shared_only, max(1, min(limit, 8)))
    except (ImportError, OSError, ValueError, RuntimeError):
        import logging

        logging.getLogger(__name__).warning("Semantic memory unavailable; using lexical retrieval")
        semantic_rows = []
    # Reciprocal rank fusion preserves both exact terms and semantic matches.
    candidates = {str(r["id"]): r for r in rows + semantic_rows}
    scores = {}
    for ranked in (rows, semantic_rows):
        for rank, row in enumerate(ranked):
            ident = str(row["id"])
            scores[ident] = scores.get(ident, 0) + 1 / (60 + rank + 1)
    rows = [candidates[i] for i in sorted(scores, key=lambda i: (-scores[i], i))[: max(1, min(limit, 8))]]
    return [
        {
            "id": "memory:" + str(r["id"]) + ":" + str(r["version"]),
            "memory_id": str(r["id"]),
            "version": r["version"],
            "title": "Reviewed " + r["kind"].lower(),
            "text": r["content"],
            "source_label": r["source_ref"],
            "confidence": r["confidence"],
        }
        for r in rows
    ]


async def current_source(conn, source, app, owner, *, shared_only=False):
    if "memory_id" not in source:
        return False
    return bool(
        await (
            await conn.execute(
                """SELECT 1 FROM platform_memories
        WHERE id=%s AND app_id=%s AND version=%s AND status='VALIDATED' AND expires_at>now()
        AND (visibility='app' OR (owner_id=%s AND NOT %s)) FOR SHARE""",
                (source["memory_id"], app, source["version"], owner, shared_only),
            )
        ).fetchone()
    )


async def task_owned(conn, claims, task_id):
    row = await (
        await conn.execute(
            """SELECT id,revision,current_step,status FROM chore_tasks
        WHERE id=%s AND source_app_id=%s AND tenant_id=%s AND owner_id=%s FOR UPDATE""",
            (task_id, claims["app_id"], claims["tenant"], claims["user"]),
        )
    ).fetchone()
    if not row:
        raise HTTPException(404, "Task not found")
    return row


async def snapshot(conn, task_id, event_name):
    """Deterministic operational handoff, committed atomically with the task event."""
    task = await (await conn.execute("SELECT * FROM chore_tasks WHERE id=%s", (task_id,))).fetchone()
    if not task or not task["source_app_id"]:
        return
    steps = await (
        await conn.execute(
            """SELECT position,tool,status,summary,action_hash FROM chore_steps
        WHERE task_id=%s ORDER BY position LIMIT 30""",
            (task_id,),
        )
    ).fetchall()
    pending = next((s for s in steps if s["position"] == task["current_step"]), None)
    value = {
        "goal": task["request"][:600],
        "status": task["status"],
        "task_revision": task["revision"],
        "event": event_name,
        "completed": [s for s in steps if s["status"] == "DONE"],
        "next_step": pending,
        "requires_reconciliation": task["status"] == "OUTCOME_UNKNOWN",
        "tool_results_url": "/v1/tasks/" + str(task_id),
        "authority": "Saved execution state; historical approvals do not authorize a new action",
    }
    await conn.execute(
        """INSERT INTO task_handoff_snapshots VALUES(%s,%s,%s,%s,now())
        ON CONFLICT(task_id) DO UPDATE SET payload=EXCLUDED.payload,updated_at=now()""",
        (task_id, task["source_app_id"], task["owner_id"], Jsonb(value)),
    )


def install_memory(app):
    router = APIRouter(prefix="/v1/memory", dependencies=[Depends(credential_contract)])

    async def auth(request, scope):
        pool = request.app.state.repository.pool
        claims = await authenticate(
            pool, request.headers.get("authorization", ""), request.headers.get("X-External-User-ID"), scope
        )
        return pool, claims

    async def owned(conn, claims, ident):
        row = await (
            await conn.execute(
                """SELECT * FROM platform_memories
            WHERE id=%s AND app_id=%s AND owner_id=%s FOR UPDATE""",
                (ident, claims["app_id"], claims["user"]),
            )
        ).fetchone()
        if not row:
            raise HTTPException(404, "Memory not found")
        return row

    @router.post("/candidates", status_code=201)
    async def create(body: Candidate, request: Request):
        pool, c = await auth(request, "memory:write")
        if not datetime.now(UTC) < body.expires_at <= datetime.now(UTC) + timedelta(days=366):
            raise HTTPException(422, "Expiry must be in the future and within one year")
        fp = digest(body.model_dump(mode="json"))
        async with pool.connection() as conn, conn.transaction():
            # Serializes per-app quota and duplicate submissions.
            await conn.execute("SELECT id FROM platform_apps WHERE id=%s FOR UPDATE", (c["app_id"],))
            prior = await (
                await conn.execute(
                    """SELECT * FROM platform_memories
                WHERE app_id=%s AND owner_id=%s AND request_key=%s""",
                    (c["app_id"], c["user"], body.request_key),
                )
            ).fetchone()
            if prior:
                if prior["fingerprint"] != fp:
                    raise HTTPException(409, "Request key belongs to different content")
                return prior
            count = await (
                await conn.execute("SELECT count(*) n FROM platform_memories WHERE app_id=%s", (c["app_id"],))
            ).fetchone()
            if count["n"] >= 10000:
                raise HTTPException(429, "Memory capacity reached; expire and delete unused records")
            row = await (
                await conn.execute(
                    """INSERT INTO platform_memories
                (id,app_id,owner_id,request_key,fingerprint,namespace,kind,content,source_type,source_ref,confidence,expires_at)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
                    (
                        uuid4(),
                        c["app_id"],
                        c["user"],
                        body.request_key,
                        fp,
                        body.namespace,
                        body.kind,
                        body.content,
                        body.source_type,
                        body.source_ref,
                        body.confidence,
                        body.expires_at,
                    ),
                )
            ).fetchone()
            await event(conn, c, row["id"], "CANDIDATE_CREATED")
            return row

    @router.get("/search")
    async def search(
        request: Request,
        q: str = Query(min_length=1, max_length=1500),
        namespace: Namespace = "general",
        limit: int = Query(default=6, ge=1, le=8),
    ):
        pool, c = await auth(request, "memory:read")
        return {
            "items": await retrieve(pool, c["app_id"], c["user"], q, namespace=namespace, limit=limit),
            "retrieval": "hybrid_if_configured",
            "training_allowed": False,
        }

    @router.get("/records")
    async def records(
        request: Request, before: UUID | None = None, limit: int = Query(default=50, ge=1, le=100)
    ):
        pool, c = await auth(request, "memory:read")
        async with pool.connection() as conn:
            rows = await (
                await conn.execute(
                    """SELECT * FROM platform_memories WHERE app_id=%s AND owner_id=%s
                AND (%s::uuid IS NULL OR id<%s) ORDER BY id DESC LIMIT %s""",
                    (c["app_id"], c["user"], before, before, limit),
                )
            ).fetchall()
        return {"items": rows, "next_before": rows[-1]["id"] if len(rows) == limit else None}

    @router.post("/records/{ident}/review")
    async def review(ident: UUID, body: Review, request: Request):
        pool, c = await auth(request, "memory:review")
        async with pool.connection() as conn, conn.transaction():
            row = await owned(conn, c, ident)
            if row["version"] != body.expected_version:
                raise HTTPException(409, "Memory changed; reload before reviewing")
            allowed = {"CANDIDATE": {"VALIDATED", "REJECTED"}, "VALIDATED": {"REVOKED"}}
            if body.decision not in allowed.get(row["status"], set()):
                raise HTTPException(409, "Invalid memory transition; submit a new candidate for corrections")
            if body.decision == "VALIDATED" and row["expires_at"] <= datetime.now(UTC):
                raise HTTPException(409, "Expired memory cannot be validated")
            updated = await (
                await conn.execute(
                    """UPDATE platform_memories SET status=%s,visibility=%s,
                reviewer=%s,validation_method=%s,validation_ref=%s,version=version+1,updated_at=now()
                WHERE id=%s RETURNING *""",
                    (body.decision, body.visibility, c["principal"], body.method, body.evidence_ref, ident),
                )
            ).fetchone()
            await event(
                conn, c, ident, body.decision, {"version": updated["version"], "visibility": body.visibility}
            )
            return updated

    @router.delete("/records/{ident}", status_code=204)
    async def delete(ident: UUID, request: Request):
        pool, c = await auth(request, "memory:write")
        async with pool.connection() as conn, conn.transaction():
            await owned(conn, c, ident)
            await conn.execute("DELETE FROM platform_memories WHERE id=%s", (ident,))
            await event(conn, c, ident, "DELETED")
        return Response(status_code=204)

    @router.put("/tasks/{task_id}/checkpoint")
    async def checkpoint(task_id: UUID, body: Checkpoint, request: Request):
        pool, c = await auth(request, "tasks:write")
        async with pool.connection() as conn, conn.transaction():
            task = await task_owned(conn, c, task_id)
            prior = await (
                await conn.execute("SELECT * FROM task_checkpoints WHERE task_id=%s", (task_id,))
            ).fetchone()
            rev = prior["revision"] if prior else 0
            last = await (
                await conn.execute("SELECT max(id) n FROM chore_events WHERE task_id=%s", (task_id,))
            ).fetchone()
            if body.expected_event_id != last["n"]:
                raise HTTPException(409, "Task execution changed; reload its events before writing")
            if body.expected_revision != rev or body.expected_task_revision != task["revision"]:
                raise HTTPException(409, "Task or checkpoint changed; reload before writing")
            data = body.model_dump(
                exclude={"expected_revision", "expected_task_revision", "expected_event_id"}
            )
            data["captured_event_id"] = last["n"]
            await conn.execute(
                """INSERT INTO task_checkpoints VALUES(%s,%s,%s,%s,%s,%s,now())
                ON CONFLICT(task_id) DO UPDATE SET revision=EXCLUDED.revision,task_revision=EXCLUDED.task_revision,
                payload=EXCLUDED.payload,updated_at=now()""",
                (task_id, c["app_id"], c["user"], rev + 1, task["revision"], Jsonb(data)),
            )
            await event(conn, c, None, "CHECKPOINT_SAVED", {"task_id": str(task_id), "revision": rev + 1})
            return {"revision": rev + 1, "task_revision": task["revision"], "summary": data}

    @router.get("/tasks/{task_id}/checkpoint")
    async def handoff(task_id: UUID, request: Request):
        pool, c = await auth(request, "tasks:read")
        async with pool.connection() as conn, conn.transaction():
            task = await task_owned(conn, c, task_id)
            row = await (
                await conn.execute("SELECT * FROM task_checkpoints WHERE task_id=%s", (task_id,))
            ).fetchone()
            last = await (
                await conn.execute("SELECT max(id) n FROM chore_events WHERE task_id=%s", (task_id,))
            ).fetchone()
            execution = await (
                await conn.execute("SELECT payload FROM task_handoff_snapshots WHERE task_id=%s", (task_id,))
            ).fetchone()
        return {
            "checkpoint": row,
            "execution": execution["payload"] if execution else None,
            "task": task,
            "stale": bool(
                row
                and (
                    row["task_revision"] != task["revision"]
                    or row["payload"]["captured_event_id"] != last["n"]
                )
            ),
            "authority": "Current task state and tool evidence override the summary",
        }

    @router.post("/feedback", status_code=201)
    async def feedback(body: Feedback, request: Request):
        pool, c = await auth(request, "memory:write")
        fp = digest(body.model_dump(mode="json"))
        async with pool.connection() as conn, conn.transaction():
            if body.target_type == "task":
                await task_owned(conn, c, body.target_id)
            elif not await (
                await conn.execute(
                    """SELECT 1 FROM knowledge_answers
                WHERE id=%s AND app_id=%s AND owner_id=%s""",
                    (body.target_id, c["app_id"], c["user"]),
                )
            ).fetchone():
                raise HTTPException(404, "Answer not found")
            row = await (
                await conn.execute(
                    """INSERT INTO agent_outcome_feedback
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,now())
                ON CONFLICT(app_id,owner_id,request_key) DO NOTHING RETURNING *""",
                    (
                        uuid4(),
                        c["app_id"],
                        c["user"],
                        body.request_key,
                        fp,
                        body.target_type,
                        body.target_id,
                        body.outcome,
                        body.evidence_ref,
                        body.note,
                    ),
                )
            ).fetchone()
            if row is not None:
                await event(
                    conn,
                    c,
                    None,
                    "FEEDBACK_RECORDED",
                    {"feedback_id": str(row["id"]), "outcome": row["outcome"]},
                )
            if row is None:
                row = await (
                    await conn.execute(
                        """SELECT * FROM agent_outcome_feedback
                    WHERE app_id=%s AND owner_id=%s AND request_key=%s""",
                        (c["app_id"], c["user"], body.request_key),
                    )
                ).fetchone()
                if row["fingerprint"] != fp:
                    raise HTTPException(409, "Feedback request key already used")
            return {
                "id": row["id"],
                "outcome": row["outcome"],
                "training_allowed": False,
                "verification": "app_reported",
            }

    @router.get("/feedback")
    async def feedback_list(
        request: Request, before: UUID | None = None, limit: int = Query(default=50, ge=1, le=100)
    ):
        pool, c = await auth(request, "memory:read")
        async with pool.connection() as conn:
            rows = await (
                await conn.execute(
                    """SELECT * FROM agent_outcome_feedback WHERE app_id=%s AND owner_id=%s
                AND (%s::uuid IS NULL OR id<%s) ORDER BY id DESC LIMIT %s""",
                    (c["app_id"], c["user"], before, before, limit),
                )
            ).fetchall()
        return {
            "items": rows,
            "next_before": rows[-1]["id"] if len(rows) == limit else None,
            "verification": "app_reported",
            "training_allowed": False,
        }

    @router.get("/events")
    async def events(
        request: Request, after: int = Query(default=0, ge=0), limit: int = Query(default=50, ge=1, le=100)
    ):
        pool, c = await auth(request, "memory:read")
        async with pool.connection() as conn:
            rows = await (
                await conn.execute(
                    """SELECT * FROM platform_memory_events
                WHERE app_id=%s AND owner_id=%s AND id>%s ORDER BY id LIMIT %s""",
                    (c["app_id"], c["user"], after, limit),
                )
            ).fetchall()
        return {"items": rows, "next_after": rows[-1]["id"] if rows else after}

    app.include_router(router)
    from .memory_workflow import install_workflow

    install_workflow(app)
