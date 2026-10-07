"""Separate bounded WhatsApp worker. No retry after uncertain model or message dispatch."""

import asyncio
import logging
import os
from datetime import UTC, datetime, timedelta

import httpx
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from . import knowledge
from .meta_worker import managed_config
from .whatsapp import audit


async def send(config, phone, customer, text):
    payload = {'messaging_product':'whatsapp','to':customer}
    if isinstance(text, dict):
        payload.update({'type':'interactive','interactive':text})
    else:
        payload.update({'type':'text','text':{'body':text,'preview_url':False}})
    if config.get('provider') == 'chakra':
        from .chakra_client import send_message
        return await send_message(config, phone, payload)
    async with httpx.AsyncClient(timeout=httpx.Timeout(12, connect=3)) as client:
        response = await client.post(
            f"https://graph.facebook.com/{config['graph_version']}/{phone}/messages",
            headers={"Authorization": "Bearer " + config["access_token"]},
            json=payload,
        )
        response.raise_for_status()
        return response.json()["messages"][0]["id"]


async def heartbeat(pool):
    from .gateway import model_configuration

    async with pool.connection() as conn:
        await conn.execute(
            """INSERT INTO whatsapp_worker_health(id,last_seen,model_configured) VALUES('worker',now(),%s)
            ON CONFLICT(id) DO UPDATE SET last_seen=now(),model_configured=EXCLUDED.model_configured""",
            (model_configuration()["configured"],),
        )


