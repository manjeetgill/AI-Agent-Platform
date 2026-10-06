"""Temporary, signature-checked callback relay. Never exposes the CRM UI or API."""
import hashlib
import hmac
import json
import os
import time
from pathlib import Path

import httpx
from fastapi import FastAPI, Request, Response

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


@app.post('/integrations/agent-platform/whatsapp')
async def callback(request: Request):
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > 20000:
            return Response(status_code=413)
    stamp = request.headers.get('x-agent-timestamp', '')
    signature = request.headers.get('x-agent-signature', '')
    if not stamp.isdigit() or len(stamp) != 10 or abs(time.time() - int(stamp)) > 300:
        return Response(status_code=403)
    try:
        body = json.loads(raw)
        config = json.loads(Path(os.environ['REACH_AGENT_PLATFORM_FILE']).read_text())
        org, channel, app_id = body['clinic_id'], body['channel_id'], body['app_id']
        binding = config['organizations'][org]
        if binding['app_id'] != app_id:
            return Response(status_code=403)
        secret = hmac.new(binding['secret'].encode(), f'{org}:{app_id}:{channel}'.encode(), hashlib.sha256).hexdigest()
        expected = hmac.new(secret.encode(), stamp.encode()+b'.'+raw, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            return Response(status_code=403)
    except (ValueError, KeyError, TypeError, OSError):
        return Response(status_code=403)
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(35, connect=3), follow_redirects=False) as client:
            result = await client.post('http://127.0.0.1:18000/integrations/agent-platform/whatsapp',
                content=bytes(raw), headers={'Content-Type': 'application/json',
                'X-Agent-Timestamp': stamp, 'X-Agent-Signature': signature})
        return Response(content=result.content, status_code=result.status_code, media_type='application/json')
    except httpx.HTTPError:
        return Response(status_code=503)
