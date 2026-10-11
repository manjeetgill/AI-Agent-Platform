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

from . import (
    calling_audio,
    calling_media,  # noqa: F401 -- compatibility hook for existing media adapter tests
    media_client,
    voice_config,
    voice_conversation,
    voice_events,
)
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
    config: voice_config.VoiceConfig | None = None


class AgentUpdate(Agent):
    expected_version: int = Field(ge=1)


class Offer(Strict):
    sdp: str = Field(min_length=20, max_length=65536)
    request_key: str = Field(min_length=8, max_length=100, pattern=r"^[a-zA-Z0-9_.:-]+$")
    audio_consent: bool
    transcript_consent: bool = False
    caller_phone: str | None = Field(default=None, pattern=r"^\+[1-9]\d{7,14}$")


def ready():
    if os.environ.get("AI_WEB_VOICE_ENABLED") != "1":
        return False
    if media_client.enabled():
        return media_client.configured()
    try:
        import aiortc  # noqa: F401
        import webrtcvad  # noqa: F401

        voice_config.gateway()
        calling_audio.speech_key()
        calling_audio.speech_key("tts")
        return True
    except (ImportError, ValueError):
        return False


def voice_gateway():
    return voice_config.gateway()


def configured_gateway(agent):
    with voice_config.using(agent.get('config')):
        return voice_gateway()


def snapshot(agent):
    result = {k: v for k, v in agent.items() if k in {'id', 'app_id', 'owner_id', 'name', 'greeting', 'fallback', 'documents', 'visibility', 'language', 'version', 'config'}}
    result['config'] = (voice_config.VoiceConfig.model_validate(agent['config']) if agent.get('config') else voice_config.defaults()).model_dump()
    return json.loads(json.dumps(result, default=str))


async def save_version(conn, agent):
    await conn.execute('INSERT INTO web_voice_versions(agent_id,version,snapshot) VALUES(%s,%s,%s) ON CONFLICT DO NOTHING',
                       (agent['id'], agent['version'], Jsonb(snapshot(agent))))


class Configuration(Strict):
    expected_version: int = Field(ge=1)
    config: voice_config.VoiceConfig


class RestoreVersion(Strict):
    expected_version: int = Field(ge=1)
    source_version: int = Field(ge=1)


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


