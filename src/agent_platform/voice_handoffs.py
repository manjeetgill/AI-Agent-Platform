"""Short-lived, conversation-bound WhatsApp → internet voice invitations."""

import hashlib
import json
import os
import re
import secrets
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID, uuid4, uuid5

from fastapi import HTTPException, Request
from fastapi.responses import FileResponse

from . import sahayak_bridge, voice_ice

ASSETS = Path(__file__).parent / "console"
HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; media-src 'self' blob:; frame-ancestors 'none'; form-action 'self'",
    "Permissions-Policy": "microphone=(self), camera=(), geolocation=()",
}


def public_origin():
    value = os.environ.get("AI_WEB_VOICE_PUBLIC_ORIGIN", "").rstrip("/")
    url = urlsplit(value)
    if (
        os.environ.get("AI_WEB_VOICE_ENABLED") != "1"
        or not url.hostname
        or (
            url.scheme != "https"
            and not (url.scheme == "http" and url.hostname in {"localhost", "127.0.0.1"})
        )
        or url.username
        or url.password
        or url.query
        or url.fragment
        or url.path
    ):
        return None
    return value


async def issue(pool, channel, conversation, message):
    origin = public_origin()
    if not origin or channel.get("execution_mode") != "SAHAYAK":
        raise ValueError("Internet voice invitation unavailable")
    token = secrets.token_urlsafe(32)
    owner = uuid5(UUID(str(channel["app_id"])), "voice-conversation:" + str(conversation["id"]))
    async with pool.connection() as conn, conn.transaction():
        agent = await (
            await conn.execute(
                """INSERT INTO web_voice_agents(id,app_id,owner_id,external_id,name,greeting,fallback)
            VALUES(%s,%s,%s,%s,'AI receptionist','Continue your appointment booking.',
            'Please return to WhatsApp or contact the clinic reception.')
            ON CONFLICT(app_id,owner_id,external_id) DO UPDATE SET external_id=EXCLUDED.external_id RETURNING id""",
                (uuid4(), channel["app_id"], channel["owner_id"], "whatsapp:" + str(channel["id"])),
            )
        ).fetchone()
        await conn.execute(
            """INSERT INTO web_voice_handoffs(id,token_hash,message_id,conversation_id,agent_id,app_id,owner_id)
            VALUES(%s,%s,%s,%s,%s,%s,%s)""",
            (
                uuid4(),
                hashlib.sha256(token.encode()).hexdigest(),
                message["id"],
                conversation["id"],
                agent["id"],
                channel["app_id"],
                owner,
            ),
        )
    # Fragment keeps the secret out of normal HTTP/proxy access logs and referrers.
    return origin + "/voice/call#" + token


async def clinic_turn(pool, handoff_id, event, text, *, resume=False):
    async with pool.connection() as conn:
        row = await (
            await conn.execute(
                """SELECT h.conversation_id FROM web_voice_handoffs h WHERE h.id=%s""",
                (handoff_id,),
            )
        ).fetchone()
        conversation = (
            await (
                await conn.execute(
                    "SELECT * FROM whatsapp_conversations WHERE id=%s", (row["conversation_id"],)
                )
            ).fetchone()
            if row
            else None
        )
        channel = (
            await (
                await conn.execute(
                    "SELECT * FROM whatsapp_channels WHERE id=%s", (conversation["channel_id"],)
                )
            ).fetchone()
            if conversation
            else None
        )
    if (
        not channel
        or not channel["enabled"]
        or conversation["mode"] != "AUTO"
        or channel["execution_mode"] != "SAHAYAK"
    ):
        return {"action": "handoff", "text": "Please return to WhatsApp or contact the clinic reception."}
    result = await sahayak_bridge.request(
        channel, event, conversation["customer"], text, pool=pool, voice_resume=resume, voice_turn=not resume
    )
    stop = result["handledBy"] == "human" or result["redFlag"]
    if stop:
        async with pool.connection() as conn:
            await conn.execute(
                "UPDATE whatsapp_conversations SET mode='HUMAN' WHERE id=%s", (conversation["id"],)
            )
    return {
        "action": "handoff" if stop else "continue",
        "text": "\n".join(result["replies"])[:1200]
        or "Please return to WhatsApp or contact the clinic reception.",
    }


def install(app, start, status, end):
    @app.middleware("http")
    async def privacy_headers(request, next_handler):
        response = await next_handler(request)
        if request.url.path.startswith("/public/voice/"):
            response.headers["Cache-Control"] = "no-store"
            response.headers["Referrer-Policy"] = "no-referrer"
        return response

    async def authorized(request):
        header = request.headers.get("Authorization", "")
        token = header.removeprefix("Bearer ")
        if not header.startswith("Bearer ") or not re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
            raise HTTPException(401, "Open the private voice link from your WhatsApp conversation")
        pool = request.app.state.repository.pool
        async with pool.connection() as conn:
            row = await (
                await conn.execute(
                    """SELECT h.* FROM web_voice_handoffs h
                JOIN whatsapp_conversations c ON c.id=h.conversation_id
                JOIN whatsapp_channels w ON w.id=c.channel_id
                JOIN web_voice_agents a ON a.id=h.agent_id
                WHERE h.token_hash=%s AND h.expires_at>now() AND w.enabled AND a.enabled
                AND c.mode='AUTO' AND w.execution_mode='SAHAYAK'""",
                    (hashlib.sha256(token.encode()).hexdigest(),),
                )
            ).fetchone()
        if not row:
            raise HTTPException(410, "This link is unavailable or expired. Request a new one in WhatsApp.")
        return pool, row, {"app_id": row["app_id"], "user": row["owner_id"]}

    @app.get("/voice/call")
    async def page():
        return FileResponse(ASSETS / "voice-call.html", headers=HEADERS)

    @app.get("/voice/assets/{name}")
    async def asset(name: str):
        if name not in {"voice-call.js", "voice-widget.js", "style.css"}:
            raise HTTPException(404)
        return FileResponse(ASSETS / name, headers=HEADERS)

    @app.get("/public/voice/invitation")
    async def invitation(request: Request):
        _, row, _ = await authorized(request)
        return {
            "agent_id": row["agent_id"],
            "used": row["consumed_at"] is not None,
            "expires_at": row["expires_at"],
            "ice_servers": voice_ice.servers(row["id"])
            or json.loads(os.environ.get("AI_WEB_VOICE_BROWSER_ICE_SERVERS", "[]")),
        }

    from .web_voice import Offer

    @app.post("/public/voice/session", status_code=201)
    async def create(body: Offer, request: Request):
        pool, row, claims = await authorized(request)
        return await start(pool, claims, row["agent_id"], body, handoff=row)

    @app.get("/public/voice/session/{ident}")
    async def read(ident: UUID, request: Request):
        pool, row, claims = await authorized(request)
        if row["session_id"] != ident:
            raise HTTPException(404)
        return await status(pool, claims, ident)

    @app.post("/public/voice/session/{ident}/end")
    async def stop(ident: UUID, request: Request):
        pool, row, claims = await authorized(request)
        if row["session_id"] != ident:
            raise HTTPException(404)
        return await end(pool, claims, ident)
