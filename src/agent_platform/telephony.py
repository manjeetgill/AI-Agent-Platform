"""BYOC control plane and bounded inbound voice sessions for app-owned gateways.

A gateway key is bound to exactly one app, owner, number and agent. It cannot choose
another identity, place calls, or grant itself access to business tools. Carrier verification
is an operator operation, never a boolean supplied by a consuming app.
"""

import asyncio
import hashlib
import hmac
import secrets
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID, uuid4

from cryptography.exceptions import InvalidTag
from fastapi import APIRouter, HTTPException, Query, Request, Response
from psycopg.types.json import Jsonb
from pydantic import Field

from . import telephony_tools, voice_conversation  # noqa: F401 -- shared legacy conversation hook
from .service_identity import authenticate
from .tasks import Strict
from .web_voice import voice_gateway


class Connection(Strict):
    external_id: str = Field(min_length=1, max_length=100)
    agent_id: UUID
    number: str = Field(pattern=r"^\+[1-9]\d{7,14}$")


class Activation(Strict):
    enabled: bool


class Start(Strict):
    call_id: UUID
    # The app's authenticated gateway asserts its configured DID, never a caller identity.
    number: str = Field(pattern=r"^\+[1-9]\d{7,14}$")


class Turn(Strict):
    text: str = Field(min_length=1, max_length=1500)


class End(Strict):
    state: Literal["ENDED", "FAILED", "UNKNOWN"]


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def public(row):
    return {
        k: row[k]
        for k in (
            "id",
            "external_id",
            "agent_id",
            "number",
            "enabled",
            "carrier_verified_at",
            "heartbeat_at",
            "created_at",
        )
    }


async def audit(conn, ident, event, actor):
    await conn.execute(
        "INSERT INTO telephony_audit(connection_id,event,actor) VALUES(%s,%s,%s)", (ident, event, actor)
    )


async def verify_carrier(pool, ident, reference, *, actor, verified=True):
    """Operator-only after inspecting the customer's carrier authorization/routing evidence.

    reference is an opaque support/change-ticket identifier, not credentials/documents.
    Excluded from the HTTP API so a customer cannot self-verify an arbitrary number.
    """
    if not reference.strip() or len(reference) > 200 or not actor.strip():
        raise ValueError("An operator and evidence reference are required")
    async with pool.connection() as conn, conn.transaction():
        row = await (
            await conn.execute("SELECT * FROM telephony_connections WHERE id=%s FOR UPDATE", (ident,))
        ).fetchone()
        if not row:
            raise ValueError("Connection not found")
        await conn.execute(
            """UPDATE telephony_connections SET carrier_verified_at=%s,
            verification_reference=%s,enabled=false WHERE id=%s""",
            (datetime.now(UTC) if verified else None, reference, ident),
        )
        await conn.execute(
            "UPDATE telephony_sessions SET state='UNKNOWN',ended_at=now() WHERE connection_id=%s AND state='ACTIVE'",
            (ident,),
        )
        await audit(conn, ident, "carrier_verified" if verified else "carrier_revoked", actor)


async def checks(conn, row):
    agent = await (
        await conn.execute(
            """SELECT * FROM web_voice_agents WHERE id=%s AND app_id=%s
        AND (owner_id=%s OR visibility='app')""",
            (row["agent_id"], row["app_id"], row["owner_id"]),
        )
    ).fetchone()
    blockers = []
    if not row["carrier_verified_at"]:
        blockers.append("carrier_authorization_required")
    if not row["heartbeat_at"] or row["heartbeat_at"] < datetime.now(UTC) - timedelta(seconds=90):
        blockers.append("gateway_offline")
    if not agent or not agent["enabled"]:
        blockers.append("agent_unavailable")
    try:
        voice_gateway()
    except ValueError:
        blockers.append("model_configuration_required")
    cfg = await telephony_tools.current_configuration(conn, row)
    if cfg:
        try:
            telephony_tools.allowed_endpoint(row["app_id"], cfg["endpoint"])
            telephony_tools.unseal(cfg["secret_cipher"], telephony_tools.context(row))
        except (ValueError, KeyError, InvalidTag):
            blockers.append("business_tools_unavailable")
    return blockers, agent


