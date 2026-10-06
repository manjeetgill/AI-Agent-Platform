"""Private Asterisk AudioSocket pilot. One app-owned connection (or legacy pilot agent) per bridge.

No telephone origination, caller-ID selection or recording. Business tools run only
through the platform permission gate in connection-bound mode.
The public API and other platform workers do not depend on this optional process.
"""

import asyncio
import contextlib
import io
import json
import logging
import math
import os
import sqlite3
import ssl
import struct
import time
import wave
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID

import httpx

from . import calling_audio

LOG = logging.getLogger(__name__)


def config(path):
    value = json.loads(Path(path).read_text())
    if value.get("mode") not in {"diagnostic", "agent"}:
        raise ValueError("Choose diagnostic or agent mode")
    if value.get("tts_provider", "gemini") not in {"gemini", "local"}:
        raise ValueError("Choose gemini or local speech output")
    if value["mode"] == "agent":
        url = urlsplit(value.get("platform_url", ""))
        if (
            url.scheme != "https"
            or not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
            or url.path not in {"", "/"}
        ):
            raise ValueError("Platform URL must be an HTTPS origin")
        if value.get("connection_id"):
            UUID(value["connection_id"])
            if not value.get("gateway_key", "").startswith("tgw_") or len(value["gateway_key"]) < 40:
                raise ValueError("Configure a private connection-bound gateway key")
            import re

            if not re.fullmatch(r"\+[1-9][0-9]{7,14}", value.get("number", "")):
                raise ValueError("Configure the connection's authorized number")
            return value
        UUID(value["agent_id"])
        if not value.get("external_user_id") or len(value["external_user_id"]) > 200:
            raise ValueError("Set the owning external user ID")
        if len(value.get("service_key", "")) < 32 or not value.get("gemini_key"):
            raise ValueError("Configure private platform and speech credentials")
    return value


class Journal:
    """Reserve before media/model work. Crashes are UNKNOWN, never auto-redialed."""

    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.execute("CREATE TABLE IF NOT EXISTS calls (id TEXT PRIMARY KEY, started REAL, state TEXT)")
        self.db.execute("UPDATE calls SET state='UNKNOWN' WHERE state='ACTIVE'")
        self.db.commit()

    def begin(self, ident):
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            recent = self.db.execute(
                "SELECT count(*) FROM calls WHERE started>?", (time.time() - 86400,)
            ).fetchone()[0]
            active = self.db.execute("SELECT count(*) FROM calls WHERE state='ACTIVE'").fetchone()[0]
            if recent >= 30 or active >= 2:
                raise ValueError("Pilot call limit reached")
            self.db.execute("INSERT INTO calls VALUES(?,?,?)", (str(ident), time.time(), "ACTIVE"))

    def end(self, ident, state):
        with self.db:
            self.db.execute("UPDATE calls SET state=? WHERE id=?", (state, str(ident)))


def packet(kind, payload=b""):
    return struct.pack("!BH", kind, len(payload)) + payload


async def read_packet(reader):
    kind, size = struct.unpack("!BH", await asyncio.wait_for(reader.readexactly(3), 5))
    # No oversized allocations, alternative audio formats or instruction messages.
    if (kind == 0x01 and size != 16) or (kind == 0x10 and (not size or size > 3200 or size % 2)):
        raise ValueError("Malformed AudioSocket packet")
    if kind not in {0, 1, 3, 0x10, 0xFF} or (kind in {0, 3, 0xFF} and size > 1):
        raise ValueError("Unsupported AudioSocket packet")
    return kind, await asyncio.wait_for(reader.readexactly(size), 5)


