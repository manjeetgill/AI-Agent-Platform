"""Backend-only business provisioning. Each business is a separate application boundary."""
import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import Field

from .service_identity import authenticate
from .tasks import Strict

# A parent backend must already hold every permission it can delegate.
Feature = Literal['whatsapp_support', 'whatsapp_notifications', 'whatsapp_calling',
                  'internet_voice', 'telephone_voice', 'memory']

FEATURE_SCOPES: dict[Feature, set[str]] = {
    'whatsapp_support': {'channels:read', 'channels:write', 'knowledge:read',
                         'knowledge:write', 'knowledge:publish'},
    'whatsapp_notifications': {'channels:read', 'channels:write', 'alerts:send'},
    'whatsapp_calling': {'channels:read', 'calling:read', 'calling:write'},
    'internet_voice': {'agents:read', 'agents:write', 'agents:run', 'knowledge:read',
                       'knowledge:write', 'knowledge:publish'},
    'telephone_voice': {'agents:read', 'agents:write', 'agents:run', 'telephony:read',
                        'telephony:write', 'knowledge:read', 'knowledge:write',
                        'knowledge:publish'},
    'memory': {'memory:read', 'memory:write', 'memory:review'},
}


class Business(Strict):
    external_id: str = Field(min_length=1, max_length=100, pattern=r'^[a-zA-Z0-9_.:-]+$')
    name: str = Field(min_length=1, max_length=100)


class KeyRequest(Strict):
    days: int = Field(default=30, ge=1, le=90)
    features: list[Feature] = Field(default_factory=lambda: ['whatsapp_support'],
                                    min_length=1, max_length=6)


def public(row):
    return {k: row[k] for k in ('id', 'external_id', 'name', 'app_id', 'created_at')}


def enabled_features(scopes):
    granted = set(scopes)
    return [feature for feature, required in FEATURE_SCOPES.items() if required <= granted]


def install_business_workspaces(app):
    router = APIRouter(prefix='/businesses', tags=['Business workspaces'])

    async def context(request, write=False):
        pool = request.app.state.repository.pool
        claims = await authenticate(pool, request.headers.get('authorization', ''),
                                    request.headers.get('X-External-User-ID'),
                                    'channels:write' if write else 'channels:read')
        return pool, claims

    @router.post('', status_code=201)
    async def create(body: Business, request: Request):
        pool, claims = await context(request, True)
        async with pool.connection() as conn, conn.transaction():
            if await (await conn.execute('SELECT 1 FROM platform_business_workspaces WHERE app_id=%s', (claims['app_id'],))).fetchone():
                raise HTTPException(403, 'Business workspaces cannot provision nested businesses')
            # Serialize concurrent creates, including reused external IDs.
            await conn.execute('SELECT id FROM platform_apps WHERE id=%s FOR UPDATE', (claims['app_id'],))
            row = await (await conn.execute('SELECT * FROM platform_business_workspaces WHERE parent_app_id=%s AND external_id=%s',
                                            (claims['app_id'], body.external_id))).fetchone()
            if row:
                if str(row['owner_id']) != claims['user']:
                    raise HTTPException(409, 'Business reference already reserved')
                return public(row)
            app_id = uuid4()
            await conn.execute("SELECT set_config('agent.app',%s,true)",(str(app_id),))
            await conn.execute('INSERT INTO platform_apps(id,tenant_id,name) VALUES(%s,%s,%s)',
                               (app_id, claims['tenant'], body.name))
            await conn.execute("SELECT set_config('agent.app',%s,true)",(claims['app_id'],))
            row = await (await conn.execute('''INSERT INTO platform_business_workspaces
                (id,parent_app_id,owner_id,external_id,name,app_id) VALUES(%s,%s,%s,%s,%s,%s) RETURNING *''',
                (uuid4(), claims['app_id'], claims['user'], body.external_id, body.name, app_id))).fetchone()
        return public(row)

    @router.get('')
    async def listing(request: Request, after: UUID | None = None):
        pool, claims = await context(request)
        async with pool.connection() as conn:
            rows = await (await conn.execute('''SELECT * FROM platform_business_workspaces
                WHERE parent_app_id=%s AND owner_id=%s AND (%s::uuid IS NULL OR id>%s)
                ORDER BY id LIMIT 51''', (claims['app_id'], claims['user'], after, after))).fetchall()
        return {'businesses': [public(row) for row in rows[:50]],
                'next_after': str(rows[49]['id']) if len(rows)>50 else None}

    @router.post('/{business_id}/credentials', status_code=201)
    async def credential(business_id: UUID, body: KeyRequest, request: Request, response: Response):
        response.headers['Cache-Control'] = 'no-store'
        pool, claims = await context(request, True)
        if len(body.features) != len(set(body.features)):
            raise HTTPException(422, 'Select each feature once')
        scopes = sorted(set().union(*(FEATURE_SCOPES[feature] for feature in body.features)))
        for scope in scopes:
            await authenticate(pool, request.headers.get('authorization', ''),
                               request.headers.get('X-External-User-ID'), scope)
        ident, secret = uuid4(), secrets.token_urlsafe(32)
        expires = datetime.now(UTC) + timedelta(days=body.days)
        async with pool.connection() as conn, conn.transaction():
            row = await (await conn.execute('''SELECT * FROM platform_business_workspaces
                WHERE id=%s AND parent_app_id=%s AND owner_id=%s FOR UPDATE''',
                (business_id, claims['app_id'], claims['user']))).fetchone()
            if not row:
                raise HTTPException(404, 'Business not found')
            # A repeat creates a replacement and revokes previous business service keys.
            await conn.execute('UPDATE platform_service_keys SET revoked_at=now() WHERE app_id=%s AND revoked_at IS NULL', (row['app_id'],))
            await conn.execute('''INSERT INTO platform_service_keys(id,app_id,secret_hash,scopes,expires_at)
                VALUES(%s,%s,%s,%s,%s)''',
                (ident,row['app_id'],hashlib.sha256(secret.encode()).hexdigest(),scopes,expires))
        return {'app_id': row['app_id'], 'credential': f'apk_{ident}.{secret}', 'expires_at': expires,
                'storage': 'backend-only', 'external_user_id': 'business-owner',
                'features': body.features, 'scopes': scopes}

    @router.get('/{business_id}/credentials')
    async def credential_status(business_id: UUID, request: Request):
        pool, claims = await context(request)
        async with pool.connection() as conn:
            row = await (await conn.execute('''SELECT w.*,k.id AS key_id,k.scopes,k.expires_at,
                k.created_at AS credential_created_at FROM platform_business_workspaces w
                LEFT JOIN LATERAL (SELECT id,scopes,expires_at,created_at FROM platform_service_keys
                    WHERE app_id=w.app_id AND revoked_at IS NULL AND expires_at>now()
                    ORDER BY created_at DESC LIMIT 1) k ON true
                WHERE w.id=%s AND w.parent_app_id=%s AND w.owner_id=%s''',
                (business_id, claims['app_id'], claims['user']))).fetchone()
        if not row:
            raise HTTPException(404, 'Business not found')
        return {'business': public(row), 'active': bool(row['key_id']), 'key_id': row['key_id'],
                'scopes': row['scopes'] or [], 'features': enabled_features(row['scopes'] or []),
                'expires_at': row['expires_at'], 'created_at': row['credential_created_at'],
                'credential_recoverable': False}

    app.include_router(router, prefix='/v1')
    # Compatibility path for the first WhatsApp-only integration clients.
    app.include_router(router, prefix='/v1/whatsapp', include_in_schema=False)
