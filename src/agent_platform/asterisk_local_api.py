"""Local TLS gateway for exactly one agent or app-owned connection; no operator UI or general proxy."""

import hmac

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import Response


def create_gateway(settings, *, transport=None):
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    byoc = bool(settings.get("connection_id"))
    prefix = (
        ("/v1/telephony/gateway/" + settings["connection_id"])
        if byoc
        else ("/v1/voice/agents/" + settings["agent_id"])
    )
    key = settings["gateway_key"] if byoc else settings["service_key"]

    async def forward(request: Request):
        if not hmac.compare_digest(request.headers.get("authorization", ""), "Bearer " + key) or (
            not byoc and request.headers.get("x-external-user-id") != settings["external_user_id"]
        ):
            return Response(status_code=401)
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > (384000 if byoc and "/speech/" in request.url.path else 16384):
                return Response(status_code=413)
        try:
            async with httpx.AsyncClient(transport=transport, timeout=65, follow_redirects=False) as client:
                result = await client.request(
                    request.method,
                    "http://127.0.0.1:4061" + request.url.path,
                    headers={
                        "Authorization": "Bearer " + key,
                        **({} if byoc else {"X-External-User-ID": settings["external_user_id"]}),
                        "Content-Type": request.headers.get("content-type", "application/json"),
                    },
                    content=bytes(body),
                )
        except httpx.HTTPError:
            return Response(status_code=503)
        return Response(
            result.content,
            status_code=result.status_code,
            media_type=result.headers.get("content-type", "application/json"),
            headers={
                "Cache-Control": "no-store",
                **(
                    {"X-Voice-Action": result.headers["X-Voice-Action"]}
                    if "X-Voice-Action" in result.headers
                    else {}
                ),
            },
        )

    if byoc:
        for suffix, method in [
            ("/heartbeat", "POST"),
            ("/calls", "POST"),
            ("/calls/{call_id}", "GET"),
            ("/calls/{call_id}/end", "POST"),
            ("/calls/{call_id}/turns/{turn}", "POST"),
            ("/calls/{call_id}/speech/{turn}", "POST"),
        ]:
            app.add_api_route(prefix + suffix, forward, methods=[method])
    else:
        app.add_api_route(prefix + "/preview", forward, methods=["POST"])
        app.add_api_route(prefix + "/readiness", forward, methods=["GET"])
    return app
