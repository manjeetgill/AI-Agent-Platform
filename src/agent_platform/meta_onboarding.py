"""Owner-scoped Embedded Signup sessions and a narrowly scoped hosted browser flow."""
import asyncio
import hashlib
import hmac
import secrets
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import Field, SecretStr

from .meta_client import MetaClient, MetaError, config, setup_status
from .meta_crypto import aad, seal
from .service_identity import authenticate
from .tasks import Strict

ASSETS = Path(__file__).with_name('console')
MESSAGES = {
    'PLATFORM_SETUP_REQUIRED': 'WhatsApp connection is not available yet. The platform owner must finish Meta app setup.',
    'CONNECTING': 'Complete authorization in the Meta window.',
    'EXCHANGING': 'Securing your Meta authorization…',
    'WAITING_ASSETS': 'Waiting for Meta to return your business number. Finish the Meta window.',
    'QUEUED': 'Finding your WhatsApp business account…',
    'ASSETS_DISCOVERED': 'Connecting your business number…',
    'PHONE_CONNECTING': 'Registering your business number…',
    'WEBHOOK_CONFIGURING': 'Setting up messaging and secure webhooks…',
    'VERIFYING': 'Verifying your connection…',
    'CONNECTED': 'WhatsApp connected successfully.',
    'REAUTH_REQUIRED': 'Meta authorization expired or was revoked. Connect with Meta again.',
    'ASSET_NOT_AUTHORIZED': 'Meta did not grant access to that business account. Reconnect and select the correct account.',
    'PHONE_NOT_AUTHORIZED': 'No authorized number was found in that account. Complete number setup in Meta and reconnect.',
    'NUMBER_VERIFICATION_REQUIRED': 'Verify your business number in Meta, then retry connection.',
    'REGISTRATION_UNCERTAIN': 'Number registration is still unconfirmed. Retry verification; registration will not be repeated blindly.',
    'REGISTRATION_ACTION_REQUIRED': 'Meta could not register this number. Check its verification and existing two-step PIN in Meta.',
    'META_REQUEST_REJECTED': 'Meta could not complete this step. Check business permissions or account requirements, then retry.',
    'PROVIDER_UNAVAILABLE': 'Meta is temporarily unavailable. Your saved connection can be checked again.',
    'WEBHOOK_PENDING': 'Messaging setup is still being verified. Retry connection checks.',
    'ASSET_ALREADY_CONNECTED': 'That account or number belongs to another workspace. Choose your own account.',
    'CANCELLED': 'Connection was cancelled. You can connect again when ready.',
    'DISCONNECTING': 'Stopping messaging and removing this connection…',
    'DISCONNECTED': 'WhatsApp disconnected. Connect again to resume.',
    'SESSION_EXPIRED': 'This connection session expired. Return to your app and start Connect WhatsApp again.',
    'UNSUPPORTED_FLOW': 'This number requires a different Meta onboarding flow. Choose a Cloud API number or complete its setup in Meta.',
}


def safe_session(row):
    state = row['state']
    return {'id': str(row['id']), 'state': state, 'message': MESSAGES.get(row.get('error_code') or state, 'Checking your connection…'),
            'connection_id': str(row['connection_id']) if row.get('connection_id') else None,
            'can_retry': bool(row.get('token_cipher') and row.get('phone_id')) and state in {'NUMBER_VERIFICATION_REQUIRED', 'REGISTRATION_UNCERTAIN', 'REGISTRATION_ACTION_REQUIRED', 'META_REQUEST_REJECTED', 'PROVIDER_UNAVAILABLE', 'WEBHOOK_PENDING'},
            'authorization_valid': (bool(row.get('token_cipher')) or state == 'CONNECTED') and state not in {'REAUTH_REQUIRED','CANCELLED','DISCONNECTED','DISCONNECTING'}}


async def current_session(pool, row):
    if row['state'] == 'CONNECTED' and row.get('connection_id'):
        async with pool.connection() as conn:
            connection = await (await conn.execute('SELECT state,error_code FROM meta_connections WHERE id=%s AND app_id=%s AND owner_id=%s', (row['connection_id'],row['app_id'],row['owner_id']))).fetchone()
        if connection and connection['state'] != 'CONNECTED':
            return {**row, **connection}
    return row


class Code(Strict):
    code: SecretStr = Field(min_length=10, max_length=8192)


class Assets(Strict):
    waba_id: str = Field(pattern=r'^\d{5,30}$')
    phone_id: str = Field(pattern=r'^\d{5,30}$')
    business_id: str | None = Field(default=None, pattern=r'^\d{5,30}$')
    finish_type: Literal['FINISH'] = 'FINISH'


class Confirm(Strict):
    confirm: Literal[True]