async def tick(pool, model=None, sender=send):
    async with pool.connection() as conn, conn.transaction():
        # Expired dispatches are quarantined, never put back on the queue.
        await conn.execute("""UPDATE whatsapp_messages SET status='UNKNOWN',updated_at=now()
            WHERE status IN ('GENERATING','SENDING') AND updated_at<now()-interval '2 minutes'""")
        job = await (
            await conn.execute("""SELECT m.*, c.channel_id FROM whatsapp_messages m
            JOIN whatsapp_conversations c ON c.id=m.conversation_id
            WHERE m.status='PENDING' AND NOT EXISTS (SELECT 1 FROM whatsapp_messages busy
                WHERE busy.conversation_id=c.id AND busy.status IN ('GENERATING','READY','SENDING','UNKNOWN'))
            ORDER BY m.received_at,m.id FOR UPDATE OF c,m SKIP LOCKED LIMIT 1""")
        ).fetchone()
        if job:
            await conn.execute(
                "UPDATE whatsapp_messages SET status='GENERATING',updated_at=now() WHERE id=%s", (job["id"],)
            )
    if job:
        try:
            async with pool.connection() as conn:
                channel = await (
                    await conn.execute("SELECT * FROM whatsapp_channels WHERE id=%s", (job["channel_id"],))
                ).fetchone()
                conversation = await (
                    await conn.execute(
                        "SELECT * FROM whatsapp_conversations WHERE id=%s", (job["conversation_id"],)
                    )
                ).fetchone()
            if (
                not channel["enabled"]
                or conversation["mode"] != "AUTO"
                or job["received_at"] < datetime.now(UTC) - timedelta(hours=23, minutes=55)
            ):
                async with pool.connection() as conn:
                    await conn.execute(
                        "UPDATE whatsapp_messages SET status='CANCELLED' WHERE id=%s AND status='GENERATING'",
                        (job["id"],),
                    )
                return True
            from .channel_trials import reserve_committed
            if not await reserve_committed(pool, channel['app_id'], 'whatsapp_turns', job['id']):
                async with pool.connection() as conn:
                    await conn.execute("UPDATE whatsapp_messages SET status='CANCELLED' WHERE id=%s", (job['id'],))
                    await audit(conn, channel['id'], job['conversation_id'], 'trial:blocked', 'worker')
                return True
            clinic_mode = channel.get('execution_mode') == 'SAHAYAK'
            bridge_handoff = False
            if clinic_mode:
                from .sahayak_bridge import request as clinic_request
                from .voice_handoffs import issue, public_origin
                async with asyncio.timeout(45):
                    clinical = await clinic_request(channel, job['id'], conversation['customer'], job['body'], pool=pool, voice_available=bool(public_origin()))
                reply = '\n\n'.join(clinical['replies'])
                bridge_handoff = clinical['handledBy'] == 'human' or clinical['redFlag']
                if clinical.get('voice_requested') is True and not bridge_handoff:
                    link = await issue(pool, channel, conversation, job)
                    reply += '\n\n' + link
                    clinical.pop('options', None)
                handoff = not reply
                from .whatsapp_interactive import interactive_reply
                controls = interactive_reply(reply, clinical.get('options')) if not bridge_handoff else None
                result = {'source': 'sahayak', 'bridge_handoff': bridge_handoff, 'citations': [], 'interactive': controls}
            else:
                async with asyncio.timeout(45):
                    result = await knowledge.answer(
                        pool,
                        model or knowledge.gateway(),
                        channel["app_id"],
                        channel["id"],
                        job["body"],
                        "whatsapp:" + str(job["id"]),
                        documents=channel["documents"],
                    )
                handoff = not result["claims"] or bool(result["missing"])
                reply = (
                    "A person needs to help with this question."
                    if handoff
                    else "\n\n".join(c["text"] for c in result["claims"])
                )
            async with pool.connection() as conn, conn.transaction():
                await conn.execute(
                    "SELECT id FROM whatsapp_conversations WHERE id=%s FOR UPDATE", (job["conversation_id"],)
                )
                updated = await (
                    await conn.execute(
                        """UPDATE whatsapp_messages SET reply=%s,evidence=%s,status=%s,updated_at=now()
                    WHERE id=%s AND status='GENERATING' RETURNING id""",
                        (reply[:4000], Jsonb(result), "HANDOFF" if handoff else "READY", job["id"]),
                    )
                ).fetchone()
                if not updated:
                    return True
                if handoff:
                    await conn.execute(
                        "UPDATE whatsapp_conversations SET mode='HUMAN' WHERE id=%s",
                        (job["conversation_id"],),
                    )
                await audit(
                    conn,
                    channel["id"],
                    job["conversation_id"],
                    "answer:handoff" if handoff else "answer:ready",
                    "worker",
                )
        except Exception:  # noqa: BLE001 -- quarantine uncertain provider outcomes
            async with pool.connection() as conn:
                await conn.execute(
                    "UPDATE whatsapp_messages SET status='UNKNOWN',updated_at=now() WHERE id=%s AND status='GENERATING'",
                    (job["id"],),
                )
        return True

    async with pool.connection() as conn:
        candidate = await (
            await conn.execute("""SELECT m.id,c.channel_id,m.conversation_id FROM whatsapp_messages m
            JOIN whatsapp_conversations c ON c.id=m.conversation_id WHERE m.status='READY'
            ORDER BY m.received_at,m.id LIMIT 1""")
        ).fetchone()
    if not candidate:
        return False
    # Hold channel/conversation locks through bounded dispatch: takeover acknowledgement means
    # no later automatic send can begin. Already dispatched messages cannot be recalled.
    async with pool.connection() as conn, conn.transaction():
        channel = await (
            await conn.execute(
                "SELECT * FROM whatsapp_channels WHERE id=%s FOR UPDATE", (candidate["channel_id"],)
            )
        ).fetchone()
        conversation = await (
            await conn.execute(
                "SELECT * FROM whatsapp_conversations WHERE id=%s FOR UPDATE", (candidate["conversation_id"],)
            )
        ).fetchone()
        msg = await (
            await conn.execute("SELECT * FROM whatsapp_messages WHERE id=%s FOR UPDATE", (candidate["id"],))
        ).fetchone()
        if msg["status"] != "READY":
            return True
        if (
            not channel["webhook_verified_at"]
            or (not channel["enabled"] and not msg["human_reply"])
            or (conversation["mode"] != "AUTO" and not msg["human_reply"])
            or msg["received_at"] < datetime.now(UTC) - timedelta(hours=23, minutes=55)
        ):
            await conn.execute("UPDATE whatsapp_messages SET status='CANCELLED' WHERE id=%s", (msg["id"],))
            return True
        # Recheck publication/version immediately before dispatch; hold read locks until dispatched.
        for citation in (msg["evidence"] or {}).get("citations", []):
            if 'memory_id' in citation:
                from .agent_memory import current_source
                permitted = await current_source(conn, citation, channel['app_id'], channel['id'], shared_only=True)
            else:
                permitted = await (
                    await conn.execute(
                        """SELECT id FROM knowledge_documents WHERE id=%s AND app_id=%s
                    AND visibility='app' AND version=%s AND id=ANY(%s) FOR SHARE""",
                        (citation["document_id"], channel["app_id"], citation["version"], channel["documents"]),
                    )
                ).fetchone()
            if not permitted:
                await conn.execute("UPDATE whatsapp_messages SET status='HANDOFF' WHERE id=%s", (msg["id"],))
                return True
        from .channel_trials import reserve_committed
        if not await reserve_committed(pool, channel['app_id'], 'whatsapp_sends', msg['id']):
            await conn.execute("UPDATE whatsapp_messages SET status='CANCELLED' WHERE id=%s", (msg['id'],))
            await audit(conn, channel['id'], conversation['id'], 'trial:blocked', 'worker')
            return True
        config = await managed_config(pool, channel["phone_id"], channel["app_id"], channel["owner_id"])
        # Independent committed dispatch fence survives a crash while the send lock is held.
        await conn.execute(
            "UPDATE whatsapp_messages SET status='SENDING',updated_at=now() WHERE id=%s", (msg["id"],)
        )
        # The fence has no FK, so insertion does not wait on the message row lock.
        async with pool.connection() as fence:
            inserted = await (
                await fence.execute(
                    "INSERT INTO whatsapp_dispatches(message_id) VALUES(%s) ON CONFLICT DO NOTHING RETURNING message_id",
                    (msg["id"],),
                )
            ).fetchone()
        if not inserted:
            await conn.execute("UPDATE whatsapp_messages SET status='UNKNOWN' WHERE id=%s", (msg["id"],))
            return True
        try:
            async with asyncio.timeout(15):
                content = (msg['evidence'] or {}).get('interactive') if not msg['human_reply'] else None
                outbound = await sender(config, channel["phone_id"], conversation["customer"], content or msg["reply"])
            if not isinstance(outbound, str) or not 1 <= len(outbound) <= 300:
                raise ValueError("Invalid provider response")
            await conn.execute(
                "UPDATE whatsapp_messages SET status='SENT',outbound_id=%s,updated_at=now() WHERE id=%s",
                (outbound, msg["id"]),
            )
            if (msg['evidence'] or {}).get('bridge_handoff'):
                await conn.execute("UPDATE whatsapp_conversations SET mode='HUMAN' WHERE id=%s", (conversation['id'],))
            await audit(conn, channel["id"], conversation["id"], "reply:sent", "worker")
        except Exception:  # noqa: BLE001 -- never repeat an uncertain send
            await conn.execute(
                "UPDATE whatsapp_messages SET status='UNKNOWN',updated_at=now() WHERE id=%s", (msg["id"],)
            )
            await audit(conn, channel["id"], conversation["id"], "reply:unknown", "worker")
    return True


async def serve():
    async with AsyncConnectionPool(
        os.environ["AI_PLATFORM_DATABASE_URL"],
        min_size=1,
        max_size=4,
        kwargs={"row_factory": dict_row},
        open=False,
    ) as pool:
        while True:
            try:
                await heartbeat(pool)
                from .whatsapp_outbound import tick_outbound
                await tick_outbound(pool)
                from .whatsapp_alerts import tick_alerts
                if await tick_alerts(pool):
                    continue
                if await tick(pool):
                    continue
            except Exception:  # noqa: BLE001 -- never repeat an uncertain send
                # A database/configuration outage leaves durable records for the next cycle.
                logging.getLogger(__name__).warning("WhatsApp worker cycle unavailable")
            await asyncio.sleep(2)


if __name__ == "__main__":
    asyncio.run(serve())