def resample(pcm, source, target):
    import av

    if not pcm or len(pcm) % 2 or source not in {8000, 16000, 22050, 24000, 44100, 48000}:
        raise ValueError("Unsupported PCM")
    frame = av.AudioFrame(format="s16", layout="mono", samples=len(pcm) // 2)
    frame.planes[0].update(pcm)
    frame.sample_rate = source
    convert = av.AudioResampler(format="s16", layout="mono", rate=target)
    return b"".join(
        bytes(f.planes[0])[: f.samples * 2] for f in convert.resample(frame) + convert.resample(None)
    )


def tone():
    return b"".join(
        struct.pack("<h", int(3000 * math.sin(2 * math.pi * 440 * i / 8000))) for i in range(1600)
    )


class Agent:
    def __init__(self, settings):
        self.settings = settings
        context = ssl.create_default_context(cafile=settings.get("platform_ca"))
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(22, connect=3), follow_redirects=False, verify=context
        )

    async def gateway_request(self, method, path, payload=None):
        cfg = self.settings
        response = await self.client.request(
            method,
            cfg["platform_url"].rstrip("/") + "/v1/telephony/gateway/" + cfg["connection_id"] + path,
            headers={"Authorization": "Bearer " + cfg["gateway_key"]},
            json=payload,
        )
        response.raise_for_status()
        if len(response.content) > 16384:
            raise ValueError("Gateway response too large")
        return response.json()

    async def begin(self, call_id):
        if self.settings.get("connection_id"):
            return await self.gateway_request(
                "POST", "/calls", {"call_id": str(call_id), "number": self.settings["number"]}
            )

    async def end(self, call_id, state):
        if self.settings.get("connection_id"):
            await self.gateway_request("POST", f"/calls/{call_id}/end", {"state": state})

    async def speech(self, call_id, turn, pcm=b""):
        cfg = self.settings
        async with self.client.stream(
            "POST",
            cfg["platform_url"].rstrip("/")
            + f"/v1/telephony/gateway/{cfg['connection_id']}/calls/{call_id}/speech/{turn}",
            headers={
                "Authorization": "Bearer " + cfg["gateway_key"],
                "Content-Type": "application/octet-stream",
            },
            content=pcm,
            timeout=httpx.Timeout(65, connect=3),
        ) as response:
            response.raise_for_status()
            raw = bytearray()
            async for chunk in response.aiter_bytes():
                raw.extend(chunk)
                if len(raw) > 4_400_000:
                    raise ValueError("Speech response too large")
            action = response.headers.get("X-Voice-Action")
        if action not in {"continue", "end", "handoff"}:
            raise ValueError("Invalid speech action")
        with wave.open(io.BytesIO(raw), "rb") as wav:
            if wav.getnchannels() != 1 or wav.getsampwidth() != 2:
                raise ValueError("Unsupported platform speech")
            rate = wav.getframerate()
            data = wav.readframes(wav.getnframes())
        return resample(data, rate, 8000), action

    async def heartbeat(self):
        while True:
            try:
                await self.gateway_request("POST", "/heartbeat")
            except httpx.HTTPError:
                LOG.warning("asterisk_gateway_heartbeat_failed")
            await asyncio.sleep(30)

    async def reply(self, call_id, turn, text, history):
        if self.settings.get("connection_id"):
            result = await self.gateway_request("POST", f"/calls/{call_id}/turns/{turn}", {"text": text})
            return self.validate_reply(result, business_tools=True)
        cfg = self.settings
        response = await self.client.post(
            cfg["platform_url"].rstrip("/") + "/v1/voice/agents/" + cfg["agent_id"] + "/preview",
            headers={
                "Authorization": "Bearer " + cfg["service_key"],
                "X-External-User-ID": cfg["external_user_id"],
            },
            json={"text": text, "history": history[-8:], "request_key": f"asterisk:{call_id}:{turn}"},
        )
        response.raise_for_status()
        if len(response.content) > 16384:
            raise ValueError("Agent response too large")
        result = response.json()
        return self.validate_reply(result)

    @staticmethod
    def validate_reply(result, business_tools=False):
        if (
            result.get("action") not in {"continue", "end", "handoff"}
            or not isinstance(result.get("text"), str)
            or not 1 <= len(result["text"]) <= 1200
            or (
                result.get("actions_executed") is not False
                and not (business_tools and result.get("actions_executed") is True)
            )
        ):
            raise ValueError("Invalid voice response")
        return result


