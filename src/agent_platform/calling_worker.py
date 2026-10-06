"""Single-owner pilot WhatsApp WebRTC worker; durable signaling with no blind send retries."""

import asyncio
import hashlib
import json
import os
import time
from uuid import uuid4

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from . import calling_media, calling_provider
from .calling import configuration_hash, connected
from .calling_dialogue import respond


async def ingest(pool, channel, payload):
    async with pool.connection() as conn, conn.transaction():
        agent = await (
            await conn.execute(
                "SELECT id FROM calling_agents WHERE whatsapp_channel_id=%s AND app_id=%s AND owner_id=%s",
                (channel["id"], channel["app_id"], channel["owner_id"]),
            )
        ).fetchone()
        if not agent:
            return
        for entry in payload.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value", {})
                if (
                    change.get("field") != "calls"
                    or value.get("metadata", {}).get("phone_number_id") != channel["phone_id"]
                ):
                    continue
                for event in value.get("calls", []):
                    if (
                        not isinstance(event, dict)
                        or not isinstance(event.get("id"), str)
                        or not event["id"].startswith("wacid.")
                        or len(event["id"]) > 300
                    ):
                        continue
                    if event.get("event") not in {"connect", "terminate"}:
                        continue
                    raw = json.dumps(event, sort_keys=True, separators=(",", ":"))
                    if len(raw) > 70000:
                        continue
                    await conn.execute(
                        "INSERT INTO whatsapp_call_events(agent_id,provider_call_id,event_hash,payload) VALUES(%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                        (agent["id"], event["id"], hashlib.sha256(raw.encode()).hexdigest(), Jsonb(event)),
                    )


