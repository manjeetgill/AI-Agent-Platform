"""Process-owned audio runtime shared by embedded compatibility and the media service."""
import asyncio
from uuid import uuid4

from fastapi import HTTPException

from . import calling_media, voice_config, voice_events


class VoiceRuntime:
    def __init__(self):
        self.id = uuid4()
        self.sessions = {}
        self.watchers = {}

    async def close(self):
        await asyncio.gather(*(s.close() for s in tuple(self.sessions.values())), return_exceptions=True)
        for task in self.watchers.values():
            task.cancel()
        await asyncio.gather(*self.watchers.values(), return_exceptions=True)
        self.watchers.clear()

    async def watch(self, pool, ident, media):
        try:
            while not getattr(media, 'closed', False):
                async with pool.connection() as conn:
                    row = await (await conn.execute("""UPDATE web_voice_sessions s SET media_heartbeat=now()
                        FROM web_voice_agents a WHERE s.id=%s AND a.id=s.agent_id AND a.enabled
                        AND a.deleted_at IS NULL AND s.state IN ('STARTING','ACTIVE') AND s.expires_at>now()
                        RETURNING s.id""", (ident,))).fetchone()
                if not row:
                    await media.close()
                    return
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 -- fail closed when control state cannot be checked
            if hasattr(media, 'fail'):
                await media.fail('control_state')
            else:
                await media.close()
        finally:
            self.watchers.pop(str(ident), None)

    async def start(self, pool, agent, call, sdp):
        from .db_scope import APIPool, BoundPool, identity
        if isinstance(pool, APIPool):
            pool = BoundPool(pool, identity.get() or {})
        await voice_events.record(pool, call['id'], 'connecting', {'version': agent['version']})
        sessions = self.sessions
        history = []  # Per-call bounded memory; persistent transcript is separately opt-in.

        async def respond(text, on_claim=None):
            from .web_voice import dialogue
            with voice_config.using(agent.get("config")):
                reply = await dialogue(pool, agent, call, text, history, on_claim)
            if reply.pop("reset_history", False):
                history.clear()
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

            await voice_events.record(pool, call["id"], "ended")

        async def event(kind, details=None, text=None):
            if kind == 'connected':
                async with pool.connection() as conn:
                    await conn.execute('UPDATE web_voice_sessions SET media_connected=true WHERE id=%s', (call['id'],))
            if kind in {'turn_failed', 'failed'}:
                async with pool.connection() as conn:
                    await conn.execute("UPDATE web_voice_sessions SET state='FAILED',ended_at=now() WHERE id=%s AND state IN ('STARTING','ACTIVE')", (call['id'],))
            await voice_events.record(pool, call["id"], kind, details, text)

        media = None
        try:
            greeting = agent["greeting"]
            if call.get("handoff_id"):
                from .voice_handoffs import clinic_turn

                resume = await clinic_turn(
                    pool, call["handoff_id"], uuid4(), "Resume appointment conversation", resume=True
                )
                if resume["action"] != "continue":
                    raise ValueError("Conversation is handled by reception")
                greeting = resume["text"]
            media = calling_media.Session(respond, greeting, ended)
            media.meter = (pool, call["id"])
            media.supports_streaming = True
            media.profile = agent["config"]
            media.event = event
            sessions[str(call["id"])] = media
            answer = await asyncio.wait_for(media.remote(sdp, "offer"), timeout=20)
            async with pool.connection() as conn:
                await conn.execute(
                    "UPDATE web_voice_sessions SET state='ACTIVE' WHERE id=%s AND state='STARTING'",
                    (call["id"],),
                )
            self.watchers[str(call["id"])] = asyncio.create_task(self.watch(pool, call["id"], media))
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
            await voice_events.record(pool, call["id"], "failed", {"stage": "connection"})
            raise HTTPException(502, "Audio connection could not be established") from None