class Call:
    def __init__(self, reader, writer, ident, agent, diagnostic=False, greeting=None):
        self.reader, self.writer, self.ident, self.agent = reader, writer, ident, agent
        self.diagnostic, self.busy = diagnostic, True
        self.queue = asyncio.Queue(maxsize=1)
        self.history = []
        self.greeting = greeting or "How can I help you?"

    async def send_audio(self, pcm):
        start = time.monotonic()
        for offset in range(0, len(pcm), 320):
            await asyncio.sleep(max(0, start + offset / 16000 - time.monotonic()))
            self.writer.write(packet(0x10, pcm[offset : offset + 320].ljust(320, b"\0")))
            await asyncio.wait_for(self.writer.drain(), 2)

    async def say(self, text):
        if self.agent.settings.get("tts_provider", "gemini") == "local":
            pcm, rate = await calling_audio.local_speak(text)
        else:
            pcm, rate = await calling_audio.speak(text)
        if len(pcm) > rate * 2 * 40:
            raise ValueError("Speech too long")
        await self.send_audio(resample(pcm, rate, 8000))

    async def receive(self):
        chunks, quiet = bytearray(), 0
        while True:
            try:
                kind, raw = await read_packet(self.reader)
            except asyncio.IncompleteReadError as exc:
                if exc.partial:
                    raise
                return  # Asterisk may close TCP on a normal SIP BYE.
            if kind in {0, 0xFF} or (kind == 3 and raw == b"#"):
                return
            if kind == 3:
                continue
            if kind != 0x10:
                raise ValueError("Unexpected session packet")
            if self.busy:
                chunks.clear()
                quiet = 0
                continue
            samples = struct.unpack("<" + "h" * (len(raw) // 2), raw)
            loud = sum(abs(x) for x in samples) / len(samples) > 300
            if loud or chunks:
                remaining = 192000 - len(chunks)
                chunks.extend(raw[:remaining])
                quiet = 0 if loud else quiet + len(raw)
            if len(chunks) >= 192000 or (chunks and quiet >= 12800):
                self.busy = True
                self.queue.put_nowait(bytes(chunks))
                chunks.clear()
                quiet = 0

    async def converse(self):
        if self.diagnostic:
            await self.send_audio(tone())
        elif self.agent.settings.get("connection_id"):
            pcm, _ = await self.agent.speech(self.ident, 0)
            await self.send_audio(pcm)
        else:
            await self.say(
                "Hello. I am an AI assistant. Your speech is processed by our AI service. "
                "Press hash to end this call. " + self.greeting
            )
        self.busy = False
        turn = 0
        for _ in range(15):
            pcm = await self.queue.get()
            if self.diagnostic:
                await self.send_audio(tone())
            elif self.agent.settings.get("connection_id"):
                turn += 1
                output, action = await self.agent.speech(self.ident, turn, resample(pcm, 8000, 16000))
                await self.send_audio(output)
                if action != "continue":
                    return
            else:
                text = await calling_audio.transcribe(resample(pcm, 8000, 16000))
                if text.strip():
                    turn += 1
                    result = await self.agent.reply(self.ident, turn, text, self.history)
                    self.history = (
                        self.history
                        + [{"role": "user", "text": text}, {"role": "assistant", "text": result["text"]}]
                    )[-8:]
                    await self.say(result["text"])
                    if result["action"] != "continue":
                        return
            self.busy = False

    async def run(self):
        tasks = [asyncio.create_task(self.receive()), asyncio.create_task(self.converse())]
        try:
            async with asyncio.timeout(120):
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    task.result()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            self.history.clear()


async def serve(settings, host="0.0.0.0", port=9092):
    journal = Journal(os.environ.get("AI_ASTERISK_JOURNAL", "/state/calls.sqlite3"))
    agent = Agent(settings)
    if settings["mode"] == "agent" and not settings.get("connection_id"):
        os.environ["AI_CALLING_AUDIO_API_KEY"] = settings["gemini_key"]
    connections = set()

    async def handle(reader, writer):
        # Bound even pre-handshake sockets; UUID is a session ID, not authentication.
        task = asyncio.current_task()
        if len(connections) >= 2:
            writer.close()
            await writer.wait_closed()
            return
        connections.add(task)
        ident, state, reserved, admitted = None, "FAILED", False, False
        try:
            kind, payload = await read_packet(reader)
            if kind != 1:
                raise ValueError("UUID must be first")
            ident = UUID(bytes=payload)
            journal.begin(ident)
            reserved = True
            metadata = None
            if settings["mode"] == "agent":
                metadata = await agent.begin(ident)
                admitted = True
            LOG.info("asterisk_call_started id=%s mode=%s", ident, settings["mode"])
            await Call(
                reader,
                writer,
                ident,
                agent,
                settings["mode"] == "diagnostic",
                (metadata or {}).get("greeting"),
            ).run()
            state = "ENDED"
        except asyncio.CancelledError:
            state = "UNKNOWN"
            raise
        except Exception as exc:  # noqa: BLE001 -- never log provider bodies or credentials
            LOG.warning(
                "asterisk_call_failed type=%s status=%s",
                type(exc).__name__,
                getattr(getattr(exc, "response", None), "status_code", None),
            )
        finally:
            if reserved:
                if admitted:
                    with contextlib.suppress(Exception):
                        await agent.end(ident, state)
                journal.end(ident, state)
                LOG.info("asterisk_call_finished id=%s state=%s", ident, state)
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
            connections.discard(task)

    heartbeat = asyncio.create_task(agent.heartbeat()) if settings.get("connection_id") else None
    server = await asyncio.start_server(handle, host, port, limit=8192)
    LOG.info("asterisk_bridge_ready mode=%s telephone_connected=false", settings["mode"])
    try:
        async with server:
            await server.serve_forever()
    finally:
        for task in tuple(connections):
            task.cancel()
        await asyncio.gather(*connections, return_exceptions=True)
        if heartbeat:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
        await agent.client.aclose()
        journal.db.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    asyncio.run(serve(config(os.environ["AI_ASTERISK_CONFIG"])))
