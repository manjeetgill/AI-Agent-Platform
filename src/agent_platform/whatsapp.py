"""Inbound-only WhatsApp support. Credentials are operator supplied, never returned by APIs."""

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from fastapi import APIRouter, Header, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse
from pydantic import Field, model_validator

from . import knowledge
from .service_identity import authenticate
from .tasks import Strict, TaskError


class Channel(Strict):
    phone_id: str = Field(pattern=r"^\d{5,30}$")
    name: str = Field(min_length=1, max_length=80)
    documents: list[UUID] = Field(default_factory=list, max_length=50)
    execution_mode: Literal['RAG', 'SAHAYAK'] = 'RAG'

    @model_validator(mode='after')
    def validate_documents(self):
        if self.execution_mode == 'RAG' and not self.documents:
            raise ValueError('RAG channels require documents')
        return self


class ClinicBridge(Strict):
    clinic_id: UUID
    url: str = Field(max_length=2048)
    secret: str = Field(min_length=32, max_length=256)


class Workflow(Strict):
    execution_mode: Literal["RAG", "SAHAYAK"]


class Activation(Strict):
    enabled: bool


class HumanReply(Strict):
    text: str = Field(min_length=1, max_length=4000)


class Takeover(Strict):
    mode: str = Field(pattern=r"^(AUTO|HUMAN)$")


class AgentDetails(Strict):
    name: str = Field(min_length=1, max_length=80)
    documents: list[UUID] = Field(min_length=1, max_length=50)


def connection_configs():
    values = json.loads(os.environ.get("AI_WHATSAPP_CONNECTIONS", "{}"))
    filename = os.environ.get("AI_WHATSAPP_CONNECTIONS_FILE")
    if filename:
        path = Path(filename)
        if path.is_symlink():
            raise HTTPException(503, "Unsafe WhatsApp credential file")
        if path.exists():
            if path.stat().st_mode & 0o077 or path.stat().st_uid != os.getuid():
                raise HTTPException(503, "WhatsApp credential file must be private")
            values.update(json.loads(path.read_text()))
    return values


def connection(phone, app=None):
    value = connection_configs().get(phone)
    if not value or (app is not None and value.get("app_id") is not None and value.get("app_id") != str(app)):
        raise HTTPException(503, "WhatsApp connection not provisioned for this app")
    if value.get('provider') == 'chakra':
        secret = value.get('webhook_secret') or os.environ.get('AI_CHAKRA_WEBHOOK_SECRET', '')
        if (not all(value.get(k) for k in ('access_token', 'plugin_id', 'waba_id'))
            or not secret or len(secret) < 32
            or value.get('phone_id') != phone
            or not re.fullmatch(r'v\d+\.\d+', value.get('graph_version', ''))):
            raise HTTPException(503, 'Chakra connection incomplete')
        return {**value, 'webhook_secret': secret}
    if value.get('provider', 'meta') != 'meta':
        raise HTTPException(503, 'Unsupported WhatsApp provider')
    if not all(value.get(k) for k in ("access_token", "app_secret", "verify_token", "graph_version")):
        raise HTTPException(503, "WhatsApp connection incomplete")
    if not re.fullmatch(r"v\d+\.\d+", value["graph_version"]):
        raise HTTPException(503, "Invalid Graph API version")
    return value


async def audit(conn, channel, conversation, event, actor):
    await conn.execute(
        "INSERT INTO whatsapp_audit(channel_id,conversation_id,event,actor) VALUES(%s,%s,%s,%s)",
        (channel, conversation, event, actor),
    )