async def record(conn, row, event):
    await conn.execute('INSERT INTO meta_connection_events(app_id,owner_id,session_id,connection_id,event) VALUES(%s,%s,%s,%s,%s)',
        (row['app_id'], row['owner_id'], row.get('session_id', row['id']), row.get('connection_id'), event))


async def parse(request, model):
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > 12000:
            raise HTTPException(413, 'Connection response too large')
    try:
        return model.model_validate_json(body)
    except ValueError:
        raise HTTPException(422, 'Meta returned an incomplete connection response. Reconnect to try again.') from None


def install_onboarding(app):
    from .public_pages import install_public_pages

    install_public_pages(app)
    router = APIRouter()

    async def api_context(request, scope='channels:write'):
        pool = request.app.state.repository.pool
        claims = await authenticate(pool, request.headers.get('authorization', ''), request.headers.get('X-External-User-ID'), scope)
        return pool, claims

    def settings():
        try:
            return config()
        except MetaError as exc:
            raise HTTPException(503, MESSAGES[exc.code]) from None

    async def browser_context(request, session_id, mutation=False):
        cfg = settings()
        if mutation and request.headers.get('origin') != cfg['public_url']:
            raise HTTPException(403, 'Connection origin rejected')
        secret = request.cookies.get('meta_signup', '')
        pool = request.app.state.repository.pool
        from .db_scope import connection_identity
        await connection_identity(pool, 'signup', session_id)
        async with pool.connection() as conn:
            row = await (await conn.execute('SELECT * FROM meta_signup_sessions WHERE id=%s', (session_id,))).fetchone()
        if not row or not hmac.compare_digest(row['secret_hash'], hashlib.sha256(secret.encode()).hexdigest()):
            raise HTTPException(401, 'Return to your app and start Connect WhatsApp')
        if row['expires_at'] <= datetime.now(UTC):
            raise HTTPException(401, MESSAGES['SESSION_EXPIRED'])
        return pool, row, cfg

    @router.get('/v1/whatsapp/onboarding/config')
    async def configuration(request: Request):
        await api_context(request, 'channels:read')
        result = setup_status()
        if result['available']:
            result['public_url'] = config()['public_url']
        return result

    @router.post('/v1/whatsapp/onboarding', status_code=201)
    async def start(request: Request):
        pool, claims = await api_context(request)
        cfg = settings()
        ident, secret = uuid4(), secrets.token_urlsafe(32)
        async with pool.connection() as conn, conn.transaction():
            await conn.execute('SELECT id FROM platform_apps WHERE id=%s FOR UPDATE', (claims['app_id'],))
            count = await (await conn.execute("SELECT count(*) AS n FROM meta_signup_sessions WHERE app_id=%s AND owner_id=%s AND created_at>now()-interval '1 hour'", (claims['app_id'], claims['user']))).fetchone()
            if count['n'] >= 10:
                raise HTTPException(429, 'Too many connection attempts. Try again later.')
            row = await (await conn.execute('INSERT INTO meta_signup_sessions(id,app_id,owner_id,secret_hash) VALUES(%s,%s,%s,%s) RETURNING *',
                (ident, claims['app_id'], claims['user'], hashlib.sha256(secret.encode()).hexdigest()))).fetchone()
            await record(conn, row, 'signup:started')
        return {'id': ident, 'connect_url': cfg['public_url']+'/connect/whatsapp/'+str(ident)+'#ticket='+secret}

    @router.get('/v1/whatsapp/onboarding/{session_id}')
    async def status(session_id: UUID, request: Request):
        pool, claims = await api_context(request)
        async with pool.connection() as conn:
            row = await (await conn.execute('SELECT * FROM meta_signup_sessions WHERE id=%s AND app_id=%s AND owner_id=%s', (session_id, claims['app_id'], claims['user']))).fetchone()
        if not row:
            raise HTTPException(404, 'Connection attempt not found')
        return safe_session(await current_session(pool, row))

    @router.get('/v1/whatsapp/onboarding/{session_id}/channel')
    async def connected_channel(session_id: UUID, request: Request):
        pool, claims = await api_context(request, 'channels:read')
        async with pool.connection() as conn:
            row = await (await conn.execute("""
                SELECT ch.id,ch.app_id,ch.phone_id,ch.enabled,ch.execution_mode,ch.webhook_verified_at
                FROM meta_signup_sessions s
                JOIN meta_connections c ON c.id=s.connection_id
                    AND c.app_id=s.app_id AND c.owner_id=s.owner_id
                JOIN whatsapp_channels ch ON ch.phone_id=c.phone_id
                    AND ch.app_id=c.app_id AND ch.owner_id=c.owner_id
                WHERE s.id=%s AND s.app_id=%s AND s.owner_id=%s
                    AND s.state='CONNECTED' AND c.state='CONNECTED'
                    AND c.checked_at>now()-interval '15 minutes'
                    AND (c.token_expires_at IS NULL OR c.token_expires_at>now())
                """, (session_id, claims['app_id'], claims['user']))).fetchone()
        if not row:
            raise HTTPException(409, 'A verified, healthy Meta connection is required before preparing the agent')
        return row

    @router.get('/connect/whatsapp/{session_id}')
    async def page(session_id: UUID):
        return FileResponse(ASSETS/'meta-connect.html', headers={'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer',
            'Content-Security-Policy': "default-src 'none'; script-src 'self' https://connect.facebook.net; style-src 'self'; connect-src 'self' https://www.facebook.com https://graph.facebook.com; frame-src https://www.facebook.com https://web.facebook.com; img-src 'self' https://www.facebook.com data:; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"})

    @router.get('/connect/whatsapp-assets/{asset}')
    async def static(asset: str):
        if asset not in {'meta-connect.js', 'meta-connect.css'}:
            raise HTTPException(404)
        return FileResponse(ASSETS/asset)

    @router.post('/connect/whatsapp/{session_id}/bootstrap')
    async def bootstrap(session_id: UUID, request: Request):
        cfg = settings()
        if request.headers.get('origin') != cfg['public_url']:
            raise HTTPException(403, 'Connection origin rejected')
        ticket = request.headers.get('X-Signup-Ticket', '')
        if not 32 <= len(ticket) <= 128:
            raise HTTPException(401, 'Connection link is invalid')
        pool = request.app.state.repository.pool
        from .db_scope import connection_identity
        await connection_identity(pool,'signup',session_id)
        async with pool.connection() as conn:
            row = await (await conn.execute('SELECT * FROM meta_signup_sessions WHERE id=%s AND expires_at>now()', (session_id,))).fetchone()
        if not row or not hmac.compare_digest(row['secret_hash'], hashlib.sha256(ticket.encode()).hexdigest()):
            raise HTTPException(401, 'Connection link expired. Start from your app again.')
        response = JSONResponse({'ready': True})
        response.set_cookie('meta_signup', ticket, secure=True, httponly=True, samesite='strict',
                            path='/connect/whatsapp/'+str(session_id), max_age=1800)
        response.headers['Cache-Control'] = 'no-store'
        return response

    @router.get('/connect/whatsapp/{session_id}/status')
    async def browser_status(session_id: UUID, request: Request):
        pool, row, cfg = await browser_context(request, session_id)
        return {**safe_session(await current_session(pool, row)), 'sdk': {'app_id': cfg['app_id'], 'configuration_id': cfg['configuration_id'], 'graph_version': cfg['graph_version']}}

    @router.post('/connect/whatsapp/{session_id}/code')
    async def exchange(session_id: UUID, request: Request):
        pool, row, _ = await browser_context(request, session_id, True)
        body = await parse(request, Code)
        code = body.code.get_secret_value()
        hashed = hashlib.sha256(code.encode()).hexdigest()
        async with pool.connection() as conn, conn.transaction():
            row = await (await conn.execute('SELECT * FROM meta_signup_sessions WHERE id=%s FOR UPDATE', (session_id,))).fetchone()
            if row['code_hash']:
                if row['code_hash'] != hashed:
                    raise HTTPException(409, 'Start a new connection attempt before reconnecting')
                return safe_session(row)
            if row['state'] != 'CONNECTING':
                raise HTTPException(409, 'Start a new connection attempt')
            # Serialize claims across sessions as well as within one session.
            await conn.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))', ('meta-code:'+hashed,))
            # Claim the one-use code before requesting exchange; never replay after uncertainty.
            duplicate = await (await conn.execute('SELECT id FROM meta_signup_sessions WHERE code_hash=%s', (hashed,))).fetchone()
            if duplicate:
                raise HTTPException(409, 'Authorization already used. Reconnect with Meta.')
            await conn.execute("UPDATE meta_signup_sessions SET state='EXCHANGING',code_hash=%s,updated_at=now() WHERE id=%s", (hashed, session_id))
        try:
            async with asyncio.timeout(20):
                client = MetaClient()
                token = await client.exchange(code)
                expires = await client.validate(token)
            encrypted = seal(token, aad(row))
            async with pool.connection() as conn:
                await conn.execute("UPDATE meta_signup_sessions SET token_cipher=%s,token_expires_at=%s,state=CASE WHEN phone_id IS NULL THEN 'WAITING_ASSETS' ELSE 'QUEUED' END,updated_at=now() WHERE id=%s AND state='EXCHANGING'", (encrypted, expires, session_id))
        except Exception:  # noqa: BLE001 -- codes/tokens/provider details must never escape
            async with pool.connection() as conn:
                await conn.execute("UPDATE meta_signup_sessions SET state='REAUTH_REQUIRED',error_code='REAUTH_REQUIRED',updated_at=now() WHERE id=%s AND state='EXCHANGING'", (session_id,))
        return {'received': True}

    @router.post('/connect/whatsapp/{session_id}/assets')
    async def assets(session_id: UUID, request: Request):
        pool, row, _ = await browser_context(request, session_id, True)
        body = await parse(request, Assets)
        async with pool.connection() as conn, conn.transaction():
            row = await (await conn.execute('SELECT * FROM meta_signup_sessions WHERE id=%s FOR UPDATE', (session_id,))).fetchone()
            if row['phone_id']:
                if row['phone_id'] != body.phone_id or row['waba_id'] != body.waba_id:
                    raise HTTPException(409, 'Asset selection changed. Start a new Meta connection.')
                return {'received': True}
            if row['state'] not in {'CONNECTING', 'EXCHANGING', 'WAITING_ASSETS'}:
                raise HTTPException(409, 'This connection attempt is no longer active')
            await conn.execute("UPDATE meta_signup_sessions SET waba_id=%s,phone_id=%s,business_id=%s,finish_type=%s,state=CASE WHEN token_cipher IS NOT NULL THEN 'QUEUED' ELSE state END,updated_at=now() WHERE id=%s", (body.waba_id, body.phone_id, body.business_id, body.finish_type, session_id))
        return {'received': True}

    @router.post('/connect/whatsapp/{session_id}/cancel')
    async def cancel(session_id: UUID, request: Request):
        pool, _, _ = await browser_context(request, session_id, True)
        async with pool.connection() as conn:
            await conn.execute("UPDATE meta_signup_sessions SET state='CANCELLED',token_cipher=NULL,updated_at=now() WHERE id=%s AND state IN ('CONNECTING','EXCHANGING','WAITING_ASSETS')", (session_id,))
        return {'cancelled': True}

    @router.post('/connect/whatsapp/{session_id}/retry')
    async def retry(session_id: UUID, request: Request):
        pool, _, _ = await browser_context(request, session_id, True)
        async with pool.connection() as conn:
            await conn.execute("UPDATE meta_signup_sessions SET state='QUEUED',error_code=NULL,lease_until=NULL,updated_at=now() WHERE id=%s AND token_cipher IS NOT NULL AND phone_id IS NOT NULL AND state NOT IN ('CONNECTED','CANCELLED','EXCHANGING','REAUTH_REQUIRED','QUEUED') AND (lease_until IS NULL OR lease_until<now())", (session_id,))
        return {'checking': True}

    @router.get('/v1/whatsapp/connections')
    async def connections(request: Request):
        pool, claims = await api_context(request, 'channels:read')
        async with pool.connection() as conn:
            return await (await conn.execute('SELECT id,phone_id,display_number,business_name,state,error_code,checked_at,token_expires_at FROM meta_connections WHERE app_id=%s AND owner_id=%s ORDER BY created_at DESC LIMIT 100', (claims['app_id'], claims['user']))).fetchall()

    @router.post('/v1/whatsapp/connections/{connection_id}/disconnect')
    async def disconnect(connection_id: UUID, body: Confirm, request: Request):
        pool, claims = await api_context(request)
        async with pool.connection() as conn, conn.transaction():
            row = await (await conn.execute("UPDATE meta_connections SET state='DISCONNECTING',checked_at=NULL,updated_at=now() WHERE id=%s AND app_id=%s AND owner_id=%s RETURNING *", (connection_id, claims['app_id'], claims['user']))).fetchone()
            if not row:
                raise HTTPException(404, 'Connection not found')
            await conn.execute('UPDATE whatsapp_channels SET enabled=false,webhook_verified_at=NULL WHERE phone_id=%s AND app_id=%s AND owner_id=%s', (row['phone_id'], claims['app_id'], claims['user']))
            await conn.execute("UPDATE whatsapp_outbound SET status='CANCELLED',error='Business disconnected' WHERE status IN ('DRAFT','QUEUED') AND channel_id IN (SELECT id FROM whatsapp_channels WHERE phone_id=%s)", (row['phone_id'],))
            await conn.execute("UPDATE meta_signup_sessions SET state='CANCELLED',token_cipher=NULL WHERE connection_id=%s", (connection_id,))
            await conn.execute('INSERT INTO meta_connection_events(app_id,owner_id,connection_id,event) VALUES(%s,%s,%s,%s)', (claims['app_id'],claims['user'],connection_id,'connection:disconnect_requested'))
        return {'state': 'DISCONNECTING'}

    app.include_router(router)
