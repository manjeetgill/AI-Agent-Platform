"""Reusable WhatsApp calling management; business apps retain their own identities."""

import hashlib
import hmac
import json
import time
from typing import Literal
from uuid import UUID, uuid4

import httpx
from fastapi import APIRouter, HTTPException, Request
from pydantic import Field

from . import calling_provider as provider
from .meta_worker import managed_config
from .service_identity import authenticate
from .tasks import Strict


class Agent(Strict):
    external_id: str = Field(min_length=1, max_length=100, pattern=r"^[a-zA-Z0-9_.:-]+$")
    name: str = Field(min_length=1, max_length=120)
    connection_ref: None = None  # Retained in the request contract; retired registries cannot be attached.
    language: Literal["en-IN", "en-US", "hi-IN"] = "en-IN"
    greeting: str = Field(min_length=1, max_length=800)
    handoff_message: str = Field(min_length=1, max_length=800)
    max_turns: int = Field(default=15, ge=1, le=30)


class Activation(Strict):
    enabled: bool


class Connection(Strict):
    channel_id: UUID
    authorized_to_connect: Literal[True]


class TestCall(Strict):
    recipient: str = Field(pattern=r"^\+[1-9]\d{7,14}$")
    idempotency_key: str = Field(min_length=8, max_length=100, pattern=r"^[a-zA-Z0-9_.:-]+$")


class Approval(Strict):
    approved: Literal[True]


async def invoke(cfg, payload):
    """Stable signed business-handler contract shared by apps, including Sahayak."""
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


async def connected(pool, agent):
    async with pool.connection() as conn:
        row = await (
            await conn.execute(
                "SELECT * FROM whatsapp_channels WHERE id=%s AND app_id=%s AND owner_id=%s",
                (agent["whatsapp_channel_id"], agent["app_id"], agent["owner_id"]),
            )
        ).fetchone()
    if not row:
        raise HTTPException(409, "Connect an authorized WhatsApp business number first")
    cfg = await managed_config(pool, row["phone_id"], row["app_id"], row["owner_id"], provider=row.get("provider"))
    return row, cfg


