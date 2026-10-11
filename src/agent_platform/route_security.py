"""Authenticate versioned APIs before body validation, including future routes.

Endpoints still enforce their own scopes, ownership and channel permissions.
Provider callbacks and device/gateway credentials have separate authenticators.
"""
import hashlib
import re

from fastapi import HTTPException
from starlette.responses import JSONResponse

from .db_scope import identity, operator_access, verified_delegation
from .delegation import authenticate as authenticate_delegation
from .service_identity import authenticate
from .tokens import TokenError

EMAIL_CALLBACK = re.compile(r'/v1/notifications/email/webhooks/[a-fA-F0-9-]{36}/(?:resend|brevo)$')
DEVICE_ROUTE = re.compile(r'/v1/retailer-browser/(?:poll|stop|[a-fA-F0-9-]{36}/(?:authorize|result))$')
GATEWAY_ROUTE = re.compile(r'/v1/telephony/gateway/[a-fA-F0-9-]{36}/(?:heartbeat|calls(?:/.*)?)$')
LOCAL_BROWSER_ROUTE = re.compile(r'/v1/browser/(?:poll|stop|[a-fA-F0-9-]{36}/(?:authorize|result))$')


def install(app, legacy_key):
    @app.middleware('http')
    async def authenticate_api(request, call_next):
        identity.set(None)
        operator_access.set(False)
        path = request.url.path
        if path == '/monitor' or path.startswith('/monitor/'):
            from .local_monitor import local
            try:
                local(request)
                operator_access.set(True)
            except HTTPException as exc:
                return JSONResponse({'detail': exc.detail}, status_code=exc.status_code)
        if path.startswith('/console/'):
            from .console_app import local
            try:
                local(request,mutation=request.method not in {'GET','HEAD','OPTIONS'})
                operator_access.set(True)
            except HTTPException as exc:
                return JSONResponse({'detail':exc.detail},status_code=exc.status_code)
        if not path.startswith('/v1/') or EMAIL_CALLBACK.fullmatch(path):
            return await call_next(request)
        authorization = request.headers.get('authorization', '')
        if not authorization.startswith('Bearer ') or len(authorization) > 8192:
            return JSONResponse({'detail': 'Authentication required'}, status_code=401)
        token = authorization[7:]
        # These routes validate their own narrowly scoped credentials. Never allow
        # a device or gateway token to enter a generic application endpoint.
        if ((token.startswith('rbr_') and DEVICE_ROUTE.fullmatch(path)) or
                (token.startswith('tgw_') and GATEWAY_ROUTE.fullmatch(path)) or
                (LOCAL_BROWSER_ROUTE.fullmatch(path) and re.fullmatch(r'[a-fA-F0-9-]{36}\.[A-Za-z0-9_-]{43}', token))):
            return await call_next(request)
        try:
            if token.startswith('apk_'):
                pool = getattr(request.app.state.repository, 'pool', None)
                if pool is None:
                    raise HTTPException(503, 'Identity storage unavailable')
                claims = await authenticate(pool, authorization, request.headers.get('x-external-user-id'), None)
            else:
                claims = await authenticate_delegation(getattr(request.app.state.repository, 'pool', None), legacy_key, token)
            request.state.identity = claims
        except HTTPException as exc:
            return JSONResponse({'detail': exc.detail}, status_code=exc.status_code)
        except TokenError:
            return JSONResponse({'detail': 'Invalid delegation'}, status_code=401)
        marker = identity.set(claims)
        verified = verified_delegation.set((hashlib.sha256(token.encode()).digest(), claims))
        try:
            return await call_next(request)
        finally:
            verified_delegation.reset(verified)
            identity.reset(marker)
