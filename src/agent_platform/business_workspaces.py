"""Backend-only business provisioning. Each business is a separate application boundary."""
import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import Field

from .service_identity import authenticate
from .tasks import Strict

# A parent backend must already hold every permission it can delegate.
BUSINESS_SCOPES = ['channels:read', 'channels:write', 'knowledge:read',
                   'knowledge:write', 'knowledge:publish']


class Business(Strict):
    external_id: str = Field(min_length=1, max_length=100, pattern=r'^[a-zA-Z0-9_.:-]+$')
    name: str = Field(min_length=1, max_length=100)


class KeyRequest(Strict):
    days: int = Field(default=30, ge=1, le=90)


def public(row):
    return {k: row[k] for k in ('id', 'external_id', 'name', 'app_id', 'created_at')}


def install_business_workspaces(app):
    router = APIRouter(prefix='/v1/whatsapp/businesses')

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
            await conn.execute('INSERT INTO platform_apps(id,tenant_id,name) VALUES(%s,%s,%s)',
                               (app_id, claims['tenant'], body.name))
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
        for scope in BUSINESS_SCOPES:
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
                VALUES(%s,%s,%s,%s,%s)''', (ident,row['app_id'],hashlib.sha256(secret.encode()).hexdigest(),BUSINESS_SCOPES,expires))
        return {'app_id': row['app_id'], 'credential': f'apk_{ident}.{secret}', 'expires_at': expires,
                'storage': 'backend-only', 'external_user_id': 'business-owner'}

    app.include_router(router)
