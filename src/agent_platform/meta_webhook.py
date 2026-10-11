"""One verified app webhook; signed events route only to the matching owned assets."""
import hashlib
import hmac
import json

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse

from .meta_client import MetaError, config
from .whatsapp import ingest


def install_meta_webhook(app):
    router=APIRouter()

    @router.get('/webhooks/meta', response_class=PlainTextResponse)
    async def verify(request: Request):
        try:
            cfg=config(signup=False)
        except MetaError:
            raise HTTPException(503, 'Messaging setup unavailable') from None
        challenge=request.query_params.get('hub.challenge','')
        if (request.query_params.get('hub.mode')!='subscribe' or not challenge or len(challenge)>200
            or not hmac.compare_digest(request.query_params.get('hub.verify_token',''),cfg['verify_token'])):
            raise HTTPException(403,'Webhook verification failed')
        async with request.app.state.repository.pool.connection() as conn:
            await conn.execute('INSERT INTO meta_platform_webhook(app_id,callback_url,verified_at) VALUES(%s,%s,now()) ON CONFLICT(app_id) DO UPDATE SET callback_url=EXCLUDED.callback_url,verified_at=now(),updated_at=now()', (cfg['app_id'],cfg['public_url']+'/webhooks/meta'))
        return challenge

    @router.post('/webhooks/meta')
    async def events(request: Request):
        try:
            cfg=config(signup=False)
        except MetaError:
            raise HTTPException(503,'Messaging setup unavailable') from None
        raw=bytearray()
        async for part in request.stream():
            raw.extend(part)
            if len(raw)>256000:
                raise HTTPException(413,'Webhook too large')
        expected='sha256='+hmac.new(cfg['app_secret'].encode(),raw,hashlib.sha256).hexdigest()
        if not hmac.compare_digest(request.headers.get('X-Hub-Signature-256',''),expected):
            raise HTTPException(403,'Invalid webhook signature')
        try:
            payload=json.loads(raw)
            if payload.get('object')!='whatsapp_business_account' or not isinstance(payload.get('entry'),list):
                raise TypeError()
            for entry in payload['entry']:
                if not isinstance(entry,dict) or not isinstance(entry.get('id'),str) or not isinstance(entry.get('changes'),list):
                    raise TypeError()
                for change in entry['changes']:
                    if not isinstance(change,dict) or not isinstance(change.get('value'),dict):
                        raise TypeError()
        except (ValueError,TypeError,AttributeError):
            raise HTTPException(400,'Invalid webhook payload') from None
        pool=request.app.state.repository.pool
        digest=hashlib.sha256(raw).hexdigest()
        async with pool.connection() as conn:
            if await (await conn.execute('SELECT digest FROM meta_webhook_receipts WHERE digest=%s',(digest,))).fetchone():
                return {'received':True}
        for entry in payload['entry']:
            for change in entry['changes']:
                value=change['value']
                from .db_scope import connection_identity
                if change.get('field')=='account_update':
                    await connection_identity(pool,'meta_waba',entry['id'])
                    async with pool.connection() as conn:
                        await conn.execute('UPDATE meta_connections SET checked_at=NULL WHERE waba_id=%s',(entry['id'],))
                    continue
                if change.get('field') not in {'messages', 'calls'}:
                    continue
                if not isinstance(value.get('metadata'), dict):
                    raise HTTPException(400, 'Invalid message metadata')
                phone=value['metadata'].get('phone_number_id')
                if not isinstance(phone, str):
                    raise HTTPException(400, 'Invalid message metadata')
                await connection_identity(pool,'meta_phone',phone)
                async with pool.connection() as conn:
                    channel=await (await conn.execute("SELECT c.* FROM whatsapp_channels c JOIN meta_connections m ON m.phone_id=c.phone_id AND m.app_id=c.app_id AND m.owner_id=c.owner_id WHERE m.phone_id=%s AND m.waba_id=%s AND m.state NOT IN ('DISCONNECTING','DISCONNECTED')", (phone,entry['id']))).fetchone()
                if channel:
                    try:
                        await ingest(pool,channel,{'entry':[{'changes':[change]}]})
                    except (ValueError,TypeError,AttributeError,KeyError):
                        raise HTTPException(400,'Invalid message event') from None
        # Mark only after processing. If a crash occurs, existing message IDs make replay safe.
        async with pool.connection() as conn:
            await conn.execute('INSERT INTO meta_webhook_receipts(digest) VALUES(%s) ON CONFLICT DO NOTHING',(digest,))
        return {'received':True}

    app.include_router(router)