async def ingest(pool, channel, payload):
    """Atomic message + queue record; retrying a webhook cannot create a second job."""
    from .calling_worker import ingest as ingest_calls
    await ingest_calls(pool, channel, payload)
    async with pool.connection() as conn, conn.transaction():
        for entry in payload.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value", {})
                metadata = value.get("metadata", {})
                if metadata.get("phone_number_id") != channel["phone_id"]:
                    continue
                display = re.sub(r'[^0-9]', '', str(metadata.get('display_phone_number', '')))
                if re.fullmatch(r'[1-9]\d{7,14}', display):
                    await conn.execute('''UPDATE whatsapp_channels SET display_number=%s
                        WHERE id=%s AND (display_number IS NULL OR display_number<>%s)''',
                        ('+' + display, channel['id'], '+' + display))
                for status in value.get("statuses", []):
                    if status.get("status") in {"sent", "delivered", "read", "failed"}:
                        await conn.execute(
                            """UPDATE whatsapp_outbound SET delivery=%s,updated_at=now()
                            WHERE channel_id=%s AND outbound_id=%s AND
                            (delivery IS NULL OR delivery NOT IN ('read','delivered')
                             OR (delivery='delivered' AND %s='read'))""",
                            (status["status"], channel["id"], status.get("id"), status["status"]),
                        )
                        await conn.execute(
                            """UPDATE whatsapp_alert_deliveries SET delivery=%s,updated_at=now()
                            WHERE channel_id=%s AND provider_id=%s AND
                            (delivery IS NULL OR delivery NOT IN ('read','delivered')
                             OR (delivery='delivered' AND %s='read'))""",
                            (status['status'], channel['id'], status.get('id'), status['status']),
                        )
                        # Ignore stale receipts; sent/read evidence takes precedence over uncertainty.
                        await conn.execute(
                            """UPDATE whatsapp_messages m SET delivery=%s,updated_at=now()
                            FROM whatsapp_conversations c WHERE m.conversation_id=c.id AND c.channel_id=%s
                            AND m.outbound_id=%s AND (m.delivery IS NULL OR m.delivery NOT IN ('read','delivered')
                                OR (m.delivery='delivered' AND %s='read'))""",
                            (status["status"], channel["id"], status.get("id"), status["status"]),
                        )
                for msg in value.get("messages", []):
                    sender, mid = msg.get("from", ""), msg.get("id", "")
                    if (
                        not re.fullmatch(r"\d{5,20}", sender)
                        or not isinstance(mid, str)
                        or not 1 <= len(mid) <= 300
                    ):
                        continue
                    try:
                        at = datetime.fromtimestamp(int(msg["timestamp"]), UTC)
                    except (KeyError, ValueError, TypeError, OverflowError, OSError):
                        continue
                    if at > datetime.now(UTC) + timedelta(minutes=5):
                        continue
                    body = msg.get("text", {}).get("body", "") if msg.get("type") == "text" else ""
                    if msg.get('type') == 'interactive' and channel.get('execution_mode') == 'SAHAYAK':
                        selection = msg.get('interactive', {})
                        if isinstance(selection, dict):
                            chosen = selection.get('button_reply') or selection.get('list_reply') or {}
                            body = chosen.get('id', '') if isinstance(chosen, dict) else ''
                            if not isinstance(body, str) or not re.fullmatch(r'sc:[a-f0-9]{20}:\d', body):
                                body = ''
                    if not isinstance(body, str):
                        body = ""
                    conversation = await (
                        await conn.execute(
                            """INSERT INTO whatsapp_conversations(id,channel_id,customer)
                        VALUES(%s,%s,%s) ON CONFLICT(channel_id,customer) DO UPDATE SET updated_at=now() RETURNING *""",
                            (uuid4(), channel["id"], sender),
                        )
                    ).fetchone()
                    from .whatsapp_alerts import consume_verification
                    verification = await consume_verification(conn, channel['id'], sender, body)
                    clinic_control = channel.get('execution_mode') == 'SAHAYAK' and body.strip().lower() in {'human', 'agent', 'stop', 'unsubscribe'}
                    handoff = (
                        not body.strip()
                        or len(body) > 1500
                        or (not clinic_control and body.strip().lower() in {"human", "agent", "stop", "unsubscribe"})
                    )
                    state = (
                        "CANCELLED" if verification else "HANDOFF"
                        if handoff or conversation["mode"] == "HUMAN" or not channel["enabled"]
                        else "PENDING"
                    )
                    inserted = await (
                        await conn.execute(
                            """INSERT INTO whatsapp_messages
                        (id,conversation_id,provider_id,body,received_at,status) VALUES(%s,%s,%s,%s,%s,%s)
                        ON CONFLICT(provider_id) DO NOTHING RETURNING id""",
                            (uuid4(), conversation["id"], mid, body[:1500], at, state),
                        )
                    ).fetchone()
                    if inserted:
                        from .whatsapp_opt_in import consume_alert_start
                        await consume_alert_start(conn, channel['id'], conversation['id'], inserted['id'], body, at)
                        if verification:
                            await audit(conn, channel['id'], conversation['id'], 'recipient:verified', 'provider')
                        if clinic_control:
                            # Let Sahayak persist consent/handoff, but cancel older queued automatic replies immediately.
                            await conn.execute("UPDATE whatsapp_messages SET status='CANCELLED' WHERE conversation_id=%s AND id<>%s AND NOT human_reply AND status IN ('PENDING','GENERATING','READY')", (conversation['id'], inserted['id']))
                        if body.strip().lower() in {"stop", "unsubscribe"}:
                            await conn.execute('UPDATE whatsapp_conversations SET opted_out=true WHERE id=%s', (conversation['id'],))
                        if handoff:
                            await conn.execute(
                                "UPDATE whatsapp_conversations SET mode='HUMAN' WHERE id=%s",
                                (conversation["id"],),
                            )
                            await conn.execute(
                                "UPDATE whatsapp_messages SET status='CANCELLED' WHERE conversation_id=%s AND NOT human_reply AND status IN ('PENDING','GENERATING','READY')",
                                (conversation["id"],),
                            )
                        await audit(conn, channel["id"], conversation["id"], "inbound:" + state, "provider")