class Worker:
    def __init__(self, pool):
        self.pool = pool
        self.sessions = {}

    async def session(self, agent, channel, cfg, call):
        async def dialogue(text):
            return await respond(self.pool, agent, channel, call, text)

        async def ended():
            self.sessions.pop(call["provider_call_id"], None)
            if not call["provider_call_id"]:
                return
            try:
                await calling_provider.request(
                    cfg,
                    channel["phone_id"],
                    "calls",
                    method="POST",
                    payload={
                        "messaging_product": "whatsapp",
                        "action": "terminate",
                        "call_id": call["provider_call_id"],
                    },
                )
            except calling_provider.CallingError:
                pass  # Session is already closed locally. Do not retry uncertain provider mutations.
            async with self.pool.connection() as conn:
                await conn.execute(
                    "UPDATE calling_calls SET state='COMPLETED',updated_at=now() WHERE id=%s AND state='ACTIVE'",
                    (call["id"],),
                )
                await conn.execute(
                    "UPDATE whatsapp_call_requests SET state='COMPLETED',updated_at=now() WHERE provider_call_id=%s AND state IN ('SUBMITTED','CONNECTED')",
                    (call["provider_call_id"],),
                )

        result = calling_media.Session(dialogue, agent["greeting"], ended)
        return result

    async def event(self):
        async with self.pool.connection() as conn, conn.transaction():
            row = await (
                await conn.execute(
                    "SELECT * FROM whatsapp_call_events WHERE state='PENDING' ORDER BY id FOR UPDATE SKIP LOCKED LIMIT 1"
                )
            ).fetchone()
            if not row:
                return False
            await conn.execute("UPDATE whatsapp_call_events SET state='PROCESSING' WHERE id=%s", (row["id"],))
            agent = await (
                await conn.execute("SELECT * FROM calling_agents WHERE id=%s", (row["agent_id"],))
            ).fetchone()
        event, provider_id = row["payload"], row["provider_call_id"]
        state = "UNKNOWN"
        try:
            channel, cfg = await connected(self.pool, agent)
            if event["event"] == "terminate":
                session = self.sessions.get(provider_id)
                if session:
                    await session.close()
                async with self.pool.connection() as conn:
                    await conn.execute(
                        "INSERT INTO calling_calls(id,agent_id,provider_call_id,caller,state) VALUES(%s,%s,%s,'','COMPLETED') ON CONFLICT(agent_id,provider_call_id) DO NOTHING",
                        (uuid4(), agent["id"], provider_id),
                    )
                    await conn.execute(
                        "UPDATE calling_calls SET state='COMPLETED',duration_seconds=%s,updated_at=now() WHERE agent_id=%s AND provider_call_id=%s",
                        (min(max(int(event.get("duration", 0)), 0), 86400), agent["id"], provider_id),
                    )
                    await conn.execute(
                        "UPDATE whatsapp_call_requests SET state='COMPLETED',updated_at=now() WHERE agent_id=%s AND provider_call_id=%s",
                        (agent["id"], provider_id),
                    )
                state = "DONE"
            elif event.get("direction") == "BUSINESS_INITIATED":
                session = self.sessions.get(provider_id)
                if session and event.get("session", {}).get("sdp_type") == "answer":
                    await session.remote(event["session"]["sdp"], "answer")
                    state = "DONE"
            elif event.get("direction") == "USER_INITIATED":
                async with self.pool.connection() as conn:
                    exists = await (
                        await conn.execute(
                            "SELECT 1 FROM calling_calls WHERE agent_id=%s AND provider_call_id=%s",
                            (agent["id"], provider_id),
                        )
                    ).fetchone()
                if exists:
                    state = "DONE"  # Never answer a previously seen call again.
                elif not agent["enabled"] or len(self.sessions) >= 2:
                    await calling_provider.request(
                        cfg,
                        channel["phone_id"],
                        "calls",
                        method="POST",
                        payload={"messaging_product": "whatsapp", "action": "reject", "call_id": provider_id},
                    )
                    state = "DONE"
                elif (
                    event.get("session", {}).get("sdp_type") == "offer"
                    and time.time() - float(event["timestamp"]) < 60
                ):
                    async with self.pool.connection() as conn:
                        call = await (
                            await conn.execute(
                                "INSERT INTO calling_calls(id,agent_id,provider_call_id,caller) VALUES(%s,%s,%s,%s) RETURNING *",
                                (
                                    uuid4(),
                                    agent["id"],
                                    provider_id,
                                    "+" + str(event.get("from", "")).lstrip("+")[:20],
                                ),
                            )
                        ).fetchone()
                    session = await self.session(agent, channel, cfg, call)
                    self.sessions[provider_id] = session
                    answer = await session.remote(event["session"]["sdp"], "offer")
                    await calling_provider.request(
                        cfg,
                        channel["phone_id"],
                        "calls",
                        method="POST",
                        payload={
                            "messaging_product": "whatsapp",
                            "action": "pre_accept",
                            "call_id": provider_id,
                            "session": {"sdp_type": "answer", "sdp": answer},
                        },
                    )
                    await calling_provider.request(
                        cfg,
                        channel["phone_id"],
                        "calls",
                        method="POST",
                        payload={"messaging_product": "whatsapp", "action": "accept", "call_id": provider_id},
                    )
                    state = "DONE"
        except Exception:  # noqa: BLE001 -- a failed call operation is fenced, not blindly retried
            session = self.sessions.get(provider_id)
            if session:
                await session.close()
        async with self.pool.connection() as conn:
            await conn.execute(
                "UPDATE whatsapp_call_events SET state=%s,payload=payload-'session' WHERE id=%s",
                (state, row["id"]),
            )
        return True

    async def outbound(self):
        async with self.pool.connection() as conn, conn.transaction():
            row = await (
                await conn.execute(
                    "SELECT * FROM whatsapp_call_requests WHERE state='QUEUED' ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1"
                )
            ).fetchone()
            if not row:
                return False
            await conn.execute(
                "UPDATE whatsapp_call_requests SET state='SENDING',updated_at=now() WHERE id=%s", (row["id"],)
            )
            agent = await (
                await conn.execute("SELECT * FROM calling_agents WHERE id=%s", (row["agent_id"],))
            ).fetchone()
        session, state, provider_id = None, "FAILED", None
        try:
            if (
                not agent["enabled"]
                or row["configuration_hash"] != configuration_hash(agent)
                or time.time() - row["updated_at"].timestamp() > 300
                or len(self.sessions) >= 2
            ):
                raise ValueError("Test no longer eligible")
            channel, cfg = await connected(self.pool, agent)
            if not (await calling_provider.readiness(cfg, channel["phone_id"]))["ready"]:
                raise ValueError("Provider not ready")
            # Reserve a durable call identity before generating media or dialing.
            call = {"id": uuid4(), "provider_call_id": None, "caller": row["recipient"]}
            session = await self.session(agent, channel, cfg, call)
            sdp = await session.offer()
            state = "UNKNOWN"
            provider_id = await calling_provider.start_call(
                cfg, channel["phone_id"], row["recipient"], sdp, row["id"]
            )
            call["provider_call_id"] = provider_id
            async with self.pool.connection() as conn, conn.transaction():
                await conn.execute(
                    "INSERT INTO calling_calls(id,agent_id,provider_call_id,caller) VALUES(%s,%s,%s,%s)",
                    (call["id"], agent["id"], provider_id, row["recipient"]),
                )
            self.sessions[provider_id] = session
            state = "SUBMITTED"
        except calling_provider.CallingError as exc:
            state = "UNKNOWN" if exc.uncertain else "FAILED"
        except Exception:  # noqa: BLE001, S110 -- never print credentials/audio/provider response
            pass
        if session and state != "SUBMITTED":
            await session.close()  # Cancels audio tasks; terminates only when a provider ID is known.
        async with self.pool.connection() as conn:
            await conn.execute(
                "UPDATE whatsapp_call_requests SET state=%s,provider_call_id=%s,updated_at=now() WHERE id=%s AND state='SENDING'",
                (state, provider_id, row["id"]),
            )
        return True

    async def run(self):
        # Media lives in this process. A single DB session lock prevents competing
        # workers consuming its signaling. Scale through explicit media sharding later.
        async with self.pool.connection() as lock:
            got = await (await lock.execute("SELECT pg_try_advisory_lock(79342019) AS owned")).fetchone()
            await lock.commit()
            if not got["owned"]:
                raise RuntimeError("Another WhatsApp audio worker is active")
            try:
                async with self.pool.connection() as conn:
                    await conn.execute(
                        "UPDATE whatsapp_call_events SET state='UNKNOWN',payload=payload-'session' WHERE state='PROCESSING'"
                    )
                    await conn.execute(
                        "UPDATE whatsapp_call_requests SET state='UNKNOWN' WHERE state IN ('SENDING','SUBMITTED','CONNECTED')"
                    )
                    await conn.execute("UPDATE calling_calls SET state='HANDOFF' WHERE state='ACTIVE'")
                while True:
                    ready = calling_media.available()
                    async with self.pool.connection() as conn:
                        await conn.execute(
                            "INSERT INTO whatsapp_call_worker_health VALUES('voice',now(),%s) ON CONFLICT(id) DO UPDATE SET last_seen=now(),media_ready=EXCLUDED.media_ready",
                            (ready,),
                        )
                        paused = await (
                            await conn.execute(
                                "SELECT c.provider_call_id FROM calling_calls c JOIN calling_agents a ON a.id=c.agent_id WHERE c.state='ACTIVE' AND NOT a.enabled"
                            )
                        ).fetchall()
                    for call in paused:
                        if call["provider_call_id"] in self.sessions:
                            await self.sessions[call["provider_call_id"]].close()
                    if ready:
                        await self.event()
                        await self.outbound()
                    await asyncio.sleep(0.25 if ready else 2)
            finally:
                for session in tuple(self.sessions.values()):
                    await session.close()
                await lock.execute("SELECT pg_advisory_unlock(79342019)")
                await lock.commit()


async def serve():
    async with AsyncConnectionPool(
        os.environ["AI_PLATFORM_DATABASE_URL"], open=False, kwargs={"row_factory": dict_row}
    ) as pool:
        await Worker(pool).run()


if __name__ == "__main__":
    asyncio.run(serve())