def configuration_hash(agent):
    return hashlib.sha256(
        json.dumps(
            {
                k: str(agent[k])
                for k in ("id", "whatsapp_channel_id", "greeting", "language", "handoff_message", "max_turns")
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()


async def check_ready(pool, agent):
    try:
        channel, cfg = await connected(pool, agent)
    except HTTPException:
        return {
            "ready": False,
            "code": "whatsapp_connection",
            "message": "Connect your WhatsApp business number first.",
        }
    result = await provider.readiness(cfg, channel["phone_id"])
    if not result["ready"]:
        return result
    async with pool.connection() as conn:
        worker = await (
            await conn.execute(
                "SELECT 1 FROM whatsapp_call_worker_health WHERE last_seen>now()-interval '30 seconds' AND media_ready"
            )
        ).fetchone()
    if not worker:
        return {
            "ready": False,
            "code": "voice_runtime",
            "message": "WhatsApp number connected. The platform's live audio worker is not ready.",
        }
    import os

    from .calling_dialogue import handler

    if channel["execution_mode"] == "SAHAYAK" or str(agent["id"]) in json.loads(
        os.environ.get("AI_CALLING_APP_HANDLERS", "{}")
    ):
        try:
            cfg = handler(agent)
            event = str(uuid4())
            reply = await invoke(
                cfg,
                {
                    "probe": True,
                    "event_id": event,
                    "agent_id": str(agent["id"]),
                    "app_id": str(agent["app_id"]),
                },
            )
            if reply != {"ready": True, "event_id": event}:
                raise ValueError()
        except (ValueError, KeyError, TypeError, httpx.HTTPError):
            return {
                "ready": False,
                "code": "app_handler",
                "message": "Your app's business-action handler is unavailable or not configured.",
            }
    return {
        "ready": True,
        "code": "ready",
        "message": "WhatsApp calling and the audio worker are ready. Complete a real call test.",
    }


def call_view(row):
    return {k: row[k] for k in ("id", "recipient", "state", "created_at")} | {
        "summary": f"Make one WhatsApp agent test call to {row['recipient']}, with a 2-minute local audio limit. Meta/provider and AI usage charges may apply. No recording.",
        "max_duration_seconds": 120,
    }


def install_calling(app):
    router = APIRouter()

    async def auth(request, write=False):
        pool = request.app.state.repository.pool
        claims = await authenticate(
            pool,
            request.headers.get("authorization", ""),
            request.headers.get("X-External-User-ID"),
            "calling:write" if write else "calling:read",
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

    @router.get("/v1/calling/capabilities")
    async def capabilities(request: Request):
        _, claims = await auth(request)
        return {
            "app_id": claims["app_id"],
            "owner_id": claims["user"],
            "providers": ["meta", "chakra"],
            "channel": "whatsapp",
            "ordinary_phone_calls": False,
            "inbound": True,
            "outbound": "approved_test_only",
            "recording": False,
            "live_transfer": False,
            "requires_audio_worker": True,
        }

    @router.post("/v1/calling/agents", status_code=201)
    async def create(body: Agent, request: Request):
        pool, claims = await auth(request, True)
        values = body.model_dump()
        async with pool.connection() as conn, conn.transaction():
            await conn.execute("SELECT id FROM platform_apps WHERE id=%s FOR UPDATE", (claims["app_id"],))
            row = await (
                await conn.execute(
                    "SELECT * FROM calling_agents WHERE app_id=%s AND owner_id=%s AND external_id=%s",
                    (claims["app_id"], claims["user"], body.external_id),
                )
            ).fetchone()
            if row:
                if any(row[k] != v for k, v in values.items()):
                    raise HTTPException(409, "External reference already used with different settings")
                return row
            keys = ("external_id", "name", "language", "greeting", "handoff_message", "max_turns")
            row = await (
                await conn.execute(
                    "INSERT INTO calling_agents(id,app_id,owner_id,external_id,name,language,greeting,handoff_message,max_turns) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *",
                    (uuid4(), claims["app_id"], claims["user"], *(values[k] for k in keys)),
                )
            ).fetchone()
        return row

    @router.get("/v1/calling/agents")
    async def listing(request: Request, before: UUID | None = None):
        pool, claims = await auth(request)
        async with pool.connection() as conn:
            rows = await (
                await conn.execute(
                    "SELECT * FROM calling_agents WHERE app_id=%s AND owner_id=%s AND (%s::uuid IS NULL OR id<%s) ORDER BY id DESC LIMIT 51",
                    (claims["app_id"], claims["user"], before, before),
                )
            ).fetchall()
        return {"agents": rows[:50], "next_before": str(rows[49]["id"]) if len(rows) > 50 else None}

    @router.post("/v1/calling/agents/{ident}/connection")
    async def connect(ident: UUID, body: Connection, request: Request):
        pool, claims = await auth(request, True)
        async with pool.connection() as conn, conn.transaction():
            # Lock the channel as well to serialize competing bindings from two agents.
            channel = await (
                await conn.execute(
                    "SELECT * FROM whatsapp_channels WHERE id=%s AND app_id=%s AND owner_id=%s FOR UPDATE",
                    (body.channel_id, claims["app_id"], claims["user"]),
                )
            ).fetchone()
            if not channel:
                raise HTTPException(404, "WhatsApp connection not found")
            agent = await owned(conn, ident, claims, True)
            if agent["enabled"]:
                raise HTTPException(409, "Pause the agent before changing its number")
            if await (
                await conn.execute(
                    "SELECT 1 FROM calling_agents WHERE whatsapp_channel_id=%s AND id<>%s",
                    (body.channel_id, ident),
                )
            ).fetchone():
                raise HTTPException(409, "WhatsApp number already has a calling agent")
            await conn.execute(
                "UPDATE calling_agents SET whatsapp_channel_id=%s,connection_ref=NULL WHERE id=%s",
                (body.channel_id, ident),
            )
            await conn.execute(
                "INSERT INTO calling_audit(agent_id,event,actor) VALUES(%s,'whatsapp_connected',%s)",
                (ident, claims["principal"]),
            )
        return {
            "connected": True,
            "channel": "whatsapp",
            "enabled": False,
            "next_step": "Check calling eligibility and audio readiness",
        }

    @router.get("/v1/calling/agents/{ident}/setup")
    @router.get("/v1/calling/agents/{ident}/connection")
    async def setup(ident: UUID, request: Request):
        pool, claims = await auth(request)
        async with pool.connection() as conn:
            agent = await owned(conn, ident, claims)
        return {
            "configured": bool(agent["whatsapp_channel_id"]),
            "enabled": agent["enabled"],
            "channel": "whatsapp",
            "channel_id": agent["whatsapp_channel_id"],
            "next_step": "Check WhatsApp calling readiness"
            if agent["whatsapp_channel_id"]
            else "Select your connected WhatsApp business number",
        }

    @router.get("/v1/calling/agents/{ident}/readiness")
    async def readiness(ident: UUID, request: Request):
        pool, claims = await auth(request)
        async with pool.connection() as conn:
            agent = await owned(conn, ident, claims)
        return {**await check_ready(pool, agent), "enabled": agent["enabled"], "live_call_verified": False}

    @router.post("/v1/calling/agents/{ident}/activation")
    async def activate(ident: UUID, body: Activation, request: Request):
        pool, claims = await auth(request, True)
        async with pool.connection() as conn, conn.transaction():
            agent = await owned(conn, ident, claims, True)
            if body.enabled:
                ready = await check_ready(pool, agent)
                if not ready["ready"]:
                    raise HTTPException(409, ready["message"])
            await conn.execute("UPDATE calling_agents SET enabled=%s WHERE id=%s", (body.enabled, ident))
            await conn.execute(
                "INSERT INTO calling_audit(agent_id,event,actor) VALUES(%s,%s,%s)",
                (ident, "enabled:" + str(body.enabled), claims["principal"]),
            )
        return {"enabled": body.enabled, "live_call_verified": False}

    @router.post("/v1/calling/agents/{ident}/provider-settings")
    async def enable_provider(ident: UUID, body: Approval, request: Request):
        pool, claims = await auth(request, True)
        async with pool.connection() as conn:
            agent = await owned(conn, ident, claims)
        channel, cfg = await connected(pool, agent)
        current = await provider.readiness(cfg, channel["phone_id"])
        if current["code"] != "calling_disabled":
            return current  # Never change SIP routing or overwrite an unknown configuration.
        async with pool.connection() as conn:
            await conn.execute(
                "INSERT INTO calling_audit(agent_id,event,actor) VALUES(%s,'whatsapp_calling_enable_requested',%s)",
                (ident, claims["principal"]),
            )
        try:
            await provider.request(
                cfg,
                channel["phone_id"],
                "settings",
                method="POST",
                payload={"calling": {"status": "ENABLED"}},
            )
        except provider.CallingError:
            pass  # Read the real state before suggesting any further action.
        return await provider.readiness(cfg, channel["phone_id"])

    @router.post("/v1/calling/agents/{ident}/preview")
    async def preview(ident: UUID, request: Request):
        pool, claims = await auth(request)
        async with pool.connection() as conn:
            agent = await owned(conn, ident, claims)
        return {
            "simulation": True,
            "provider_called": False,
            **{k: agent[k] for k in ("greeting", "language", "handoff_message", "max_turns")},
        }

    @router.get("/v1/calling/agents/{ident}/calls")
    async def history(ident: UUID, request: Request, before: UUID | None = None):
        pool, claims = await auth(request)
        async with pool.connection() as conn:
            await owned(conn, ident, claims)
            rows = await (
                await conn.execute(
                    "SELECT id,state,last_turn,duration_seconds,created_at,updated_at FROM calling_calls WHERE agent_id=%s AND (%s::uuid IS NULL OR id<%s) ORDER BY id DESC LIMIT 51",
                    (ident, before, before),
                )
            ).fetchall()
        return {"calls": rows[:50], "next_before": str(rows[49]["id"]) if len(rows) > 50 else None}

    @router.get("/v1/calling/agents/{ident}/permission")
    async def permission(ident: UUID, recipient: str, request: Request):
        pool, claims = await auth(request)
        async with pool.connection() as conn:
            agent = await owned(conn, ident, claims)
        channel, cfg = await connected(pool, agent)
        try:
            return await provider.permission(cfg, channel["phone_id"], recipient)
        except provider.CallingError:
            raise HTTPException(409, "Unable to verify recipient's WhatsApp call permission") from None

    @router.post("/v1/calling/agents/{ident}/test-calls", status_code=201)
    async def test_call(ident: UUID, body: TestCall, request: Request):
        pool, claims = await auth(request, True)
        async with pool.connection() as conn, conn.transaction():
            agent = await owned(conn, ident, claims, True)
            prior = await (
                await conn.execute(
                    "SELECT * FROM whatsapp_call_requests WHERE agent_id=%s AND idempotency_key=%s",
                    (ident, body.idempotency_key),
                )
            ).fetchone()
            if prior:
                if prior["recipient"] != body.recipient:
                    raise HTTPException(409, "Request already used for another recipient")
                return call_view(prior)
            if not agent["enabled"]:
                raise HTTPException(409, "Enable the WhatsApp calling agent first")
            row = await (
                await conn.execute(
                    "INSERT INTO whatsapp_call_requests(id,agent_id,recipient,idempotency_key,state,configuration_hash) VALUES(%s,%s,%s,%s,'AWAITING_APPROVAL',%s) RETURNING *",
                    (uuid4(), ident, body.recipient, body.idempotency_key, configuration_hash(agent)),
                )
            ).fetchone()
        return call_view(row)

    @router.get("/v1/calling/agents/{ident}/test-calls/{test_id}")
    async def get_test(ident: UUID, test_id: UUID, request: Request):
        pool, claims = await auth(request)
        async with pool.connection() as conn:
            await owned(conn, ident, claims)
            row = await (
                await conn.execute(
                    "SELECT * FROM whatsapp_call_requests WHERE id=%s AND agent_id=%s", (test_id, ident)
                )
            ).fetchone()
        if not row:
            raise HTTPException(404)
        return call_view(row)

    @router.post("/v1/calling/agents/{ident}/test-calls/{test_id}/approve", status_code=202)
    async def approve_test(ident: UUID, test_id: UUID, body: Approval, request: Request):
        pool, claims = await auth(request, True)
        async with pool.connection() as conn, conn.transaction():
            await conn.execute("SELECT id FROM platform_apps WHERE id=%s FOR UPDATE", (claims["app_id"],))
            agent = await owned(conn, ident, claims, True)
            row = await (
                await conn.execute(
                    "SELECT *,created_at>now()-interval '5 minutes' AS fresh FROM whatsapp_call_requests WHERE id=%s AND agent_id=%s FOR UPDATE",
                    (test_id, ident),
                )
            ).fetchone()
            if not row:
                raise HTTPException(404)
            if row["state"] != "AWAITING_APPROVAL":
                return call_view(row)
            if (
                not row["fresh"]
                or not agent["enabled"]
                or row["configuration_hash"] != configuration_hash(agent)
            ):
                raise HTTPException(409, "Approval expired, agent paused or configuration changed")
            ready = await check_ready(pool, agent)
            if not ready["ready"]:
                raise HTTPException(409, ready["message"])
            limits = await (
                await conn.execute(
                    "SELECT count(*) FILTER (WHERE r.approved_by IS NOT NULL AND r.created_at>now()-interval '24 hours') AS daily,count(*) FILTER (WHERE r.state IN ('QUEUED','SENDING','SUBMITTED','CONNECTED','UNKNOWN')) AS pending FROM whatsapp_call_requests r JOIN calling_agents a ON a.id=r.agent_id WHERE a.app_id=%s",
                    (claims["app_id"],),
                )
            ).fetchone()
            if limits["pending"] or limits["daily"] >= 3:
                raise HTTPException(409, "One active test per app and three approved tests per 24 hours")
            row = await (
                await conn.execute(
                    "UPDATE whatsapp_call_requests SET state='QUEUED',approved_by=%s,updated_at=now() WHERE id=%s RETURNING *",
                    (claims["principal"], test_id),
                )
            ).fetchone()
        return call_view(row)

    app.include_router(router)
