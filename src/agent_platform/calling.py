"""Reusable inbound calling: owner-scoped setup, durable turns and signed app callbacks."""

import hashlib
import hmac
import json
import time
from typing import Literal
from uuid import UUID, uuid4

import httpx
from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import Field

from .calling_provider import settings, verify, xml_reply
from .calling_readiness import provider_readiness
from .service_identity import authenticate
from .tasks import Strict


class Agent(Strict):
    external_id: str = Field(min_length=1, max_length=100, pattern=r"^[a-zA-Z0-9_.:-]+$")
    name: str = Field(min_length=1, max_length=120)
    connection_ref: str | None = Field(default=None, min_length=1, max_length=100)
    language: Literal["en-IN", "en-US", "hi-IN"] = "en-IN"
    greeting: str = Field(min_length=1, max_length=800)
    handoff_message: str = Field(min_length=1, max_length=800)
    max_turns: int = Field(default=15, ge=1, le=30)


class Activation(Strict):
    enabled: bool


class AppReply(Strict):
    event_id: UUID
    action: Literal["continue", "end", "handoff"]
    text: str = Field(min_length=1, max_length=1200)


def urls(agent, cfg):
    base = cfg["public_url"].rstrip("/") + "/webhooks/calling/" + str(agent["id"])
    return {"inbound": base + "/inbound", "status": base + "/status"}


async def invoke(cfg, payload):
    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()
    stamp = str(int(time.time()))
    sig = hmac.new(cfg["handler_secret"].encode(), stamp.encode() + b"." + raw, hashlib.sha256).hexdigest()
    async with httpx.AsyncClient(timeout=httpx.Timeout(5, connect=2), follow_redirects=False) as client:
        response = await client.post(
            cfg["handler_url"],
            content=raw,
            headers={
                "Content-Type": "application/json",
                "X-Agent-Timestamp": stamp,
                "X-Agent-Signature": sig,
            },
        )
        response.raise_for_status()
        if len(response.content) > 16384:
            raise ValueError("Oversized app response")
        return response.json()


