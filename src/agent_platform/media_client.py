"""Bounded control-plane client. Never retries an uncertain media dispatch."""
import json
import os

import httpx
from fastapi import HTTPException

from . import media_contract


def enabled():
    return bool(os.environ.get('AI_VOICE_MEDIA_URL'))


def configured():
    try:
        media_contract.key()
        media_contract.origin()
        return True
    except ValueError:
        return False


async def send(path, payload):
    raw = json.dumps(payload, separators=(',',':'), default=str).encode()
    socket = os.environ.get('AI_VOICE_MEDIA_SOCKET')
    if socket and (not socket.startswith('/') or media_contract.origin() != 'http://localhost'):
        raise ValueError('Unix media socket requires an absolute path and http://localhost origin')
    transport = httpx.AsyncHTTPTransport(uds=socket) if socket else None
    async with httpx.AsyncClient(timeout=httpx.Timeout(23, connect=2), follow_redirects=False, transport=transport) as client:
        response = await client.post(media_contract.origin()+path, content=raw,
                                     headers=media_contract.headers('POST', path, raw))
        response.raise_for_status()
        if len(response.content)>70000:
            raise ValueError('Media response too large')
        return response.json()


async def check_ready():
    try:
        result = await send('/internal/v1/readiness', {})
        if result.get('status') != 'ready':
            raise ValueError()
    except (ValueError, httpx.HTTPError):
        raise HTTPException(503, 'Voice media service unavailable; other platform features remain available') from None


async def start(call, sdp):
    result = await send('/internal/v1/sessions', {'session_id':call['id'], 'app_id':call['app_id'], 'owner_id':call['owner_id'], 'sdp':sdp})
    if str(result.get('id')) != str(call['id']) or result.get('type') != 'answer' or not isinstance(result.get('sdp'),str) or len(result['sdp'])>65536:
        raise ValueError('Invalid media response')
    return result
