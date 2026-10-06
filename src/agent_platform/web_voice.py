"""App-owned internet voice calls. No telephone or WhatsApp provider is involved."""

import asyncio
import hashlib
import hmac
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import httpx
from fastapi import APIRouter, HTTPException, Request
from psycopg.types.json import Jsonb
from pydantic import Field

from . import calling_audio, calling_media, voice_conversation
from .gateway import FallbackProvider, Gateway
from .service_identity import authenticate
from .tasks import Strict, TaskError


class Agent(Strict):
    external_id: str = Field(min_length=1, max_length=100, pattern=r"^[a-zA-Z0-9_.:-]+$")
    name: str = Field(min_length=1, max_length=120)
    greeting: str = Field(min_length=1, max_length=500)
    fallback: str = Field(
        default="I could not confirm that. Please contact the business directly.",
        min_length=1,
        max_length=500,
    )
    visibility: str = Field(default="private", pattern=r"^(private|app)$")
    documents: list[UUID] = Field(default_factory=list, max_length=20)
    language: str = Field(default="auto", pattern=r"^(auto|en|hi|hinglish)$")


class Offer(Strict):
    sdp: str = Field(min_length=20, max_length=65536)
    request_key: str = Field(min_length=8, max_length=100, pattern=r"^[a-zA-Z0-9_.:-]+$")
    audio_consent: bool


def ready():
    if os.environ.get("AI_WEB_VOICE_ENABLED") != "1":
        return False
    try:
        import aiortc  # noqa: F401

        calling_audio.key()
        calling_audio.speech_key()
        return True
    except (ImportError, ValueError):
        return False


def voice_gateway():
    gateway = Gateway(FallbackProvider("gemini", calling_audio.key()), max_attempts=1, allow_fallback=False)
    gateway.fast_model = gateway.reasoning_model = os.environ.get(
        "AI_WEB_VOICE_MODEL", "gemini-3.1-flash-lite"
    )
    return gateway


def action_handler(agent):
    cfg = json.loads(os.environ.get("AI_WEB_VOICE_HANDLERS", "{}")).get(str(agent["id"]))
    if cfg is None:
        return None
    url = urlsplit(cfg.get("url", ""))
    if (
        cfg.get("app_id") != str(agent["app_id"])
        or url.scheme != "https"
        or not url.hostname
        or url.username
        or url.password
        or url.fragment
        or len(cfg.get("secret", "")) < 32
    ):
        raise ValueError("Invalid app action handler")
    return cfg


async def invoke(cfg, payload):
    raw = json.dumps(payload, separators=(",", ":")).encode()
    stamp = str(int(time.time()))
    signature = hmac.new(cfg["secret"].encode(), stamp.encode() + b"." + raw, hashlib.sha256).hexdigest()
    async with httpx.AsyncClient(timeout=httpx.Timeout(8, connect=3), follow_redirects=False) as client:
        response = await client.post(
            cfg["url"],
            content=raw,
            headers={
                "Content-Type": "application/json",
                "X-Agent-Timestamp": stamp,
                "X-Agent-Signature": signature,
            },
        )
        response.raise_for_status()
        if len(response.content) > 16384:
            raise ValueError("App response too large")
        return response.json()


