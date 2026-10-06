"""Durable voice turns using the platform's existing RAG or an app-owned action handler."""

import hashlib
import json
import os
from urllib.parse import urlsplit
from uuid import uuid4

from psycopg.types.json import Jsonb

from . import knowledge
from .calling import invoke


def handler(agent):
    cfg = json.loads(os.environ.get("AI_CALLING_APP_HANDLERS", "{}")).get(str(agent["id"]))
    if not isinstance(cfg, dict) or cfg.get("app_id") != str(agent["app_id"]):
        raise ValueError("App voice handler not configured")
    url = urlsplit(cfg.get("handler_url", ""))
    if (
        url.scheme != "https"
        or not url.hostname
        or url.username
        or url.password
        or url.fragment
        or len(cfg.get("handler_secret", "")) < 32
    ):
        raise ValueError("Invalid app voice handler")
    return cfg


async def respond(pool, agent, channel, call, text):
    fallback = {"action": "handoff", "text": agent["handoff_message"]}
    if not isinstance(text, str) or not text.strip() or len(text) > 1500:
        return fallback
    event = uuid4()
    digest = hashlib.sha256(text.encode()).hexdigest()
    async with pool.connection() as conn, conn.transaction():
        current = await (
            await conn.execute(
                "SELECT * FROM calling_calls WHERE id=%s AND agent_id=%s FOR UPDATE",
                (call["id"], agent["id"]),
            )
        ).fetchone()
        active = await (
            await conn.execute("SELECT enabled FROM calling_agents WHERE id=%s", (agent["id"],))
        ).fetchone()
        if (
            not current
            or not active["enabled"]
            or current["state"] != "ACTIVE"
            or current["last_turn"] >= agent["max_turns"]
        ):
            return fallback
        if await (
            await conn.execute(
                "SELECT 1 FROM calling_turns WHERE call_id=%s AND state IN ('PROCESSING','UNKNOWN')",
                (call["id"],),
            )
        ).fetchone():
            return fallback
        turn = current["last_turn"] + 1
        await conn.execute(
            "INSERT INTO calling_turns(id,call_id,turn,request_hash,state) VALUES(%s,%s,%s,%s,'PROCESSING')",
            (event, call["id"], turn, digest),
        )
        await conn.execute(
            "UPDATE calling_calls SET last_turn=%s,updated_at=now() WHERE id=%s", (turn, call["id"])
        )
    try:
        if channel["execution_mode"] == "SAHAYAK" or str(agent["id"]) in json.loads(
            os.environ.get("AI_CALLING_APP_HANDLERS", "{}")
        ):
            result = await invoke(
                handler(agent),
                {
                    "probe": False,
                    "event_id": str(event),
                    "call_id": str(call["id"]),
                    "agent_id": str(agent["id"]),
                    "app_id": str(agent["app_id"]),
                    "caller": call["caller"],
                    "input": text,
                    "input_trust": "untrusted_caller_data",
                    "turn": turn,
                    "language": agent["language"],
                },
            )
            if result.get("event_id") != str(event):
                raise ValueError()
            answer = {"action": result["action"], "text": result["text"]}
        else:
            result = await knowledge.answer(
                pool,
                knowledge.gateway(),
                channel["app_id"],
                channel["id"],
                text,
                "voice:" + str(event),
                documents=channel["documents"],
            )
            answer = (
                fallback
                if not result["claims"] or result["missing"]
                else {"action": "continue", "text": " ".join(c["text"] for c in result["claims"])[:1200]}
            )
        if (
            answer["action"] not in {"continue", "end", "handoff"}
            or not isinstance(answer["text"], str)
            or not 1 <= len(answer["text"]) <= 1200
        ):
            raise ValueError()
        state = "DONE"
    except Exception:  # noqa: BLE001 -- unknown business outcomes must not be replayed or logged
        answer, state = fallback, "UNKNOWN"
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE calling_turns SET state=%s,reply=%s WHERE id=%s", (state, Jsonb(answer), event)
        )
    return answer
