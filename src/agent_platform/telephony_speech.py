"""Platform-owned, bounded speech I/O for authorized telephone sessions.

Gateways send PCM, receive WAV, and never receive model credentials. Reserve each
speech turn before provider access. Local recognition uses private temporary
audio deleted after each turn; no raw audio is kept in call records.
"""

import asyncio
import hashlib
import io
import os
import wave
from datetime import UTC, datetime
from uuid import UUID

from fastapi import HTTPException, Request, Response

from . import calling_audio


def wav_bytes(pcm, rate):
    if (
        rate not in (8000, 16000, 22050, 24000, 44100, 48000)
        or not pcm
        or len(pcm) % 2
        or len(pcm) > rate * 2 * 45
    ):
        raise ValueError("Unsupported speech audio")
    out = io.BytesIO()
    with wave.open(out, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(pcm)
    return out.getvalue()


async def synthesize(text):
    if os.environ.get("AI_TELEPHONY_TTS_PROVIDER", "gemini") == "local":
        return await calling_audio.local_speak(text)
    return await calling_audio.speak(text)


def install_speech(router, gateway, session, checks, text_reply, Turn):
    async def process(ident, call_id, turn, request):
        if not 0 <= turn <= 15:
            raise HTTPException(422, "Invalid speech turn")
        pool = request.app.state.repository.pool
        # Authenticate before reading potentially large audio bodies.
        async with pool.connection() as conn:
            row = await gateway(conn, ident, request)
            call = await session(conn, ident, call_id)
            blockers, agent = await checks(conn, row)
            if (
                not row["enabled"]
                or blockers
                or call["state"] != "ACTIVE"
                or call["expires_at"] <= datetime.now(UTC)
            ):
                raise HTTPException(409, "Call is not active")
        audio = bytearray()
        if turn:
            if request.headers.get("content-type", "").split(";")[0] != "application/octet-stream":
                raise HTTPException(415, "Send mono signed 16-bit little-endian PCM at 16000 Hz")
            async for chunk in request.stream():
                audio.extend(chunk)
                if len(audio) > 384000:
                    raise HTTPException(413, "Speech exceeds 12 seconds")
            if not audio or len(audio) % 2:
                raise HTTPException(422, "Invalid PCM")
        async with pool.connection() as conn, conn.transaction():
            await gateway(conn, ident, request)
            current = await session(conn, ident, call_id)
            if current["state"] != "ACTIVE" or current["expires_at"] <= datetime.now(UTC):
                raise HTTPException(409, "Call ended")
            if turn and turn != current["turns"] + 1:
                raise HTTPException(409, "Speech turns must be in order")
            existing = await (
                await conn.execute(
                    "SELECT 1 FROM telephony_speech_usage WHERE session_id=%s AND (turn=%s OR state='STARTED')",
                    (call["id"], turn),
                )
            ).fetchone()
            if existing:
                raise HTTPException(409, "Speech turn already attempted; do not replay")
            await conn.execute(
                "INSERT INTO telephony_speech_usage(session_id,turn,input_hash,state,input_seconds) VALUES(%s,%s,%s,'STARTED',%s)",
                (call["id"], turn, hashlib.sha256(audio).hexdigest(), len(audio) / 32000),
            )
        try:
            remaining = (current["expires_at"] - datetime.now(UTC)).total_seconds()
            async with asyncio.timeout(max(0.01, min(60, remaining))):
                if turn:
                    transcript = await calling_audio.transcribe(bytes(audio))
                    audio.clear()
                    # Empty speech still consumes its reserved turn, avoiding endless paid transcription.
                    result = await text_reply(
                        ident, call_id, turn, Turn(text=transcript.strip() or "[inaudible speech]"), request
                    )
                else:
                    notice = "I am an AI assistant. Your speech is processed to help with this call. "
                    if agent.get("language") in ("hi", "hinglish"):
                        notice = "मैं AI assistant हूँ। इस call में आपकी आवाज़ process की जाती है। "
                    result = {
                        "text": (notice + agent["greeting"])[:1200],
                        "action": "continue",
                        "actions_executed": False,
                    }
                pcm, rate = await synthesize(result["text"])
                encoded = wav_bytes(pcm, rate)
            async with pool.connection() as conn:
                latest = await gateway(conn, ident, request)
                active = await session(conn, ident, call_id)
                blockers, _ = await checks(conn, latest)
                if (
                    not latest["enabled"]
                    or blockers
                    or active["state"] != "ACTIVE"
                    or active["expires_at"] <= datetime.now(UTC)
                ):
                    raise ValueError("Call stopped while generating speech")
                await conn.execute(
                    "UPDATE telephony_speech_usage SET state='DONE',output_seconds=%s WHERE session_id=%s AND turn=%s",
                    (len(pcm) / (rate * 2), call["id"], turn),
                )
            return Response(
                encoded,
                media_type="audio/wav",
                headers={"Cache-Control": "no-store", "X-Voice-Action": result["action"]},
            )
        except BaseException as exc:
            async with pool.connection() as conn:
                await conn.execute(
                    "UPDATE telephony_speech_usage SET state='UNKNOWN' WHERE session_id=%s AND turn=%s",
                    (call["id"], turn),
                )
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise HTTPException(
                503, "Speech unavailable; inspect the saved call/action outcome before trying again"
            ) from None
        finally:
            audio.clear()

    @router.post("/gateway/{ident}/calls/{call_id}/speech/{turn}")
    async def speech(ident: UUID, call_id: UUID, turn: int, request: Request):
        return await process(ident, call_id, turn, request)
