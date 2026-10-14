"""Ordered, content-free call telemetry; transcripts require opt-in and expire separately."""
import os

from psycopg.types.json import Jsonb

KINDS = {'connecting', 'connected', 'ended', 'failed', 'interrupted', 'asr', 'asr_partial', 'reasoning', 'tts', 'user', 'assistant', 'turn_failed', 'streamed_claim', 'specialist_handoff'}


async def record(pool, session, kind, details=None, text=None):
    if kind not in KINDS:
        raise ValueError('Invalid voice event')
    # Explicit primitive fields only: no upstream errors, tokens, audio, SDP or tool arguments.
    allowed = {'duration_ms', 'provider', 'model', 'outcome', 'version', 'characters', 'stage', 'target_agent_id', 'hop'}
    details = {k: v for k, v in (details or {}).items() if k in allowed and isinstance(v, (str, int, float, bool))}
    async with pool.connection() as conn, conn.transaction():
        row = await (await conn.execute('UPDATE web_voice_sessions SET event_sequence=event_sequence+1 WHERE id=%s RETURNING event_sequence,transcript_consent', (session,))).fetchone()
        if not row:
            return
        await conn.execute('INSERT INTO web_voice_events(session_id,sequence,kind,details) VALUES(%s,%s,%s,%s)', (session, row['event_sequence'], kind, Jsonb(details)))
        if row['transcript_consent'] and text and kind in {'user', 'assistant', 'asr_partial'}:
            await conn.execute('INSERT INTO web_voice_transcripts(session_id,sequence,role,content) VALUES(%s,%s,%s,%s)', (session, row['event_sequence'], 'user' if kind == 'asr_partial' else kind, text[:1500]))


async def expire(pool):
    async with pool.connection() as conn:
        await conn.execute('DELETE FROM web_voice_transcripts WHERE expires_at<=now()')
        days = max(1, min(365, int(os.environ.get('AI_VOICE_EVENT_RETENTION_DAYS', '30'))))
        await conn.execute("DELETE FROM web_voice_events WHERE created_at<now()-(%s * interval '1 day')", (days,))