async def dialogue(pool, agent, call, text, history=None):
    fallback = {"action": "handoff", "text": agent["fallback"]}
    if not text.strip() or len(text) > 1500:
        return fallback
    event = uuid4()
    async with pool.connection() as conn, conn.transaction():
        current = await (
            await conn.execute("SELECT * FROM web_voice_sessions WHERE id=%s FOR UPDATE", (call["id"],))
        ).fetchone()
        enabled = await (
            await conn.execute("SELECT enabled,language FROM web_voice_agents WHERE id=%s", (agent["id"],))
        ).fetchone()
        if (
            not current
            or not enabled
            or not enabled["enabled"]
            or current["state"] != "ACTIVE"
            or current["turns"] >= 15
            or current["expires_at"].timestamp() <= time.time()
        ):
            return fallback
        if await (
            await conn.execute(
                "SELECT 1 FROM web_voice_turns WHERE session_id=%s AND state IN ('STARTED','UNKNOWN')",
                (call["id"],),
            )
        ).fetchone():
            return fallback
        agent = {**agent, "language": enabled["language"]}
        turn = current["turns"] + 1
        await conn.execute(
            "INSERT INTO web_voice_turns(id,session_id,turn,input_hash,state) VALUES(%s,%s,%s,%s,'STARTED')",
            (event, call["id"], turn, hashlib.sha256(text.encode()).hexdigest()),
        )
        await conn.execute("UPDATE web_voice_sessions SET turns=%s WHERE id=%s", (turn, call["id"]))
    try:
        cfg = action_handler(agent)
        if call.get("handoff_id"):
            from .voice_handoffs import clinic_turn

            reply = await clinic_turn(pool, call["handoff_id"], event, text)
        elif cfg:
            result = await invoke(
                cfg,
                {
                    "event_id": str(event),
                    "session_id": str(call["id"]),
                    "agent_id": str(agent["id"]),
                    "app_id": str(agent["app_id"]),
                    "user_id": str(call["owner_id"]),
                    "input": text,
                    "input_trust": "untrusted_speech",
                    "turn": turn,
                    "channel": "web_voice",
                    "language": agent.get("language", "auto"),
                    "conversation": history or [],
                },
            )
            if result.get("event_id") != str(event):
                raise ValueError("Mismatched action response")
            reply = {"action": result["action"], "text": result["text"]}
        else:
            reply = await asyncio.wait_for(
                voice_conversation.respond(
                    pool,
                    voice_gateway(),
                    agent,
                    call["owner_id"],
                    text,
                    "web-voice:" + str(event),
                    history or [],
                ),
                timeout=20,
            )

        if (
            reply["action"] not in {"continue", "end", "handoff"}
            or not isinstance(reply["text"], str)
            or not 1 <= len(reply["text"]) <= 1200
        ):
            raise ValueError("Invalid action reply")
    except (Exception, asyncio.CancelledError) as exc:  # never retry an uncertain business action
        async with pool.connection() as conn:
            await conn.execute("UPDATE web_voice_turns SET state='UNKNOWN' WHERE id=%s", (event,))
        if isinstance(exc, asyncio.CancelledError):
            raise
        return fallback
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE web_voice_turns SET state='DONE',reply=%s WHERE id=%s", (Jsonb(reply), event)
        )
    return reply


