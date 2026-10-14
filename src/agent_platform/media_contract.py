"""Versioned private control/media HMAC contract. No browser credentials or arbitrary URLs."""
import hashlib
import hmac
import json
import os
import time
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field

AUDIENCE = 'agent-platform-media-v1'
MAX_BODY = 70000


class SessionClaim(BaseModel):
    model_config = ConfigDict(extra='forbid')
    session_id: UUID
    app_id: UUID
    owner_id: UUID
    sdp: str = Field(min_length=20, max_length=65536)


def key():
    value = os.environ.get('AI_VOICE_MEDIA_SECRET', '')
    if len(value) < 48:
        raise ValueError('Configure a dedicated media secret of at least 48 characters')
    return value.encode()


def origin():
    value = os.environ.get('AI_VOICE_MEDIA_URL', '').rstrip('/')
    parsed = urlsplit(value)
    if parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ('', '/'):
        raise ValueError('Invalid media origin')
    if not parsed.hostname or not (parsed.scheme == 'https' or (parsed.scheme == 'http' and parsed.hostname in {'127.0.0.1', '::1', 'localhost'})):
        raise ValueError('Media origin needs HTTPS or explicit loopback HTTP')
    return value


def material(method, path, stamp, nonce, body):
    return '\n'.join([AUDIENCE, method.upper(), path, stamp, nonce, hashlib.sha256(body).hexdigest()]).encode()


def headers(method, path, body):
    stamp, nonce = str(int(time.time())), str(uuid4())
    signature = hmac.new(key(), material(method, path, stamp, nonce, body), hashlib.sha256).hexdigest()
    return {'Content-Type':'application/json','X-Media-Time':stamp,'X-Media-Nonce':nonce,'X-Media-Signature':signature}


async def authenticate(request, pool):
    # Read incrementally: reject oversized authenticated or unauthenticated bodies.
    chunks = bytearray()
    async for part in request.stream():
        chunks.extend(part)
        if len(chunks) > MAX_BODY:
            raise HTTPException(413, 'Media request too large')
    body = bytes(chunks)
    stamp, nonce = request.headers.get('X-Media-Time',''), request.headers.get('X-Media-Nonce','')
    try:
        if abs(time.time()-int(stamp)) > 30 or str(UUID(nonce)) != nonce:
            raise ValueError()
        expected = hmac.new(key(), material(request.method, request.url.path, stamp, nonce, body), hashlib.sha256).hexdigest()
        if request.url.query or not hmac.compare_digest(expected, request.headers.get('X-Media-Signature','')):
            raise ValueError()
    except (ValueError, TypeError):
        raise HTTPException(401, 'Invalid media authorization') from None
    async with pool.connection() as conn, conn.transaction():
        await conn.execute('DELETE FROM voice_media_nonces WHERE expires_at<now()')
        accepted = await (await conn.execute("INSERT INTO voice_media_nonces(nonce,expires_at) VALUES(%s,now()+interval '61 seconds') ON CONFLICT DO NOTHING RETURNING nonce", (nonce,))).fetchone()
        if not accepted:
            raise HTTPException(409, 'Media request already consumed')
    try:
        return json.loads(body) if body else {}
    except ValueError:
        raise HTTPException(422, 'Invalid media request') from None
