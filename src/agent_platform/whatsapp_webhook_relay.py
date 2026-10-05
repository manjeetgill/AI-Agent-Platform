"""Development relay: expose only signed WhatsApp webhook paths, never the local console."""
from uuid import UUID

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response


def create_relay(transport=None):
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.api_route('/webhooks/whatsapp/{channel_id}', methods=['GET', 'POST'])
    async def forward(channel_id: UUID, request: Request):
        raw = bytearray()
        async for part in request.stream():
            raw.extend(part)
            if len(raw) > 256000:
                raise HTTPException(413, 'Webhook too large')
        query = {key: request.query_params[key] for key in
                 ('hub.mode', 'hub.verify_token', 'hub.challenge') if key in request.query_params}
        headers = {'Content-Type': 'application/json',
                   'X-Hub-Signature-256': request.headers.get('X-Hub-Signature-256', '')}
        try:
            async with httpx.AsyncClient(transport=transport, timeout=httpx.Timeout(10, connect=2)) as client:
                result = await client.request(request.method,
                    'http://127.0.0.1:4061/webhooks/whatsapp/' + str(channel_id),
                    params=query, headers=headers, content=bytes(raw))
        except httpx.HTTPError:
            raise HTTPException(503, 'Webhook receiver unavailable') from None
        return Response(result.content, status_code=result.status_code,
                        media_type=result.headers.get('content-type', 'application/json'),
                        headers={'Cache-Control': 'no-store'})

    @app.api_route('/webhooks/meta', methods=['GET', 'POST'])
    @app.get('/connect/whatsapp-assets/{asset}')
    @app.get('/connect/whatsapp/{session_id}')
    @app.api_route('/connect/whatsapp/{session_id}/{action}', methods=['GET', 'POST'])
    async def hosted(request: Request, session_id: UUID | None = None, action: str | None = None, asset: str | None = None):
        if action and action not in {'bootstrap','status','code','assets','cancel','retry'}:
            raise HTTPException(404)
        if asset and asset not in {'meta-connect.js','meta-connect.css'}:
            raise HTTPException(404)
        raw=bytearray()
        async for part in request.stream():
            raw.extend(part)
            if len(raw)>256000:
                raise HTTPException(413,'Request too large')
        headers={k:v for k,v in request.headers.items() if k.lower() in
                 {'content-type','origin','x-hub-signature-256','x-signup-ticket'}}
        if request.cookies.get('meta_signup'):
            headers['Cookie']='meta_signup='+request.cookies['meta_signup']
        query={k:v for k,v in request.query_params.items() if k in {'hub.mode','hub.verify_token','hub.challenge'}}
        try:
            async with httpx.AsyncClient(transport=transport,timeout=httpx.Timeout(25,connect=2)) as client:
                result=await client.request(request.method,'http://127.0.0.1:4061'+request.url.path,
                                            params=query,headers=headers,content=bytes(raw))
        except httpx.HTTPError:
            raise HTTPException(503,'Connection service unavailable') from None
        safe={k:v for k,v in result.headers.items() if k.lower() in
              {'content-type','content-security-policy','set-cookie','referrer-policy'}}
        safe['Cache-Control']='no-store'
        return Response(result.content,status_code=result.status_code,headers=safe)

    return app


if __name__ == '__main__':
    import uvicorn
    uvicorn.run(create_relay(), host='127.0.0.1', port=4063, access_log=False)