def install_web_voice(app):
    router = APIRouter(prefix="/v1/voice")
    runtime_id, sessions = uuid4(), {}
    prior = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(instance):
        async with prior(instance):
            try:
                yield
            finally:
                await asyncio.gather(*(s.close() for s in tuple(sessions.values())), return_exceptions=True)

    app.router.lifespan_context = lifespan

    async def context(request, scope):
        pool = request.app.state.repository.pool
        claims = await authenticate(
            pool, request.headers.get("Authorization"), request.headers.get("X-External-User-ID"), scope
        )
        return pool, claims

    async def owned(conn, ident, claims, shared=False):
        row = await (
            await conn.execute(
                "SELECT * FROM web_voice_agents WHERE id=%s AND app_id=%s AND (owner_id=%s OR (%s AND visibility='app'))",
                (ident, claims["app_id"], claims["user"], shared),
            )
        ).fetchone()
        if not row:
            raise HTTPException(404, "Voice agent not found")
        return row

    @router.get("/capabilities")
    async def capabilities(request: Request):
        await context(request, "agents:read")
        return {
            "channel": "web_voice",
            "requires_phone_number": False,
            "requires_whatsapp": False,
            "configured": ready(),
            "live_audio_verified": False,
            "session_seconds": 120,
            "max_concurrent_sessions": 2,
            "recording": False,
            "languages": list(voice_conversation.LANGUAGES),
            "text_rehearsal": True,
            "telephone_connected": False,
            "telephone_note": "Mobile-number calling requires a separately configured telephone connector.",
        }

    @router.get("/agents/{ident}/readiness")
    async def readiness(ident: UUID, request: Request):
        pool, claims = await context(request, "agents:run")
        async with pool.connection() as conn:
            agent = await owned(conn, ident, claims, shared=True)
            count = await (
                await conn.execute(
                    "SELECT count(*) AS n FROM knowledge_documents WHERE app_id=%s AND visibility='app' AND id=ANY(%s::uuid[])",
                    (claims["app_id"], agent["documents"]),
                )
            ).fetchone()
        blockers = []
        if not agent["enabled"]:
            blockers.append("This agent is paused.")
        audio_ready = ready()
        if not audio_ready:
            blockers.append("The operator needs to configure the internet audio runtime and speech account.")
        handler = False
        try:
            handler = bool(action_handler(agent))
        except (ValueError, TypeError, AttributeError):
            blockers.append("The operator needs to correct the business-action connection.")
        if not count["n"] and not handler:
            blockers.append("Add published business FAQs or connect the app's business actions.")
        return {
            "channel": "web_voice",
            "ready_to_attempt": not blockers,
            "agent_enabled": agent["enabled"],
            "language": agent["language"],
            "knowledge_documents_available": count["n"],
            "business_actions_configured": handler,
            "business_actions_verified": False,
            "speech_provider": "gemini",
            "model_access_verified": False,
            "telephone_connected": False,
            "requires_phone_number": False,
            "blockers": blockers,
            "message": "Ready for an internet call test. Speech quality and model access are checked during the test."
            if not blockers
            else " ".join(blockers),
        }

    @router.post("/agents/{ident}/preview")
    async def preview(ident: UUID, body: voice_conversation.Preview, request: Request):
        pool, claims = await context(request, "agents:run")
        async with pool.connection() as conn:
            agent = await owned(conn, ident, claims)
        if not agent["enabled"]:
            raise HTTPException(409, "Voice agent is paused")
        try:
            reply = await asyncio.wait_for(
                voice_conversation.respond(
                    pool,
                    voice_gateway(),
                    agent,
                    claims["user"],
                    body.text,
                    "voice-preview:" + str(ident) + ":" + body.request_key,
                    [m.model_dump() for m in body.history],
                ),
                timeout=20,
            )
        except Exception as exc:  # noqa: BLE001 -- keep provider errors and credentials private
            code = getattr(exc, "code", "")
            upstream = getattr(getattr(exc, "__cause__", None), "response", None)
            status = getattr(upstream, "status_code", None)
            logging.getLogger(__name__).warning(
                "voice_preview_failed type=%s code=%s upstream_status=%s", type(exc).__name__, code, status
            )
            if isinstance(exc, TaskError):
                raise HTTPException(
                    409,
                    "Rehearsal request rejected by usage or saved-request policy. No action was performed.",
                ) from None
            if status == 429:
                detail = "The AI provider's quota or rate limit was reached. Check its quota before starting another rehearsal."
            elif code == "MODEL_NOT_CONFIGURED":
                detail = "The voice answer model is not configured or is disabled."
            elif isinstance(exc, TimeoutError) or code == "MODEL_TIMEOUT":
                detail = "The AI answer timed out. This request was not automatically retried."
            else:
                detail = (
                    "The AI provider could not complete this rehearsal. Check model access and availability."
                )
            raise HTTPException(503, detail + " No booking, message or telephone call was made.") from None

        return {**reply, "mode": "knowledge_preview", "actions_executed": False}

    @router.post("/agents/{ident}/settings")
    async def settings(ident: UUID, body: voice_conversation.Settings, request: Request):
        pool, claims = await context(request, "agents:write")
        async with pool.connection() as conn, conn.transaction():
            await owned(conn, ident, claims)
            row = await (
                await conn.execute(
                    "UPDATE web_voice_agents SET enabled=%s,language=%s WHERE id=%s RETURNING *",
                    (body.enabled, body.language, ident),
                )
            ).fetchone()
        if not body.enabled:
            for session_id, media in tuple(sessions.items()):
                async with pool.connection() as conn:
                    same = await (
                        await conn.execute(
                            "SELECT 1 FROM web_voice_sessions WHERE id=%s AND agent_id=%s",
                            (session_id, ident),
                        )
                    ).fetchone()
                if same:
                    await media.close()
        return row

    @router.post("/agents", status_code=201)
    async def create(body: Agent, request: Request):
        pool, claims = await context(request, "agents:write")
        async with pool.connection() as conn, conn.transaction():
            for doc in body.documents:
                exists = await (
                    await conn.execute(
                        "SELECT 1 FROM knowledge_documents WHERE id=%s AND app_id=%s AND visibility='app'",
                        (doc, claims["app_id"]),
                    )
                ).fetchone()
                if not exists:
                    raise HTTPException(422, "Select published knowledge belonging to this app")
            row = await (
                await conn.execute(
                    """INSERT INTO web_voice_agents(id,app_id,owner_id,external_id,name,greeting,fallback,documents,visibility,language)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(app_id,owner_id,external_id) DO NOTHING RETURNING *""",
                    (
                        uuid4(),
                        claims["app_id"],
                        claims["user"],
                        body.external_id,
                        body.name,
                        body.greeting,
                        body.fallback,
                        body.documents,
                        body.visibility,
                        body.language,
                    ),
                )
            ).fetchone()
            if row is None:
                row = await (
                    await conn.execute(
                        "SELECT * FROM web_voice_agents WHERE app_id=%s AND owner_id=%s AND external_id=%s",
                        (claims["app_id"], claims["user"], body.external_id),
                    )
                ).fetchone()
                if any(row[k] != v for k, v in body.model_dump().items()):
                    raise HTTPException(409, "Agent ID already has different settings")
        return row

    @router.get("/agents")
    async def listing(request: Request, before: UUID | None = None):
        pool, claims = await context(request, "agents:read")
        async with pool.connection() as conn:
            rows = await (
                await conn.execute(
                    "SELECT * FROM web_voice_agents WHERE app_id=%s AND owner_id=%s AND (%s::uuid IS NULL OR id<%s) ORDER BY id DESC LIMIT 51",
                    (claims["app_id"], claims["user"], before, before),
                )
            ).fetchall()
        return {"agents": rows[:50], "next_before": str(rows[49]["id"]) if len(rows) > 50 else None}

    @router.post("/agents/{ident}/sessions", status_code=201)
    async def start(ident: UUID, body: Offer, request: Request):
        pool, claims = await context(request, "agents:run")
        return await start_for(pool, claims, ident, body)

    async def start_for(pool, claims, ident, body, handoff=None):
        if not body.audio_consent:
            raise HTTPException(422, "Microphone/audio processing consent is required")
        if not ready():
            raise HTTPException(503, "Internet voice is not configured on this server")
        async with pool.connection() as conn, conn.transaction():
            if handoff:
                locked = await (
                    await conn.execute(
                        "SELECT * FROM web_voice_handoffs WHERE id=%s AND expires_at>now() FOR UPDATE",
                        (handoff["id"],),
                    )
                ).fetchone()
                if not locked or locked["consumed_at"]:
                    raise HTTPException(409, "This voice invitation has already been used or expired")
                agent = await (
                    await conn.execute(
                        "SELECT * FROM web_voice_agents WHERE id=%s AND app_id=%s", (ident, claims["app_id"])
                    )
                ).fetchone()
            else:
                agent = await owned(conn, ident, claims, shared=True)
            if not agent or not agent["enabled"]:
                raise HTTPException(409, "Voice agent is paused")
            if not handoff and not agent["documents"] and not action_handler(agent):
                raise HTTPException(409, "Add business knowledge or configure the app action handler first")
            # Global reservation lock bounds media even with multiple API processes.
            await conn.execute("SELECT pg_advisory_xact_lock(79342020)")
            await conn.execute(
                "UPDATE web_voice_sessions SET state='UNKNOWN',ended_at=now() WHERE state IN ('STARTING','ACTIVE') AND expires_at<now()"
            )
            if await (
                await conn.execute(
                    "SELECT 1 FROM web_voice_sessions WHERE app_id=%s AND owner_id=%s AND request_key=%s",
                    (claims["app_id"], claims["user"], body.request_key),
                )
            ).fetchone():
                raise HTTPException(
                    409,
                    "This connection attempt already exists. Check its status; do not retry automatically.",
                )
            active = await (
                await conn.execute(
                    "SELECT count(*) AS n FROM web_voice_sessions WHERE state IN ('STARTING','ACTIVE')"
                )
            ).fetchone()
            if active["n"] >= 2:
                raise HTTPException(429, "Voice capacity is busy. Try again after an active call ends.")
            if await (
                await conn.execute(
                    "SELECT 1 FROM web_voice_sessions WHERE app_id=%s AND owner_id=%s AND state IN ('STARTING','ACTIVE')",
                    (claims["app_id"], claims["user"]),
                )
            ).fetchone():
                raise HTTPException(409, "You already have an active voice session")
            used = await (
                await conn.execute(
                    "SELECT count(*) AS n FROM web_voice_sessions WHERE app_id=%s AND created_at>now()-interval '24 hours'",
                    (claims["app_id"],),
                )
            ).fetchone()
            if used["n"] >= 30:
                raise HTTPException(429, "Pilot limit of 30 voice sessions per app per day reached")
            call = await (
                await conn.execute(
                    "INSERT INTO web_voice_sessions(id,agent_id,app_id,owner_id,request_key,offer_hash,runtime_id,state) VALUES(%s,%s,%s,%s,%s,%s,%s,'STARTING') RETURNING *",
                    (
                        uuid4(),
                        ident,
                        claims["app_id"],
                        claims["user"],
                        body.request_key,
                        hashlib.sha256(body.sdp.encode()).hexdigest(),
                        runtime_id,
                    ),
                )
            ).fetchone()

            if handoff:
                await conn.execute(
                    "UPDATE web_voice_handoffs SET consumed_at=now(),session_id=%s WHERE id=%s",
                    (call["id"], handoff["id"]),
                )
                await conn.execute(
                    "UPDATE web_voice_sessions SET handoff_id=%s WHERE id=%s", (handoff["id"], call["id"])
                )
                call["handoff_id"] = handoff["id"]

        history = []  # Per-call bounded memory; never shared or persisted as raw transcript.

        async def respond(text):
            reply = await dialogue(pool, agent, call, text, history)
            if reply["action"] == "continue":
                history.extend(
                    [{"role": "user", "text": text[:1500]}, {"role": "assistant", "text": reply["text"]}]
                )
                del history[:-8]
            return reply

        async def ended():
            sessions.pop(str(call["id"]), None)
            history.clear()
            async with pool.connection() as conn:
                await conn.execute(
                    "UPDATE web_voice_sessions SET state='ENDED',ended_at=now() WHERE id=%s AND state IN ('STARTING','ACTIVE')",
                    (call["id"],),
                )

        media = None
        try:
            greeting = agent["greeting"]
            if handoff:
                from .voice_handoffs import clinic_turn

                resume = await clinic_turn(
                    pool, handoff["id"], uuid4(), "Resume appointment conversation", resume=True
                )
                if resume["action"] != "continue":
                    raise ValueError("Conversation is handled by reception")
                greeting = resume["text"]
            media = calling_media.Session(respond, greeting, ended)
            sessions[str(call["id"])] = media
            answer = await asyncio.wait_for(media.remote(body.sdp, "offer"), timeout=20)
            async with pool.connection() as conn:
                await conn.execute(
                    "UPDATE web_voice_sessions SET state='ACTIVE' WHERE id=%s AND state='STARTING'",
                    (call["id"],),
                )
            return {
                "id": call["id"],
                "sdp": answer,
                "type": "answer",
                "expires_at": call["expires_at"],
                "state": "CONNECTING",
            }
        except Exception:  # noqa: BLE001 -- SDP and provider details must not appear in errors
            if media:
                await media.close()
            async with pool.connection() as conn:
                await conn.execute(
                    "UPDATE web_voice_sessions SET state='FAILED',ended_at=now() WHERE id=%s", (call["id"],)
                )
            raise HTTPException(502, "Audio connection could not be established") from None

    async def session_row(pool, ident, claims):
        async with pool.connection() as conn:
            row = await (
                await conn.execute(
                    "SELECT * FROM web_voice_sessions WHERE id=%s AND app_id=%s AND owner_id=%s",
                    (ident, claims["app_id"], claims["user"]),
                )
            ).fetchone()
        if not row:
            raise HTTPException(404, "Voice session not found")
        return row

    @router.get("/sessions/{ident}")
    async def status(ident: UUID, request: Request):
        pool, claims = await context(request, "agents:read")
        return await status_for(pool, claims, ident)

    async def status_for(pool, claims, ident):
        row = await session_row(pool, ident, claims)
        media = sessions.get(str(ident))
        return {
            "id": ident,
            "state": row["state"],
            "turns": row["turns"],
            "expires_at": row["expires_at"],
            "audio_connected": bool(media and media.pc.connectionState == "connected"),
        }

    @router.post("/sessions/{ident}/end")
    async def end(ident: UUID, request: Request):
        pool, claims = await context(request, "agents:run")
        return await end_for(pool, claims, ident)

    async def end_for(pool, claims, ident):
        row = await session_row(pool, ident, claims)
        media = sessions.get(str(ident))
        if media:
            await media.close()
        elif row["state"] in {"STARTING", "ACTIVE"}:
            raise HTTPException(
                409,
                "Session belongs to another runtime. Its audio expires automatically; use the same runtime for control.",
            )
        return {"ended": True}

    app.include_router(router)
    from .voice_handoffs import install

    install(app, start_for, status_for, end_for)