async def dialogue(pool, agent, call, text, history=None, on_claim=None):
    agent = {**agent, "retain_content": False}
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
        # Current enabled status is a kill switch; language and configuration stay pinned to the session.
        turn = current["turns"] + 1
        await conn.execute(
            "INSERT INTO web_voice_turns(id,session_id,turn,input_hash,state) VALUES(%s,%s,%s,%s,'STARTED')",
            (event, call["id"], turn, hashlib.sha256(text.encode()).hexdigest()),
        )
        await conn.execute("UPDATE web_voice_sessions SET turns=%s WHERE id=%s", (turn, call["id"]))
    external_dispatch = False
    try:
        cfg = action_handler(agent)
        if call.get("handoff_id"):
            from .voice_handoffs import clinic_turn

            external_dispatch = True
            reply = await clinic_turn(pool, call["handoff_id"], event, text)
        elif cfg:
            external_dispatch = True
            result = await invoke(
                cfg,
                {
                    **({"caller_phone":agent["caller_phone"]} if agent.get("caller_phone") else {}),
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
            from .voice_specialists import select
            agent, routed = await select(pool, agent, call, text)
            if routed:
                async with pool.connection() as conn:
                    await conn.execute("UPDATE web_voice_turns SET state='DONE',reply=%s WHERE id=%s", (Jsonb({'action':'continue'}), event))
                return routed
            selected_gateway = configured_gateway(agent)
            if on_claim and agent.get("config", {}).get("stream_grounded_quotes"):
                selected_gateway.on_claim = on_claim
            reply = await asyncio.wait_for(
                voice_conversation.respond(
                    pool,
                    selected_gateway,
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
            # Cancelling a read-only answer cannot leave a business action uncertain.
            # It must release the turn fence so a caller can ask their next question.
            state = 'CANCELLED' if isinstance(exc, asyncio.CancelledError) and not external_dispatch else 'UNKNOWN'
            await conn.execute("UPDATE web_voice_turns SET state=%s WHERE id=%s", (state, event))
        if isinstance(exc, asyncio.CancelledError):
            raise
        return fallback
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE web_voice_turns SET state='DONE',reply=%s WHERE id=%s", (Jsonb({"action": reply["action"]}), event)
        )
    return reply


def install_web_voice(app):
    router = APIRouter(prefix="/v1/voice")
    from .voice_runtime import VoiceRuntime
    runtime = VoiceRuntime()
    runtime_id, sessions = runtime.id, runtime.sessions
    prior = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(instance):
        async with prior(instance):
            try:
                yield
            finally:
                await runtime.close()

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
                "SELECT * FROM web_voice_agents WHERE id=%s AND app_id=%s AND deleted_at IS NULL AND (owner_id=%s OR (%s AND visibility='app'))",
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
            "speech_provider": calling_audio.speech_provider(),
            "reasoning_provider": voice_config.defaults().reasoning,
            "default_config": voice_config.defaults().model_dump(),
            "configurable_providers": {"asr": ["gemini", "sarvam", "local"], "tts": ["gemini", "sarvam", "local"], "reasoning": ["gemini", "nvidia", "openrouter", "together", "local"]},
            "barge_in": True,
            "asr_partial_streaming": False,
            "local_rolling_partial_transcripts": True,
            "source_quotation_streaming": True,
            "arbitrary_token_speech_streaming": False,
            "usage_accounting": "per-stage estimates with pre-dispatch spend reservations",
            "tts_sentence_streaming": True,
            "transcript_retention_hours": 24,
            "live_audio_verified": False,
            "session_seconds": 120,
            "max_concurrent_sessions": 2,
            "recording": False,
            "languages": list(voice_conversation.LANGUAGES),
            "text_rehearsal": True,
            "telephone_connected": False,
            "telephone_note": "Mobile-number calling requires a separately configured telephone connector.",
        }

    @router.get('/usage')
    async def usage(request: Request):
        pool, claims = await context(request, 'agents:read')
        async with pool.connection() as conn:
            row = await (await conn.execute("""SELECT
                count(*) FILTER(WHERE created_at>now()-interval '24 hours') AS daily_sessions,
                count(*) FILTER(WHERE created_at>=date_trunc('month',now())) AS monthly_sessions,
                count(*) FILTER(WHERE state IN ('STARTING','ACTIVE') AND expires_at>now()) AS active_sessions,
                coalesce(sum(greatest(0,least(120,extract(epoch FROM coalesce(ended_at,least(now(),expires_at))-created_at)))),0) AS duration_seconds
                FROM web_voice_sessions WHERE app_id=%s""", (claims['app_id'],))).fetchone()
            from .voice_usage import summary
            accounting = await summary(conn, claims['app_id'])
        return {**row, 'accounting': accounting, 'daily_session_limit': 30,
                'monthly_session_limit': max(1,min(10000,int(os.environ.get('AI_VOICE_MONTHLY_SESSION_LIMIT','300')))),
                'session_seconds_limit': 120, 'installation_concurrency_limit': 2,
                'estimated_cost': None, 'cost_status': 'unknown',
                'quota_basis': 'Session attempts reserve allowance, including failures; money is not estimated from duration.'}

    @router.get('/health')
    async def voice_health(request: Request):
        pool, claims = await context(request, 'agents:read')
        async with pool.connection() as conn:
            counts = await (await conn.execute("""SELECT count(*) AS sessions,
                count(*) FILTER(WHERE state='FAILED') AS failed,
                count(*) FILTER(WHERE state='UNKNOWN') AS unknown,
                count(*) FILTER(WHERE state IN ('STARTING','ACTIVE') AND expires_at>now()) AS active,
                count(*) FILTER(WHERE state='ENDED') AS ended
                FROM web_voice_sessions WHERE app_id=%s AND created_at>now()-interval '24 hours'""", (claims['app_id'],))).fetchone()
            uncertain = await (await conn.execute("""SELECT count(*) AS uncertain_turns FROM web_voice_turns t
                JOIN web_voice_sessions s ON s.id=t.session_id
                WHERE s.app_id=%s AND t.state='UNKNOWN' AND t.created_at>now()-interval '24 hours'""", (claims['app_id'],))).fetchone()
        return {'window':'last 24 hours','scope':'sessions visible to this caller',**counts,**uncertain,
                'attention_required':bool(counts['failed'] or counts['unknown'] or uncertain['uncertain_turns']),
                'telephone_verified':False}

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
        with voice_config.using(agent.get("config")):
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
            "speech_provider": snapshot(agent)["config"]["asr"],
            "config": snapshot(agent)["config"],
            "version": agent["version"],
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
            selected_gateway = configured_gateway(agent)
            reply = await asyncio.wait_for(
                voice_conversation.respond(
                    pool,
                    selected_gateway,
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
                    "UPDATE web_voice_agents SET enabled=%s,language=%s,version=version+1 WHERE id=%s RETURNING *",
                    (body.enabled, body.language, ident),
                )
            ).fetchone()
            await save_version(conn, row)
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
                    """INSERT INTO web_voice_agents(id,app_id,owner_id,external_id,name,greeting,fallback,documents,visibility,language,config)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(app_id,owner_id,external_id) DO NOTHING RETURNING *""",
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
                        Jsonb((body.config or voice_config.defaults()).model_dump()),
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
                if row["deleted_at"]:
                    raise HTTPException(409, "External ID belongs to a deleted agent")
                if any(row[k] != v for k, v in body.model_dump(exclude={"config"}).items()) or (body.config is not None and voice_config.VoiceConfig.model_validate(row["config"]).model_dump() != body.config.model_dump()):
                    raise HTTPException(409, "Agent ID already has different settings")
            await save_version(conn, row)
        return row

    @router.put('/agents/{ident}')
    async def edit_agent(ident: UUID, body: AgentUpdate, request: Request):
        pool, claims = await context(request, 'agents:write')
        async with pool.connection() as conn, conn.transaction():
            current = await owned(conn, ident, claims)
            if body.external_id != current['external_id']:
                raise HTTPException(422, 'External ID cannot be changed')
            for doc in body.documents:
                if not await (await conn.execute("SELECT 1 FROM knowledge_documents WHERE id=%s AND app_id=%s AND visibility='app'", (doc, claims['app_id']))).fetchone():
                    raise HTTPException(422, 'Select published knowledge belonging to this app')
            row = await (await conn.execute('UPDATE web_voice_agents SET name=%s,greeting=%s,fallback=%s,documents=%s,visibility=%s,language=%s,config=%s,version=version+1 WHERE id=%s AND version=%s RETURNING *',
                (body.name, body.greeting, body.fallback, body.documents, body.visibility, body.language,
                 Jsonb(body.config.model_dump() if body.config else current['config']), ident, body.expected_version))).fetchone()
            if not row:
                raise HTTPException(409, 'Agent changed; reload before editing')
            await save_version(conn, row)
        return row

    @router.delete('/agents/{ident}')
    async def delete_agent(ident: UUID, request: Request):
        pool, claims = await context(request, 'agents:write')
        async with pool.connection() as conn, conn.transaction():
            await owned(conn, ident, claims)
            await conn.execute('UPDATE web_voice_agents SET enabled=false,deleted_at=now() WHERE id=%s', (ident,))
            calls = await (await conn.execute("SELECT id FROM web_voice_sessions WHERE agent_id=%s AND state IN ('STARTING','ACTIVE')", (ident,))).fetchall()
        for call in calls:
            if str(call['id']) in sessions:
                await sessions[str(call['id'])].close()
        return {'deleted': True, 'historical_sessions_preserved': True}

    @router.post('/agents/{ident}/configuration')
    async def configuration(ident: UUID, body: Configuration, request: Request):
        pool, claims = await context(request, 'agents:write')
        async with pool.connection() as conn, conn.transaction():
            await owned(conn, ident, claims)
            row = await (await conn.execute('UPDATE web_voice_agents SET config=%s,version=version+1 WHERE id=%s AND version=%s RETURNING *',
                (Jsonb(body.config.model_dump()), ident, body.expected_version))).fetchone()
            if not row:
                raise HTTPException(409, 'Agent changed; reload its version before editing')
            await save_version(conn, row)
        return row

    @router.get('/agents/{ident}/versions')
    async def versions(ident: UUID, request: Request, before: int | None = None):
        pool, claims = await context(request, 'agents:read')
        async with pool.connection() as conn:
            await owned(conn, ident, claims)
            rows = await (await conn.execute('SELECT version,snapshot,created_at FROM web_voice_versions WHERE agent_id=%s AND (%s::int IS NULL OR version<%s) ORDER BY version DESC LIMIT 51', (ident, before, before))).fetchall()
        return {'versions': rows[:50], 'next_before': rows[49]['version'] if len(rows)>50 else None}

    @router.post('/agents/{ident}/restore')
    async def restore(ident: UUID, body: RestoreVersion, request: Request):
        pool, claims = await context(request, 'agents:write')
        async with pool.connection() as conn, conn.transaction():
            await owned(conn, ident, claims)
            source = await (await conn.execute('SELECT snapshot FROM web_voice_versions WHERE agent_id=%s AND version=%s',
                (ident, body.source_version))).fetchone()
            if not source:
                raise HTTPException(404, 'Agent version not found')
            saved = source['snapshot']
            # Restoring never restores revoked document access, secrets or the enabled switch.
            for doc in saved['documents']:
                if not await (await conn.execute("SELECT 1 FROM knowledge_documents WHERE id=%s AND app_id=%s AND visibility='app'", (doc, claims['app_id']))).fetchone():
                    raise HTTPException(409, 'Version uses knowledge that is no longer available')
            config = voice_config.VoiceConfig.model_validate(saved['config'])
            row = await (await conn.execute('UPDATE web_voice_agents SET name=%s,greeting=%s,fallback=%s,documents=%s,visibility=%s,language=%s,config=%s,version=version+1 WHERE id=%s AND version=%s AND deleted_at IS NULL RETURNING *',
                (saved['name'], saved['greeting'], saved['fallback'], [UUID(d) for d in saved['documents']], saved['visibility'], saved['language'],
                 Jsonb(config.model_dump()), ident, body.expected_version))).fetchone()
            if not row:
                raise HTTPException(409, 'Agent changed; reload before restoring')
            await save_version(conn, row)
        return row

    @router.get('/sessions')
    async def history(request: Request, before: UUID | None = None):
        pool, claims = await context(request, 'agents:read')
        await voice_events.expire(pool)
        async with pool.connection() as conn:
            rows = await (await conn.execute('SELECT id,agent_id,state,turns,created_at,ended_at,expires_at,transcript_consent,agent_snapshot FROM web_voice_sessions WHERE app_id=%s AND owner_id=%s AND (%s::uuid IS NULL OR id<%s) ORDER BY id DESC LIMIT 51', (claims['app_id'], claims['user'], before, before))).fetchall()
        return {'sessions': rows[:50], 'next_before': str(rows[49]['id']) if len(rows)>50 else None}

    @router.get("/agents")
    async def listing(request: Request, before: UUID | None = None):
        pool, claims = await context(request, "agents:read")
        async with pool.connection() as conn:
            rows = await (
                await conn.execute(
                    "SELECT * FROM web_voice_agents WHERE app_id=%s AND owner_id=%s AND deleted_at IS NULL AND (%s::uuid IS NULL OR id<%s) ORDER BY id DESC LIMIT 51",
                    (claims["app_id"], claims["user"], before, before),
                )
            ).fetchall()
        return {"agents": rows[:50], "next_before": str(rows[49]["id"]) if len(rows) > 50 else None}

    @router.post("/agents/{ident}/sessions", status_code=201)
    async def start(ident: UUID, body: Offer, request: Request):
        pool, claims = await context(request, "agents:run")
        return await start_for(pool, claims, ident, body)

    async def start_for(pool, claims, ident, body, handoff=None):
        if media_client.enabled():
            await media_client.check_ready()
        if not body.audio_consent:
            raise HTTPException(422, "Microphone/audio processing consent is required")
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
            with voice_config.using(agent.get("config")):
                if not ready():
                    raise HTTPException(503, "Selected voice providers are not configured")
            if not handoff and not agent["documents"] and not action_handler(agent):
                raise HTTPException(409, "Add business knowledge or configure the app action handler first")
            # Global reservation lock bounds media even with multiple API processes.
            await conn.execute("SELECT pg_advisory_xact_lock(79342020)")
            counts = await (await conn.execute('SELECT * FROM agent_voice_counts(%s)', (claims['app_id'],))).fetchone()
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
            if counts['active'] >= 2:
                raise HTTPException(429, "Voice capacity is busy. Try again after an active call ends.")
            if await (
                await conn.execute(
                    "SELECT 1 FROM web_voice_sessions WHERE app_id=%s AND owner_id=%s AND state IN ('STARTING','ACTIVE')",
                    (claims["app_id"], claims["user"]),
                )
            ).fetchone():
                raise HTTPException(409, "You already have an active voice session")
            if counts['daily'] >= 30:
                raise HTTPException(429, "Pilot limit of 30 voice sessions per app per day reached")
            if counts['monthly'] >= max(1, min(10000, int(os.environ.get('AI_VOICE_MONTHLY_SESSION_LIMIT', '300')))):
                raise HTTPException(429, 'Monthly voice session allowance reached')
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

            pinned = snapshot(agent)
            if body.caller_phone:
                pinned["caller_phone"] = body.caller_phone  # Contact only; never caller authentication.
            await conn.execute('UPDATE web_voice_sessions SET agent_snapshot=%s,transcript_consent=%s WHERE id=%s',
                (Jsonb(pinned), body.transcript_consent, call['id']))
            agent = {**agent, 'config': pinned['config'], 'retain_content': False, 'caller_phone': body.caller_phone}
            if media_client.enabled():
                await conn.execute('UPDATE web_voice_sessions SET media_remote=true WHERE id=%s', (call['id'],))
            if handoff:
                await conn.execute(
                    "UPDATE web_voice_handoffs SET consumed_at=now(),session_id=%s WHERE id=%s",
                    (call["id"], handoff["id"]),
                )
                await conn.execute(
                    "UPDATE web_voice_sessions SET handoff_id=%s WHERE id=%s", (handoff["id"], call["id"])
                )
                call["handoff_id"] = handoff["id"]

        if media_client.enabled():
            try:
                return await media_client.start(call, body.sdp)
            except Exception:  # noqa: BLE001 -- timeout may mean media accepted the offer
                async with pool.connection() as conn:
                    await conn.execute("UPDATE web_voice_sessions SET state='UNKNOWN',ended_at=now() WHERE id=%s AND state IN ('STARTING','ACTIVE')", (call['id'],))
                await voice_events.record(pool, call['id'], 'failed', {'stage': 'media_dispatch', 'outcome': 'unknown'})
                raise HTTPException(502, 'Media connection outcome is uncertain. Inspect this session; do not retry automatically.') from None
        return await runtime.start(pool, agent, call, body.sdp)

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
        stale = row['media_remote'] and row['media_heartbeat'] and time.time()-row['media_heartbeat'].timestamp()>10
        if row['state'] in {'STARTING', 'ACTIVE'} and (row['expires_at'].timestamp() <= time.time() or stale):
            async with pool.connection() as conn:
                await conn.execute("UPDATE web_voice_sessions SET state='UNKNOWN',ended_at=now() WHERE id=%s AND state IN ('STARTING','ACTIVE')", (ident,))
            row['state'] = 'UNKNOWN'
            if str(ident) in sessions:
                await sessions[str(ident)].close()
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
            "audio_connected": bool(media and media.pc.connectionState == "connected") if not row["media_remote"] else bool(row["state"] == "ACTIVE" and row["media_connected"] and row["media_heartbeat"] and time.time()-row["media_heartbeat"].timestamp()<10),
        }

    @router.get('/sessions/{ident}/events')
    async def events(ident: UUID, request: Request, after: int = 0):
        pool, claims = await context(request, 'agents:read')
        await session_row(pool, ident, claims)
        await voice_events.expire(pool)
        async with pool.connection() as conn:
            rows = await (await conn.execute('SELECT e.sequence,e.kind,e.details,e.created_at,t.role,t.content FROM web_voice_events e LEFT JOIN web_voice_transcripts t ON t.session_id=e.session_id AND t.sequence=e.sequence AND t.expires_at>now() WHERE e.session_id=%s AND e.sequence>%s ORDER BY e.sequence LIMIT 100', (ident, max(0, after)))).fetchall()
        return {'events': rows, 'next_after': rows[-1]['sequence'] if rows else after,
                'estimated_cost': None, 'cost_status': 'unknown', 'recording': False}

    @router.delete('/sessions/{ident}/transcript')
    async def delete_transcript(ident: UUID, request: Request):
        pool, claims = await context(request, 'agents:run')
        await session_row(pool, ident, claims)
        async with pool.connection() as conn, conn.transaction():
            await conn.execute('UPDATE web_voice_sessions SET transcript_consent=false WHERE id=%s', (ident,))
            await conn.execute('DELETE FROM web_voice_transcripts WHERE session_id=%s', (ident,))
        return {'deleted': True, 'future_storage_disabled': True}

    @router.post("/sessions/{ident}/end")
    async def end(ident: UUID, request: Request):
        pool, claims = await context(request, "agents:run")
        return await end_for(pool, claims, ident)

    async def end_for(pool, claims, ident):
        row = await session_row(pool, ident, claims)
        media = sessions.get(str(ident))
        if row["media_remote"] and row["state"] in {"STARTING", "ACTIVE"}:
            async with pool.connection() as conn:
                await conn.execute("UPDATE web_voice_sessions SET state='ENDED',ended_at=now() WHERE id=%s AND state IN ('STARTING','ACTIVE')", (ident,))
            return {"ended": True, "media_stop_requested": True}
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