def install_calling(app):
    router = APIRouter()

    async def auth(request, write=False):
        pool = request.app.state.repository.pool
        claims = await authenticate(
            pool,
            request.headers.get("authorization", ""),
            request.headers.get("X-External-User-ID"),
            "channels:write" if write else "channels:read",
        )
        return pool, claims

    async def owned(conn, ident, claims, lock=False):
        row = await (
            await conn.execute(
                "SELECT * FROM calling_agents WHERE id=%s AND app_id=%s AND owner_id=%s"
                + (" FOR UPDATE" if lock else ""),
                (ident, claims["app_id"], claims["user"]),
            )
        ).fetchone()
        if not row:
            raise HTTPException(404, "Calling agent not found")
        return row

    async def audit(conn, ident, event, actor):
        await conn.execute(
            "INSERT INTO calling_audit(agent_id,event,actor) VALUES(%s,%s,%s)", (ident, event, actor)
        )

    @router.get("/v1/calling/capabilities")
    async def capabilities(request: Request):
        _, claims = await auth(request)
        return {
            "app_id": claims["app_id"],
            "owner_id": claims["user"],
            "providers": ["twilio"],
            "inbound": True,
            "outbound": False,
            "recording": False,
            "live_transfer": False,
            "runtime": "speech-or-keypad turns through signed app handler",
        }

    @router.post("/v1/calling/agents", status_code=201)
    async def create(body: Agent, request: Request):
        pool, claims = await auth(request, True)
        values = body.model_dump()
        agent = {**values, "id": uuid4(), "app_id": claims["app_id"], "owner_id": claims["user"]}
        if body.connection_ref:
            try:
                settings(agent)
            except (ValueError, TypeError):
                raise HTTPException(
                    409, "Operator must configure an authorized number and signed app handler"
                ) from None
        async with pool.connection() as conn, conn.transaction():
            await conn.execute("SELECT id FROM platform_apps WHERE id=%s FOR UPDATE", (claims["app_id"],))
            existing = await (
                await conn.execute(
                    "SELECT * FROM calling_agents WHERE app_id=%s AND owner_id=%s AND external_id=%s",
                    (claims["app_id"], claims["user"], body.external_id),
                )
            ).fetchone()
            if existing:
                if any(existing[k] != v for k, v in values.items()):
                    raise HTTPException(409, "External reference already used with different settings")
                return existing
            if (
                body.connection_ref
                and await (
                    await conn.execute(
                        "SELECT 1 FROM calling_agents WHERE connection_ref=%s", (body.connection_ref,)
                    )
                ).fetchone()
            ):
                raise HTTPException(409, "Connection already assigned")
            row = await (
                await conn.execute(
                    """INSERT INTO calling_agents
                (id,app_id,owner_id,external_id,name,connection_ref,language,greeting,handoff_message,max_turns)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
                    tuple(
                        agent[k]
                        for k in (
                            "id",
                            "app_id",
                            "owner_id",
                            "external_id",
                            "name",
                            "connection_ref",
                            "language",
                            "greeting",
                            "handoff_message",
                            "max_turns",
                        )
                    ),
                )
            ).fetchone()
            await audit(conn, row["id"], "created", claims["principal"])
        return row

    @router.put("/v1/calling/agents/{ident}")
    async def update(ident: UUID, body: Agent, request: Request):
        pool, claims = await auth(request, True)
        async with pool.connection() as conn, conn.transaction():
            row = await owned(conn, ident, claims, True)
            if (
                row["enabled"]
                or await (
                    await conn.execute(
                        "SELECT 1 FROM calling_calls WHERE agent_id=%s AND state='ACTIVE'", (ident,)
                    )
                ).fetchone()
            ):
                raise HTTPException(409, "Pause and finish active calls before changing settings")
            if body.external_id != row["external_id"]:
                raise HTTPException(409, "External reference cannot change")
            if body.connection_ref:
                try:
                    settings({**row, **body.model_dump()})
                except (ValueError, TypeError):
                    raise HTTPException(409, "Operator must configure the authorized connection") from None
                if await (
                    await conn.execute(
                        "SELECT 1 FROM calling_agents WHERE connection_ref=%s AND id<>%s",
                        (body.connection_ref, ident),
                    )
                ).fetchone():
                    raise HTTPException(409, "Connection already assigned")
            keys = ("name", "connection_ref", "language", "greeting", "handoff_message", "max_turns")
            row = await (
                await conn.execute(
                    "UPDATE calling_agents SET "
                    + ",".join(k + "=%s" for k in keys)
                    + " WHERE id=%s RETURNING *",
                    tuple(getattr(body, k) for k in keys) + (ident,),
                )
            ).fetchone()
            await audit(conn, ident, "updated", claims["principal"])
        return row

    @router.get("/v1/calling/agents")
    async def listing(request: Request, before: UUID | None = None):
        pool, claims = await auth(request)
        async with pool.connection() as conn:
            rows = await (
                await conn.execute(
                    """SELECT * FROM calling_agents WHERE app_id=%s AND owner_id=%s
                AND (%s::uuid IS NULL OR id<%s) ORDER BY id DESC LIMIT 51""",
                    (claims["app_id"], claims["user"], before, before),
                )
            ).fetchall()
        return {"agents": rows[:50], "next_before": str(rows[49]["id"]) if len(rows) > 50 else None}

    @router.get("/v1/calling/agents/{ident}/setup")
    async def setup(ident: UUID, request: Request):
        pool, claims = await auth(request)
        async with pool.connection() as conn:
            row = await owned(conn, ident, claims)
        try:
            cfg = settings(row)
        except (ValueError, TypeError):
            return {
                "configured": False,
                "enabled": False,
                "next_step": "Operator connects an authorized voice number and app handler",
            }
        return {
            "configured": True,
            "enabled": row["enabled"],
            "number": cfg["number"],
            "webhooks": urls(row, cfg),
            "method": "POST",
            "next_step": "Configure provider webhooks, then enable and make a real inbound test call",
        }

    @router.post("/v1/calling/agents/{ident}/preview")
    async def preview(ident: UUID, request: Request):
        pool, claims = await auth(request)
        async with pool.connection() as conn:
            row = await owned(conn, ident, claims)
        return {
            "simulation": True,
            "provider_called": False,
            "greeting": row["greeting"],
            "language": row["language"],
            "handoff_message": row["handoff_message"],
            "max_turns": row["max_turns"],
            "recording": False,
        }

    @router.get("/v1/calling/agents/{ident}/readiness")
    async def readiness(ident: UUID, request: Request):
        pool, claims = await auth(request)
        async with pool.connection() as conn:
            row = await owned(conn, ident, claims)
        try:
            cfg = settings(row)
        except (ValueError, TypeError):
            return {"ready": False, "code": "configuration", "message": "Your setup team must connect an authorized incoming voice number.", "enabled": row["enabled"], "live_call_verified": False}
        provider = await provider_readiness(cfg, urls(row, cfg))
        if not provider["ready"]:
            return {**provider, "enabled": row["enabled"], "live_call_verified": False}
        probe = str(uuid4())
        try:
            answer = await invoke(cfg, {"probe": True, "event_id": probe, "agent_id": str(ident), "app_id": str(row["app_id"])})
            if answer != {"ready": True, "event_id": probe}:
                raise ValueError()
        except (httpx.HTTPError, ValueError, TypeError, KeyError, TimeoutError):
            return {"ready": False, "code": "handler", "message": "The connected app is unavailable. Restore it before enabling calls.", "enabled": row["enabled"], "live_call_verified": False}
        return {**provider, "enabled": row["enabled"], "live_call_verified": False}

    @router.post("/v1/calling/agents/{ident}/activation")
    async def activation(ident: UUID, body: Activation, request: Request):
        pool, claims = await auth(request, True)
        async with pool.connection() as conn, conn.transaction():
            row = await owned(conn, ident, claims, True)
            if body.enabled:
                try:
                    cfg = settings(row)
                    provider = await provider_readiness(cfg, urls(row, cfg))
                    if not provider["ready"]:
                        raise HTTPException(409, provider["message"])
                    probe_id = str(uuid4())
                    result = await invoke(
                        cfg,
                        {
                            "probe": True,
                            "event_id": probe_id,
                            "agent_id": str(ident),
                            "app_id": str(row["app_id"]),
                        },
                    )
                    if result != {"ready": True, "event_id": probe_id}:
                        raise ValueError("Handler not ready")
                except (httpx.HTTPError, ValueError, TypeError, KeyError, TimeoutError):
                    raise HTTPException(
                        409, "Authorized provider connection and reachable app handler required"
                    ) from None
            await conn.execute("UPDATE calling_agents SET enabled=%s WHERE id=%s", (body.enabled, ident))
            await audit(conn, ident, "enabled:" + str(body.enabled), claims["principal"])
        return {"enabled": body.enabled, "live_call_verified": False}

    @router.get("/v1/calling/agents/{ident}/calls")
    async def history(ident: UUID, request: Request, before: UUID | None = None):
        pool, claims = await auth(request)
        async with pool.connection() as conn:
            await owned(conn, ident, claims)
            rows = await (
                await conn.execute(
                    """SELECT id,state,last_turn,duration_seconds,created_at,updated_at
                FROM calling_calls WHERE agent_id=%s AND (%s::uuid IS NULL OR id<%s) ORDER BY id DESC LIMIT 51""",
                    (ident, before, before),
                )
            ).fetchall()
        return {"calls": rows[:50], "next_before": str(rows[49]["id"]) if len(rows) > 50 else None}

    async def inbound_context(ident, request):
        pool = request.app.state.repository.pool
        async with pool.connection() as conn:
            row = await (await conn.execute("SELECT * FROM calling_agents WHERE id=%s", (ident,))).fetchone()
        if not row:
            raise HTTPException(404)
        try:
            cfg = settings(row)
        except (ValueError, TypeError):
            raise HTTPException(503, "Calling connection unavailable") from None
        raw = bytearray()
        async for part in request.stream():
            raw.extend(part)
            if len(raw) > 65536:
                raise HTTPException(413)
        try:
            if request.url.query:
                raise ValueError("Unexpected query")
            params = verify(bytes(raw), request.headers, cfg, request.url.path)
        except (ValueError, UnicodeError):
            raise HTTPException(403, "Invalid calling webhook") from None
        return pool, row, cfg, params

    def response(xml):
        return Response(xml, media_type="application/xml", headers={"Cache-Control": "no-store"})

    @router.post("/webhooks/calling/{ident}/inbound")
    async def inbound(ident: UUID, request: Request):
        pool, agent, cfg, params = await inbound_context(ident, request)
        if params.get("Direction") != "inbound":
            raise HTTPException(403, "Only inbound calls are supported")
        if not agent["enabled"]:
            return response(xml_reply(agent["handoff_message"], agent["language"]))
        async with pool.connection() as conn, conn.transaction():
            await conn.execute(
                """INSERT INTO calling_calls(id,agent_id,provider_call_id,caller)
                VALUES(%s,%s,%s,%s) ON CONFLICT(agent_id,provider_call_id) DO NOTHING""",
                (uuid4(), ident, params["CallSid"], params.get("From", "anonymous")[:100]),
            )
            call = await (
                await conn.execute(
                    "SELECT * FROM calling_calls WHERE agent_id=%s AND provider_call_id=%s",
                    (ident, params["CallSid"]),
                )
            ).fetchone()
        action = (
            urls(agent, cfg)["inbound"].replace("/inbound", "/turn/1")
            if call["state"] == "ACTIVE" and call["last_turn"] == 0
            else None
        )
        return response(
            xml_reply(agent["greeting"] if action else agent["handoff_message"], agent["language"], action)
        )

    @router.post("/webhooks/calling/{ident}/turn/{turn}")
    async def turn(ident: UUID, turn: int, request: Request):
        pool, agent, cfg, params = await inbound_context(ident, request)
        fallback = xml_reply(agent["handoff_message"], agent["language"])
        text = (params.get("SpeechResult") or params.get("Digits") or "").strip()
        digest = hashlib.sha256(text.encode()).hexdigest()
        event = uuid4()
        async with pool.connection() as conn, conn.transaction():
            call = await (
                await conn.execute(
                    "SELECT * FROM calling_calls WHERE agent_id=%s AND provider_call_id=%s FOR UPDATE",
                    (ident, params["CallSid"]),
                )
            ).fetchone()
            if not call:
                raise HTTPException(404)
            previous = await (
                await conn.execute(
                    "SELECT * FROM calling_turns WHERE call_id=%s AND turn=%s", (call["id"], turn)
                )
            ).fetchone()
            if previous:
                if previous["request_hash"] != digest:
                    raise HTTPException(409, "Conflicting call turn")
                return response(previous["response_xml"] or fallback)
            if (
                not agent["enabled"]
                or call["state"] != "ACTIVE"
                or turn != call["last_turn"] + 1
                or turn > agent["max_turns"]
                or not text
                or len(text) > 4000
                or time.time() - call["created_at"].timestamp() > 900
            ):
                await conn.execute(
                    "UPDATE calling_calls SET state='HANDOFF',updated_at=now() WHERE id=%s AND state='ACTIVE'",
                    (call["id"],),
                )
                return response(fallback)
            if await (
                await conn.execute(
                    "SELECT 1 FROM calling_turns WHERE call_id=%s AND state IN ('PROCESSING','UNKNOWN')",
                    (call["id"],),
                )
            ).fetchone():
                return response(fallback)
            await conn.execute(
                "INSERT INTO calling_turns(id,call_id,turn,request_hash,state) VALUES(%s,%s,%s,%s,'PROCESSING')",
                (event, call["id"], turn, digest),
            )
            await conn.execute(
                "UPDATE calling_calls SET last_turn=%s,updated_at=now() WHERE id=%s", (turn, call["id"])
            )
        # PROCESSING committed before network IO: crash/timeout never invokes this turn twice.
        try:
            result = AppReply.model_validate(
                await invoke(
                    cfg,
                    {
                        "probe": False,
                        "event_id": str(event),
                        "call_id": str(call["id"]),
                        "agent_id": str(ident),
                        "app_id": str(agent["app_id"]),
                        "caller": call["caller"],
                        "input": text,
                        "input_trust": "untrusted_caller_data",
                        "turn": turn,
                        "language": agent["language"],
                    },
                )
            )
            if result.event_id != event:
                raise ValueError("Wrong event")
            action = (
                urls(agent, cfg)["inbound"].replace("/inbound", f"/turn/{turn + 1}")
                if result.action == "continue" and turn < agent["max_turns"]
                else None
            )
            xml = xml_reply(result.text, agent["language"], action)
            state = "ACTIVE" if action else "HANDOFF" if result.action == "handoff" else "COMPLETED"
            if result.action == "continue" and not action:
                xml, state = fallback, "HANDOFF"
            turn_state = "DONE"
        except (httpx.HTTPError, ValueError, TypeError, KeyError, TimeoutError):
            xml, state, turn_state = fallback, "HANDOFF", "UNKNOWN"
        async with pool.connection() as conn, conn.transaction():
            current = await (
                await conn.execute("SELECT state FROM calling_calls WHERE id=%s FOR UPDATE", (call["id"],))
            ).fetchone()
            enabled = await (
                await conn.execute("SELECT enabled FROM calling_agents WHERE id=%s", (ident,))
            ).fetchone()
            if current["state"] != "ACTIVE" or not enabled["enabled"]:
                xml = fallback
                state = "HANDOFF"
            await conn.execute(
                "UPDATE calling_turns SET state=%s,response_xml=%s WHERE id=%s", (turn_state, xml, event)
            )
            await conn.execute(
                "UPDATE calling_calls SET state=%s,updated_at=now() WHERE id=%s AND state='ACTIVE'",
                (state, call["id"]),
            )
        return response(xml)

    @router.post("/webhooks/calling/{ident}/status")
    async def status(ident: UUID, request: Request):
        pool, _, _, params = await inbound_context(ident, request)
        try:
            seq = int(params.get("SequenceNumber", "-1"))
            duration = int(params.get("CallDuration", "0"))
            if seq < 0 or not 0 <= duration <= 86400:
                raise ValueError()
        except ValueError:
            raise HTTPException(422, "Invalid call status") from None
        status = params.get("CallStatus")
        if status not in {"completed", "busy", "failed", "no-answer", "canceled"}:
            return {"received": True}
        async with pool.connection() as conn, conn.transaction():
            # Preserve terminal events even when they precede a delayed inbound delivery.
            await conn.execute(
                """INSERT INTO calling_calls(id,agent_id,provider_call_id,caller,state)
                VALUES(%s,%s,%s,%s,%s) ON CONFLICT(agent_id,provider_call_id) DO NOTHING""",
                (
                    uuid4(),
                    ident,
                    params["CallSid"],
                    params.get("From", "anonymous")[:100],
                    "COMPLETED" if status == "completed" else "FAILED",
                ),
            )
            await conn.execute(
                """UPDATE calling_calls SET state=CASE WHEN state='HANDOFF' THEN state ELSE %s END,
                duration_seconds=%s,status_sequence=%s,updated_at=now() WHERE agent_id=%s AND provider_call_id=%s AND status_sequence<%s""",
                (
                    "COMPLETED" if status == "completed" else "FAILED",
                    duration,
                    seq,
                    ident,
                    params["CallSid"],
                    seq,
                ),
            )
        return {"received": True}

    app.include_router(router)