def install_telephony(app):
    router = APIRouter(prefix="/v1/telephony", tags=["App-owned telephone connections"])

    async def context(request, scope):
        pool = request.app.state.repository.pool
        claims = await authenticate(
            pool, request.headers.get("authorization"), request.headers.get("x-external-user-id"), scope
        )
        return pool, claims

    async def owned(conn, ident, claims):
        row = await (
            await conn.execute(
                """SELECT * FROM telephony_connections
            WHERE id=%s AND app_id=%s AND owner_id=%s FOR UPDATE""",
                (ident, claims["app_id"], claims["user"]),
            )
        ).fetchone()
        if not row:
            raise HTTPException(404, "Connection not found")
        return row

    async def gateway(conn, ident, request):
        value = request.headers.get("authorization", "")
        if not value.startswith("Bearer tgw_") or len(value) > 200:
            raise HTTPException(401, "Invalid gateway credential")
        row = await (
            await conn.execute("SELECT * FROM telephony_connections WHERE id=%s FOR UPDATE", (ident,))
        ).fetchone()
        if not row or not hmac.compare_digest(row["gateway_hash"], digest(value[7:])):
            raise HTTPException(401, "Invalid gateway credential")
        return row

    @router.get("/capabilities")
    async def capabilities(request: Request):
        await context(request, "telephony:read")
        return {
            "carrier_account_owner": "consuming_app",
            "adapter": "asterisk_audiosocket",
            "inbound_faq": True,
            "outbound_calls": False,
            "business_mutations": "app_granted_tools_with_exact_caller_confirmation",
            "speech_owner": "platform",
            "protected_tools_require_app_verified_subject": True,
            "number_only_activation": False,
            "carrier_verification": "operator_review",
            "max_call_seconds": 120,
            "max_turns": 15,
            "raw_audio_stored": False,
            "transcripts_stored": True,
        }

    @router.post("/connections", status_code=201)
    async def create(body: Connection, request: Request, response: Response):
        pool, claims = await context(request, "telephony:write")
        ident, key = uuid4(), "tgw_" + secrets.token_urlsafe(48)
        async with pool.connection() as conn, conn.transaction():
            agent = await (
                await conn.execute(
                    """SELECT id FROM web_voice_agents WHERE id=%s
                AND app_id=%s AND (owner_id=%s OR visibility='app')""",
                    (body.agent_id, claims["app_id"], claims["user"]),
                )
            ).fetchone()
            if not agent:
                raise HTTPException(404, "Voice agent not found")
            row = await (
                await conn.execute(
                    """INSERT INTO telephony_connections
                (id,app_id,owner_id,external_id,agent_id,number,gateway_hash) VALUES(%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT(app_id,owner_id,external_id) DO NOTHING RETURNING *""",
                    (
                        ident,
                        claims["app_id"],
                        claims["user"],
                        body.external_id,
                        body.agent_id,
                        body.number,
                        digest(key),
                    ),
                )
            ).fetchone()
            if not row:
                raise HTTPException(
                    409, "External ID already exists; list connections or rotate its gateway key"
                )
            await conn.execute(
                "INSERT INTO telephony_app_limits(app_id) VALUES(%s) ON CONFLICT DO NOTHING",
                (claims["app_id"],),
            )
            await audit(conn, ident, "created", claims["principal"])
        response.headers["Cache-Control"] = "no-store"
        return {
            **public(row),
            "gateway_key": key,
            "next_step": "configure_app_gateway_and_request_carrier_verification",
        }

    @router.get("/connections")
    async def listing(request: Request, limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0)):
        pool, claims = await context(request, "telephony:read")
        async with pool.connection() as conn:
            rows = await (
                await conn.execute(
                    """SELECT * FROM telephony_connections WHERE app_id=%s
                AND owner_id=%s ORDER BY created_at,id LIMIT %s OFFSET %s""",
                    (claims["app_id"], claims["user"], limit + 1, offset),
                )
            ).fetchall()
        return {
            "connections": [public(r) for r in rows[:limit]],
            "next_offset": offset + limit if len(rows) > limit else None,
        }

    @router.get("/connections/{ident}/readiness")
    async def readiness(ident: UUID, request: Request):
        pool, claims = await context(request, "telephony:read")
        async with pool.connection() as conn:
            row = await owned(conn, ident, claims)
            blockers, _ = await checks(conn, row)
            limits = await (
                await conn.execute(
                    "SELECT concurrent_calls,daily_calls FROM telephony_app_limits WHERE app_id=%s",
                    (row["app_id"],),
                )
            ).fetchone()
        return {
            **public(row),
            "blockers": blockers,
            "ready_to_activate": not blockers,
            "accepting_calls": row["enabled"] and not blockers,
            "limits": limits,
            "live_carrier_call_tested": False,
            "outbound_calls": False,
            "speech_owner": "platform",
            "business_tools": "app-scoped grants; writes need caller confirmation; protected tools need app-verified identity",
            "note": "Readiness checks routing configuration; perform a real inbound audio test before customer rollout.",
        }

    @router.patch("/connections/{ident}")
    async def activate(ident: UUID, body: Activation, request: Request):
        pool, claims = await context(request, "telephony:write")
        async with pool.connection() as conn, conn.transaction():
            row = await owned(conn, ident, claims)
            blockers, _ = await checks(conn, row)
            if body.enabled and blockers:
                raise HTTPException(409, {"blockers": blockers})
            await conn.execute(
                "UPDATE telephony_connections SET enabled=%s WHERE id=%s", (body.enabled, ident)
            )
            if not body.enabled:
                await conn.execute(
                    "UPDATE telephony_sessions SET state='UNKNOWN',ended_at=now() WHERE connection_id=%s AND state='ACTIVE'",
                    (ident,),
                )
            await audit(conn, ident, "activated" if body.enabled else "paused", claims["principal"])
        return {"enabled": body.enabled}

    @router.post("/connections/{ident}/rotate-key")
    async def rotate(ident: UUID, request: Request, response: Response):
        pool, claims = await context(request, "telephony:write")
        key = "tgw_" + secrets.token_urlsafe(48)
        async with pool.connection() as conn, conn.transaction():
            await owned(conn, ident, claims)
            await conn.execute(
                "UPDATE telephony_connections SET gateway_hash=%s,enabled=false,heartbeat_at=NULL WHERE id=%s",
                (digest(key), ident),
            )
            await conn.execute(
                "UPDATE telephony_sessions SET state='UNKNOWN',ended_at=now() WHERE connection_id=%s AND state='ACTIVE'",
                (ident,),
            )
            await audit(conn, ident, "key_rotated", claims["principal"])
        response.headers["Cache-Control"] = "no-store"
        return {"gateway_key": key, "enabled": False}

    @router.get("/connections/{ident}/calls")
    async def calls(
        ident: UUID, request: Request, limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0)
    ):
        pool, claims = await context(request, "telephony:read")
        async with pool.connection() as conn:
            await owned(conn, ident, claims)
            rows = await (
                await conn.execute(
                    """SELECT call_id,state,turns,created_at,expires_at,ended_at
                FROM telephony_sessions WHERE connection_id=%s ORDER BY created_at DESC,id DESC LIMIT %s OFFSET %s""",
                    (ident, limit + 1, offset),
                )
            ).fetchall()
        for row in rows:
            if row["state"] == "ACTIVE" and row["expires_at"] <= datetime.now(UTC):
                row["state"] = "UNKNOWN"
        return {"calls": rows[:limit], "next_offset": offset + limit if len(rows) > limit else None}

    @router.get("/connections/{ident}/audit")
    async def events(ident: UUID, request: Request, after: int = Query(0, ge=0)):
        pool, claims = await context(request, "telephony:read")
        async with pool.connection() as conn:
            await owned(conn, ident, claims)
            rows = await (
                await conn.execute(
                    "SELECT id,event,actor,created_at FROM telephony_audit WHERE connection_id=%s AND id>%s ORDER BY id LIMIT 100",
                    (ident, after),
                )
            ).fetchall()
        return {"events": rows, "next_after": rows[-1]["id"] if rows else after}

    @router.post("/gateway/{ident}/heartbeat")
    async def heartbeat(ident: UUID, request: Request):
        async with request.app.state.repository.pool.connection() as conn:
            await gateway(conn, ident, request)
            await conn.execute("UPDATE telephony_connections SET heartbeat_at=now() WHERE id=%s", (ident,))
        return {"connected": True, "heartbeat_interval_seconds": 30}

    @router.post("/gateway/{ident}/calls", status_code=201)
    async def start(ident: UUID, body: Start, request: Request):
        async with request.app.state.repository.pool.connection() as conn, conn.transaction():
            row = await gateway(conn, ident, request)
            blockers, agent = await checks(conn, row)
            if not row["enabled"] or blockers:
                raise HTTPException(409, "Connection not active or ready")
            if body.number != row["number"]:
                raise HTTPException(403, "Number does not match this gateway")
            from .channel_trials import reserve
            if not await reserve(conn, row['app_id'], 'voice_calls', body.call_id):
                raise HTTPException(429, 'Pilot call limit or trial expiry reached')
            # Lock the common app limit row: two gateways cannot race the app quota.
            limits = await (
                await conn.execute(
                    "SELECT * FROM telephony_app_limits WHERE app_id=%s FOR UPDATE", (row["app_id"],)
                )
            ).fetchone()
            await conn.execute(
                "UPDATE telephony_sessions SET state='UNKNOWN',ended_at=expires_at WHERE app_id=%s AND state='ACTIVE' AND expires_at<=now()",
                (row["app_id"],),
            )
            existing = await (
                await conn.execute(
                    "SELECT * FROM telephony_sessions WHERE connection_id=%s AND call_id=%s",
                    (ident, body.call_id),
                )
            ).fetchone()
            if existing:
                raise HTTPException(409, "Call already reserved; inspect state, never start media twice")
            usage = await (
                await conn.execute(
                    """SELECT count(*) AS daily,
                count(*) FILTER(WHERE state='ACTIVE') AS active FROM telephony_sessions
                WHERE app_id=%s AND created_at>now()-interval '24 hours' """,
                    (row["app_id"],),
                )
            ).fetchone()
            if usage["daily"] >= limits["daily_calls"] or usage["active"] >= limits["concurrent_calls"]:
                raise HTTPException(429, "App call quota reached")
            await conn.execute(
                "INSERT INTO telephony_sessions(id,connection_id,app_id,call_id) VALUES(%s,%s,%s,%s)",
                (uuid4(), ident, row["app_id"], body.call_id),
            )
        return {
            "call_id": body.call_id,
            "state": "ACTIVE",
            "greeting": agent["greeting"],
            "max_seconds": 120,
            "max_turns": 15,
        }

    async def session(conn, ident, call_id):
        row = await (
            await conn.execute(
                "SELECT * FROM telephony_sessions WHERE connection_id=%s AND call_id=%s FOR UPDATE",
                (ident, call_id),
            )
        ).fetchone()
        if not row:
            raise HTTPException(404, "Call not found")
        return row

    @router.get("/gateway/{ident}/calls/{call_id}")
    async def status(ident: UUID, call_id: UUID, request: Request):
        async with request.app.state.repository.pool.connection() as conn:
            await gateway(conn, ident, request)
            row = await session(conn, ident, call_id)
        return {
            "call_id": call_id,
            "state": "UNKNOWN"
            if row["state"] == "ACTIVE" and row["expires_at"] <= datetime.now(UTC)
            else row["state"],
            "turns": row["turns"],
            "expires_at": row["expires_at"],
        }

    @router.post("/gateway/{ident}/calls/{call_id}/end")
    async def end(ident: UUID, call_id: UUID, body: End, request: Request):
        async with request.app.state.repository.pool.connection() as conn, conn.transaction():
            await gateway(conn, ident, request)
            row = await session(conn, ident, call_id)
            if row["state"] == "ACTIVE":
                await conn.execute(
                    "UPDATE telephony_sessions SET state=%s,ended_at=now() WHERE id=%s",
                    (body.state, row["id"]),
                )
        return {"state": body.state if row["state"] == "ACTIVE" else row["state"]}

    @router.post("/gateway/{ident}/calls/{call_id}/turns/{turn}")
    async def reply(ident: UUID, call_id: UUID, turn: int, body: Turn, request: Request):
        if not 1 <= turn <= 15:
            raise HTTPException(422, "Turn must be between 1 and 15")
        pool = request.app.state.repository.pool
        async with pool.connection() as conn, conn.transaction():
            row = await gateway(conn, ident, request)
            blockers, agent = await checks(conn, row)
            current = await session(conn, ident, call_id)
            if (
                not row["enabled"]
                or blockers
                or current["state"] != "ACTIVE"
                or current["expires_at"] <= datetime.now(UTC)
            ):
                raise HTTPException(409, "Call is no longer active")
            old = await (
                await conn.execute(
                    "SELECT * FROM telephony_turns WHERE session_id=%s AND turn=%s", (current["id"], turn)
                )
            ).fetchone()
            if old:
                if old["input_hash"] != digest(body.text):
                    raise HTTPException(409, "Turn content changed")
                if old["state"] == "DONE":
                    return old["reply"]
                raise HTTPException(409, "Turn outcome pending or unknown; do not replay")
            if turn != current["turns"] + 1:
                raise HTTPException(409, "Turns must run in order")
            pending = await (
                await conn.execute(
                    "SELECT 1 FROM telephony_turns WHERE session_id=%s AND state!='DONE'", (current["id"],)
                )
            ).fetchone()
            if pending:
                raise HTTPException(409, "Previous turn outcome unknown; end this call")
            history = await (
                await conn.execute(
                    "SELECT input_text,reply FROM telephony_turns WHERE session_id=%s ORDER BY turn DESC LIMIT 4",
                    (current["id"],),
                )
            ).fetchall()
            await conn.execute(
                "INSERT INTO telephony_turns(session_id,turn,input_hash,state,input_text) VALUES(%s,%s,%s,'STARTED',%s)",
                (current["id"], turn, digest(body.text), body.text),
            )
            await conn.execute("UPDATE telephony_sessions SET turns=%s WHERE id=%s", (turn, current["id"]))
        messages = []
        for item in reversed(history):
            messages.extend(
                [
                    {"role": "user", "text": item["input_text"]},
                    {"role": "assistant", "text": item["reply"]["text"]},
                ]
            )
        try:
            async with asyncio.timeout(
                min(20, max(0.01, (current["expires_at"] - datetime.now(UTC)).total_seconds()))
            ):
                result = await telephony_tools.converse(
                    pool, voice_gateway(), row, current, agent, turn, body.text, messages
                )
            # Tool permissions/consent are enforced by the dispatcher before any external mutation.
            if (
                result.get("action") not in {"continue", "end", "handoff"}
                or not isinstance(result.get("text"), str)
                or not 1 <= len(result["text"]) <= 1200
            ):
                raise ValueError("Invalid voice response")
            result = {
                "action": result["action"],
                "text": result["text"],
                "actions_executed": result.get("actions_executed", False) is True,
            }
        except (Exception, asyncio.CancelledError):  # noqa: BLE001 -- persist uncertain outcome without leaking provider errors
            async with pool.connection() as conn:
                await conn.execute(
                    "UPDATE telephony_turns SET state='UNKNOWN' WHERE session_id=%s AND turn=%s",
                    (current["id"], turn),
                )
            raise HTTPException(503, "Voice response unavailable; outcome saved, do not retry") from None
        async with pool.connection() as conn, conn.transaction():
            # Re-check revocation and pause after model work, before releasing an answer.
            latest = await gateway(conn, ident, request)
            fresh = await session(conn, ident, call_id)
            blockers, _ = await checks(conn, latest)
            if (
                not latest["enabled"]
                or blockers
                or fresh["state"] != "ACTIVE"
                or fresh["expires_at"] <= datetime.now(UTC)
            ):
                raise HTTPException(409, "Call stopped during response")
            await conn.execute(
                "UPDATE telephony_turns SET state='DONE',reply=%s WHERE session_id=%s AND turn=%s",
                (Jsonb(result), current["id"], turn),
            )
        return result

    telephony_tools.install_tools(router, context, owned)
    from .telephony_speech import install_speech

    install_speech(router, gateway, session, checks, reply, Turn)
    app.include_router(router)