def install_whatsapp(app):
    router = APIRouter()

    async def context(request, scope):
        pool = request.app.state.repository.pool
        claims = await authenticate(
            pool, request.headers.get("authorization", ""), request.headers.get("X-External-User-ID"), scope
        )
        return pool, claims

    async def owned(conn, channel_id, claims, lock=False):
        row = await (
            await conn.execute(
                """SELECT * FROM whatsapp_channels
            WHERE id=%s AND app_id=%s AND owner_id=%s"""
                + (" FOR UPDATE" if lock else ""),
                (channel_id, claims["app_id"], claims["user"]),
            )
        ).fetchone()
        if not row:
            raise HTTPException(404, "Channel not found")
        return row

    @router.get("/v1/whatsapp/setup")
    async def setup(request: Request):
        pool, claims = await context(request, "channels:read")
        numbers = []
        for phone, value in connection_configs().items():
            if value.get("app_id") == claims["app_id"]:
                try:
                    connection(phone, claims["app_id"])
                    numbers.append(phone)
                except HTTPException:
                    continue
        async with pool.connection() as conn:
            managed = await (await conn.execute("SELECT phone_id FROM meta_connections WHERE app_id=%s AND owner_id=%s AND state='CONNECTED' AND checked_at>now()-interval '15 minutes' AND (token_expires_at IS NULL OR token_expires_at>now())", (claims['app_id'],claims['user']))).fetchall()
            reserved = await (await conn.execute('SELECT phone_id FROM meta_connections UNION SELECT phone_id FROM whatsapp_channels WHERE app_id<>%s OR owner_id<>%s', (claims['app_id'], claims['user']))).fetchall()
            numbers = [phone for phone in numbers if phone not in {row['phone_id'] for row in reserved}]
            chakra = await (await conn.execute("SELECT phone_id FROM chakra_customer_connections WHERE app_id=%s AND owner_id=%s AND state='CONNECTED'", (claims['app_id'],claims['user']))).fetchall()
            managed_numbers = [row['phone_id'] for row in managed + chakra]
            numbers = list(dict.fromkeys(numbers + managed_numbers))
            health = await (
                await conn.execute(
                    "SELECT * FROM whatsapp_worker_health WHERE id='worker' AND last_seen>now()-interval '90 seconds'"
                )
            ).fetchone()
        public = os.environ.get("AI_PLATFORM_PUBLIC_URL", "").rstrip("/")
        if urlsplit(public).scheme != "https" or not urlsplit(public).hostname:
            public = ""
        return {
            "app_id": claims["app_id"],
            "phone_ids": numbers,
            "managed_phone_ids": managed_numbers,
            "public_url": public,
            "worker_online": bool(health),
            "model_configured": bool(health and health["model_configured"]),
        }

    @router.get('/v1/whatsapp/usage')
    async def usage(request: Request):
        pool, claims = await context(request, 'channels:read')
        async with pool.connection() as conn:
            policy = await (await conn.execute('SELECT expires_at,whatsapp_turns,whatsapp_sends,voice_calls FROM channel_trial_policies WHERE app_id=%s', (claims['app_id'],))).fetchone()
            rows = await (await conn.execute("SELECT resource,units FROM channel_trial_usage WHERE app_id=%s AND day=(now() AT TIME ZONE 'UTC')::date", (claims['app_id'],))).fetchall()
        return {'trial_policy': policy, 'usage_today': rows, 'unit': 'operations', 'monetary_budget': False}

    @router.post("/v1/whatsapp/channels", status_code=201)
    async def create(body: Channel, request: Request):
        pool, claims = await context(request, "channels:write")
        from .meta_worker import managed_config
        await managed_config(pool, body.phone_id, claims["app_id"], claims['user'])
        async with pool.connection() as conn, conn.transaction():
            for document in body.documents:
                row = await (
                    await conn.execute(
                        "SELECT id FROM knowledge_documents WHERE id=%s AND app_id=%s AND visibility='app'",
                        (document, claims["app_id"]),
                    )
                ).fetchone()
                if not row:
                    raise HTTPException(422, "Select app-shared customer-facing documents")
            row = await (
                await conn.execute(
                    """INSERT INTO whatsapp_channels(id,app_id,owner_id,phone_id,name,documents,execution_mode)
                VALUES(%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(phone_id) DO NOTHING RETURNING *""",
                    (uuid4(), claims["app_id"], claims["user"], body.phone_id, body.name, body.documents, body.execution_mode),
                )
            ).fetchone()
            if not row:
                raise HTTPException(409, "Number already connected")
            await audit(conn, row["id"], None, "created:disabled", claims["principal"])
            return row

    @router.put("/v1/whatsapp/channels/{channel_id}")
    async def edit_agent(channel_id: UUID, body: AgentDetails, request: Request):
        pool, claims = await context(request, "channels:write")
        async with pool.connection() as conn, conn.transaction():
            row = await owned(conn, channel_id, claims, True)
            if row["enabled"]:
                raise HTTPException(409, "Pause automatic replies before editing your agent")
            for document in body.documents:
                found = await (await conn.execute(
                    "SELECT id FROM knowledge_documents WHERE id=%s AND app_id=%s AND visibility='app'",
                    (document, claims["app_id"]))).fetchone()
                if not found:
                    raise HTTPException(422, "Select customer-facing documents from your application")
            await conn.execute("UPDATE whatsapp_channels SET name=%s,documents=%s WHERE id=%s",
                               (body.name, body.documents, channel_id))
            await audit(conn, channel_id, None, "agent:updated", claims["principal"])
        return {"saved": True}

    @router.put('/v1/whatsapp/channels/{channel_id}/sahayak')
    async def configure_clinic(channel_id: UUID, body: ClinicBridge, request: Request):
        from .meta_crypto import seal, unseal
        from .sahayak_bridge import binding
        from .sahayak_bridge import context as bridge_context
        pool, claims = await context(request, 'channels:write')
        url = urlsplit(body.url)
        allowed = json.loads(os.environ.get('AI_SAHAYAK_ALLOWED_ORIGINS', '[]'))
        if url.scheme+'://'+url.netloc not in allowed or url.path != '/integrations/agent-platform/whatsapp':
            raise HTTPException(422, 'Clinic endpoint must use an operator-approved origin and bridge path')
        async with pool.connection() as conn, conn.transaction():
            row = await owned(conn, channel_id, claims, True)
            if row['execution_mode'] != 'SAHAYAK':
                raise HTTPException(409, 'Select the clinic workflow first')
            cfg = {'app_id':str(row['app_id']), 'clinic_id':str(body.clinic_id), 'url':body.url, 'secret':body.secret}
            try:
                binding(row, cfg)
            except ValueError:
                raise HTTPException(422, 'Invalid clinic bridge configuration') from None
            existing = await (await conn.execute('SELECT config_cipher FROM sahayak_channel_bridges WHERE channel_id=%s', (channel_id,))).fetchone()
            if existing:
                old = json.loads(unseal(existing['config_cipher'], bridge_context(row)))
                if old == cfg:
                    return {'configured':True}
                raise HTTPException(409, 'Existing clinic binding cannot be reassigned through setup')
            if row['enabled']:
                raise HTTPException(409, 'Pause before configuring the clinic bridge')
            await conn.execute('INSERT INTO sahayak_channel_bridges(channel_id,config_cipher) VALUES(%s,%s)', (channel_id,seal(json.dumps(cfg),bridge_context(row))))
            await audit(conn, channel_id, None, 'sahayak:configured', claims['principal'])
        return {'configured':True}

    @router.put('/v1/whatsapp/channels/{channel_id}/workflow')
    async def workflow(channel_id: UUID, body: Workflow, request: Request):
        pool, claims = await context(request, 'channels:write')
        async with pool.connection() as conn, conn.transaction():
            row = await owned(conn, channel_id, claims, True)
            pending = await (await conn.execute("SELECT 1 FROM whatsapp_messages m JOIN whatsapp_conversations c ON c.id=m.conversation_id WHERE c.channel_id=%s AND m.status IN ('PENDING','GENERATING','READY','SENDING','UNKNOWN') LIMIT 1", (channel_id,))).fetchone()
            if row['enabled'] or pending:
                raise HTTPException(409, 'Pause the channel and reconcile pending messages before changing its workflow')
            if body.execution_mode == 'RAG' and not row['documents']:
                raise HTTPException(409, 'RAG requires customer-facing documents')
            await conn.execute('UPDATE whatsapp_channels SET execution_mode=%s WHERE id=%s', (body.execution_mode, channel_id))
            await audit(conn, channel_id, None, 'workflow:'+body.execution_mode, claims['principal'])
        return {'execution_mode': body.execution_mode, 'enabled': False}

    @router.post("/v1/whatsapp/channels/{channel_id}/preview")
    async def preview(channel_id: UUID, body: knowledge.Ask, request: Request):
        pool, claims = await context(request, "channels:write")
        async with pool.connection() as conn:
            row = await owned(conn, channel_id, claims)
        if row.get('execution_mode') == 'SAHAYAK':
            raise HTTPException(409, 'Use the Sahayak clinic simulator for booking previews')
        try:
            async with asyncio.timeout(45):
                result = await knowledge.answer(pool, knowledge.gateway(), row["app_id"], row["id"],
                    body.question, "preview:" + body.idempotency_key, documents=row["documents"])
            return {"result": result, "handoff": not result["claims"] or bool(result["missing"]),
                    "sent_to_whatsapp": False}
        except TaskError as exc:
            raise HTTPException(409, str(exc)) from None
        except Exception:  # noqa: BLE001 -- do not expose provider errors or credentials
            raise HTTPException(503, "Preview unavailable. Check again with the same request key.") from None

    @router.get("/v1/whatsapp/channels")
    async def channels(request: Request, before: UUID | None = None, limit: int = Query(25, ge=1, le=100)):
        pool, claims = await context(request, "channels:read")
        async with pool.connection() as conn:
            return await (
                await conn.execute(
                    """SELECT * FROM whatsapp_channels WHERE app_id=%s AND owner_id=%s
                AND (%s::uuid IS NULL OR id<%s) ORDER BY id DESC LIMIT %s""",
                    (claims["app_id"], claims["user"], before, before, limit),
                )
            ).fetchall()

    @router.post("/v1/whatsapp/channels/{channel_id}/activation")
    async def activate(channel_id: UUID, body: Activation, request: Request):
        pool, claims = await context(request, "channels:write")
        async with pool.connection() as conn, conn.transaction():
            row = await owned(conn, channel_id, claims, True)
            from .meta_worker import managed_config
            await managed_config(pool, row["phone_id"], claims["app_id"], claims['user'])
            if body.enabled:
                clinic_mode = row.get('execution_mode') == 'SAHAYAK'
                if clinic_mode:
                    try:
                        from .sahayak_bridge import probe
                        await probe(row, pool=pool)
                    except Exception:  # noqa: BLE001 -- fail closed without exposing bridge credentials or errors
                        raise HTTPException(409, 'Sahayak clinic bridge must be configured and reachable before activation') from None
                health = await (
                    await conn.execute(
                        "SELECT 1 FROM whatsapp_worker_health WHERE id='worker' AND (model_configured OR %s) AND last_seen>now()-interval '90 seconds'",
                        (clinic_mode,),
                    )
                ).fetchone()
                if not row["webhook_verified_at"] or not health:
                    raise HTTPException(
                        409,
                        "Verify the Meta webhook and start the configured WhatsApp worker before activation",
                    )
            await conn.execute(
                "UPDATE whatsapp_channels SET enabled=%s WHERE id=%s", (body.enabled, channel_id)
            )
            if not body.enabled:
                await conn.execute(
                    """UPDATE whatsapp_messages SET status='CANCELLED' WHERE status IN ('PENDING','GENERATING','READY') AND NOT human_reply
                    AND conversation_id IN (SELECT id FROM whatsapp_conversations WHERE channel_id=%s)""",
                    (channel_id,),
                )
            await audit(conn, channel_id, None, "enabled:" + str(body.enabled), claims["principal"])
        return {"enabled": body.enabled}

    @router.get("/v1/whatsapp/channels/{channel_id}/conversations")
    async def conversations(
        channel_id: UUID, request: Request, before: UUID | None = None, limit: int = Query(25, ge=1, le=100)
    ):
        pool, claims = await context(request, "channels:read")
        async with pool.connection() as conn:
            await owned(conn, channel_id, claims)
            return await (
                await conn.execute(
                    """SELECT * FROM whatsapp_conversations WHERE channel_id=%s
                AND (%s::uuid IS NULL OR id<%s) ORDER BY id DESC LIMIT %s""",
                    (channel_id, before, before, limit),
                )
            ).fetchall()

    @router.get("/v1/whatsapp/channels/{channel_id}/conversations/{conversation_id}")
    async def history(
        channel_id: UUID,
        conversation_id: UUID,
        request: Request,
        before: UUID | None = None,
        limit: int = Query(50, ge=1, le=100),
    ):
        pool, claims = await context(request, "channels:read")
        async with pool.connection() as conn:
            await owned(conn, channel_id, claims)
            return await (
                await conn.execute(
                    """SELECT m.* FROM whatsapp_messages m JOIN whatsapp_conversations c
                ON c.id=m.conversation_id WHERE c.id=%s AND c.channel_id=%s
                AND (%s::uuid IS NULL OR m.id<%s) ORDER BY m.id DESC LIMIT %s""",
                    (conversation_id, channel_id, before, before, limit),
                )
            ).fetchall()

    @router.post("/v1/whatsapp/channels/{channel_id}/conversations/{conversation_id}/mode")
    async def mode(channel_id: UUID, conversation_id: UUID, body: Takeover, request: Request):
        pool, claims = await context(request, "channels:write")
        async with pool.connection() as conn, conn.transaction():
            await owned(conn, channel_id, claims, True)
            row = await (
                await conn.execute(
                    """UPDATE whatsapp_conversations SET mode=%s,updated_at=now()
                WHERE id=%s AND channel_id=%s RETURNING id""",
                    (body.mode, conversation_id, channel_id),
                )
            ).fetchone()
            if not row:
                raise HTTPException(404, "Conversation not found")
            # Never replay old questions when automation resumes.
            await conn.execute(
                "UPDATE whatsapp_messages SET status='CANCELLED' WHERE conversation_id=%s AND status IN ('PENDING','GENERATING','READY')",
                (conversation_id,),
            )
            await audit(conn, channel_id, conversation_id, "mode:" + body.mode, claims["principal"])
        return {"mode": body.mode}

    @router.post("/v1/whatsapp/channels/{channel_id}/messages/{message_id}/reply")
    async def human_reply(channel_id: UUID, message_id: UUID, body: HumanReply, request: Request):
        pool, claims = await context(request, "channels:write")
        async with pool.connection() as conn, conn.transaction():
            channel = await owned(conn, channel_id, claims, True)
            if not channel["webhook_verified_at"]:
                raise HTTPException(409, "Verify the business connection before sending replies")
            row = await (
                await conn.execute(
                    """SELECT m.*,c.mode FROM whatsapp_messages m
                JOIN whatsapp_conversations c ON c.id=m.conversation_id
                WHERE m.id=%s AND c.channel_id=%s FOR UPDATE OF c,m""",
                    (message_id, channel_id),
                )
            ).fetchone()
            if not row:
                raise HTTPException(404, "Message not found")
            if row["human_reply"] and row["reply"] == body.text:
                return {"status": row["status"]}
            if row["mode"] != "HUMAN" or row["status"] not in {"HANDOFF", "CANCELLED"}:
                raise HTTPException(
                    409, "Take over first; uncertain or dispatched replies cannot be replaced"
                )
            if row["received_at"] < datetime.now(UTC) - timedelta(hours=23, minutes=55):
                raise HTTPException(409, "Reply window expired; wait for a new customer message")
            await conn.execute(
                "UPDATE whatsapp_messages SET reply=%s,human_reply=true,status='READY',evidence=NULL WHERE id=%s",
                (body.text, message_id),
            )
            await audit(conn, channel_id, row["conversation_id"], "human:reply-queued", claims["principal"])
        return {"status": "READY"}

    async def webhook_channel(request, channel_id):
        pool = request.app.state.repository.pool
        from .db_scope import connection_identity
        await connection_identity(pool, "channel", channel_id)
        async with pool.connection() as conn:
            row = await (
                await conn.execute("SELECT * FROM whatsapp_channels WHERE id=%s", (channel_id,))
            ).fetchone()
        if not row:
            raise HTTPException(404, "Channel not found")
        from .meta_worker import managed_config
        return pool, row, await managed_config(pool, row["phone_id"], row["app_id"], row["owner_id"])

    @router.get("/webhooks/whatsapp/{channel_id}", response_class=PlainTextResponse)
    async def verify(channel_id: UUID, request: Request):
        pool, _, config = await webhook_channel(request, channel_id)
        if config.get('provider') == 'chakra':
            raise HTTPException(405, 'Chakra verifies signed POST events')
        if request.query_params.get("hub.mode") != "subscribe" or not hmac.compare_digest(
            request.query_params.get("hub.verify_token", ""), config["verify_token"]
        ):
            raise HTTPException(403, "Verification failed")
        async with pool.connection() as conn:
            await conn.execute(
                "UPDATE whatsapp_channels SET webhook_verified_at=now() WHERE id=%s", (channel_id,)
            )
        return request.query_params.get("hub.challenge", "")[:200]

    @router.post("/webhooks/whatsapp/{channel_id}")
    async def webhook(channel_id: UUID, request: Request, x_hub_signature_256: str = Header(default="")):
        pool, row, config = await webhook_channel(request, channel_id)
        raw = bytearray()
        async for part in request.stream():
            raw.extend(part)
            if len(raw) > 256000:
                raise HTTPException(413, "Webhook too large")
        if config.get('provider') == 'chakra':
            from .chakra_client import ChakraError, verify_event
            try:
                payload = verify_event(bytes(raw), request.headers.get('X-Chakra-Signature-256', ''),
                                       config['webhook_secret'], phone_id=row['phone_id'],
                                       waba_id=config['waba_id'])
            except ChakraError as exc:
                logging.getLogger(__name__).warning(
                    'Chakra webhook rejected channel=%s reason=%s signature_present=%s',
                    channel_id, exc.code, bool(request.headers.get('X-Chakra-Signature-256')))
                raise HTTPException(403, 'Invalid Chakra webhook') from exc
            try:
                await ingest(pool, row, payload)
            except (ValueError, TypeError, AttributeError):
                raise HTTPException(400, 'Invalid webhook payload') from None
            async with pool.connection() as conn:
                await conn.execute('UPDATE whatsapp_channels SET webhook_verified_at=now() WHERE id=%s',
                                   (channel_id,))
            return {'received': True}
        signature = "sha256=" + hmac.new(config["app_secret"].encode(), raw, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, x_hub_signature_256):
            raise HTTPException(403, "Invalid webhook signature")
        try:
            payload = json.loads(raw)
            if payload.get("object") != "whatsapp_business_account":
                raise ValueError()
            await ingest(pool, row, payload)
        except (ValueError, TypeError, AttributeError):
            raise HTTPException(400, "Invalid webhook payload") from None
        return {"received": True}

    app.include_router(router)
