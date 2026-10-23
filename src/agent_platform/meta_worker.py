"""Durable Meta provisioning. Every replay reads remote state before mutation."""
import re
import secrets
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from uuid import uuid4

from fastapi import HTTPException

from .meta_client import MetaClient, MetaError, config
from .meta_crypto import aad, seal, unseal


def display_number(phone):
    value = re.sub(r'[^0-9]', '', str(phone.get('display_phone_number', '')))
    return '+' + value if re.fullmatch(r'[1-9]\d{7,14}', value) else None


async def update_session(pool, row, state, error=None):
    async with pool.connection() as conn, conn.transaction():
        updated = await (await conn.execute('UPDATE meta_signup_sessions SET state=%s,error_code=%s,lease_until=NULL,updated_at=now() WHERE id=%s AND lease_id=%s AND state<>\'CANCELLED\' RETURNING id',
                           (state, error, row['id'], row['lease_id']))).fetchone()
        if updated:
            await conn.execute('INSERT INTO meta_connection_events(app_id,owner_id,session_id,connection_id,event) SELECT app_id,owner_id,id,connection_id,%s FROM meta_signup_sessions WHERE id=%s', ('signup:'+state,row['id']))


async def platform_webhook(pool, client):
    cfg = client.settings
    callback = cfg['public_url']+'/webhooks/meta'
    token = cfg['app_id']+'|'+cfg['app_secret']
    async with pool.connection() as conn:
        await conn.execute('INSERT INTO meta_platform_webhook(app_id,callback_url) VALUES(%s,%s) ON CONFLICT(app_id) DO UPDATE SET verified_at=CASE WHEN meta_platform_webhook.callback_url=EXCLUDED.callback_url THEN meta_platform_webhook.verified_at ELSE NULL END,callback_url=EXCLUDED.callback_url', (cfg['app_id'], callback))
    subscriptions = (await client.request(cfg['app_id']+'/subscriptions', token)).get('data', [])
    found = next((s for s in subscriptions if s.get('object') == 'whatsapp_business_account'), None)
    # Meta explicitly disallows creating WhatsApp app subscriptions through this edge.
    # The platform owner configures the shared callback once in the app dashboard.
    fields = {f['name'] if isinstance(f, dict) else f for f in (found or {}).get('fields', [])}
    async with pool.connection() as conn:
        verified = await (await conn.execute('SELECT verified_at FROM meta_platform_webhook WHERE app_id=%s AND callback_url=%s', (cfg['app_id'], callback))).fetchone()
        if not found or found.get('callback_url') != callback or not found.get('active') or not {'messages', 'account_update'} <= fields or not verified or not verified['verified_at']:
            raise MetaError('WEBHOOK_PENDING')
        await conn.execute('UPDATE meta_platform_webhook SET subscription_confirmed_at=now() WHERE app_id=%s', (cfg['app_id'],))


@asynccontextmanager
async def account_lock(pool, waba):
    # Serialize remote account subscription changes across worker processes.
    # PostgreSQL releases this transaction lock automatically if the process crashes.
    async with pool.connection() as conn, conn.transaction():
        locked = await (await conn.execute('SELECT pg_try_advisory_xact_lock(hashtextextended(%s,0)) AS locked', ('meta-waba:'+waba,))).fetchone()
        if not locked['locked']:
            raise MetaError('PROVIDER_UNAVAILABLE')
        yield


async def verify_connection(pool, client, row, token, *, provision=False):
    async with account_lock(pool, row['waba_id']):
        await _verify_connection(pool, client, row, token, provision=provision)


async def _verify_connection(pool, client, row, token, *, provision=False):
    expires = await client.validate(token, row['waba_id'])
    phone = await client.phone(token, row['waba_id'], row['phone_id'])
    if phone.get('code_verification_status') != 'VERIFIED':
        raise MetaError('NUMBER_VERIFICATION_REQUIRED')
    if phone.get('status') != 'CONNECTED' or phone.get('platform_type') != 'CLOUD_API':
        if not provision:
            raise MetaError('REGISTRATION_ACTION_REQUIRED')
        # Record intention before the irreversible network boundary. Unknown outcomes need a GET check.
        async with pool.connection() as conn:
            claim = await (await conn.execute('UPDATE meta_connections SET registration_attempted_at=now() WHERE id=%s AND registration_attempted_at IS NULL RETURNING id', (row['id'],))).fetchone()
        if not claim:
            raise MetaError('REGISTRATION_UNCERTAIN')
        try:
            await client.request(row['phone_id']+'/register', token, 'POST', payload={
                'messaging_product': 'whatsapp', 'pin': unseal(row['pin_cipher'], aad(row, 'pin'))})
        except MetaError as exc:
            if not exc.uncertain:
                # Definite rejection may be retried after the customer fixes Meta requirements.
                async with pool.connection() as conn:
                    await conn.execute('UPDATE meta_connections SET registration_attempted_at=NULL WHERE id=%s', (row['id'],))
                raise MetaError('REGISTRATION_ACTION_REQUIRED') from None
            raise MetaError('REGISTRATION_UNCERTAIN') from None
        phone = await client.phone(token, row['waba_id'], row['phone_id'])
        if phone.get('status') != 'CONNECTED' or phone.get('platform_type') != 'CLOUD_API':
            raise MetaError('REGISTRATION_UNCERTAIN')
    await platform_webhook(pool, client)
    if not await client.subscribed(token, row['waba_id']):
        if not provision:
            raise MetaError('WEBHOOK_PENDING')
        await client.request(row['waba_id']+'/subscribed_apps', token, 'POST', payload={})
        if not await client.subscribed(token, row['waba_id']):
            raise MetaError('WEBHOOK_PENDING')
    callback = client.settings['public_url']+'/webhooks/meta'
    def effective(value):
        item = value.get('webhook_configuration', {})
        return item.get('phone_number') or item.get('whatsapp_business_account') or item.get('application')
    target = await client.request(row['phone_id'], token, params={'fields': 'webhook_configuration'})
    if effective(target) != callback:
        if not provision:
            raise MetaError('WEBHOOK_PENDING')
        await client.request(row['phone_id'], token, 'POST', payload={'webhook_configuration': {
            'override_callback_uri': callback, 'verify_token': client.settings['verify_token']}})
        if effective(await client.request(row['phone_id'], token, params={'fields': 'webhook_configuration'})) != callback:
            raise MetaError('WEBHOOK_PENDING')
    async with pool.connection() as conn, conn.transaction():
        updated = await (await conn.execute("UPDATE meta_connections SET state='CONNECTED',error_code=NULL,display_number=%s,business_name=%s,token_expires_at=%s,registration_confirmed_at=now(),subscription_confirmed_at=now(),checked_at=now(),updated_at=now() WHERE id=%s AND state NOT IN ('DISCONNECTING','DISCONNECTED') AND token_cipher=%s RETURNING id", (phone.get('display_phone_number'), phone.get('verified_name'), expires, row['id'], row['token_cipher']))).fetchone()
        if not updated:
            raise MetaError('CANCELLED')
        await conn.execute('''UPDATE whatsapp_channels SET webhook_verified_at=now(),
            display_number=COALESCE(%s,display_number) WHERE phone_id=%s AND app_id=%s AND owner_id=%s
            AND EXISTS(SELECT 1 FROM meta_connections WHERE id=%s AND state='CONNECTED')''',
            (display_number(phone),row['phone_id'],row['app_id'],row['owner_id'],row['id']))


async def tick_onboarding(pool, client=None):
    try:
        client = client or MetaClient()
    except MetaError:
        return False
    async with pool.connection() as conn, conn.transaction():
        await conn.execute("UPDATE meta_signup_sessions SET state='REAUTH_REQUIRED',error_code='REAUTH_REQUIRED',token_cipher=NULL WHERE state='EXCHANGING' AND updated_at<now()-interval '1 minute'")
        await conn.execute("UPDATE meta_signup_sessions SET state='CANCELLED',token_cipher=NULL WHERE expires_at<now() AND state IN ('CONNECTING','WAITING_ASSETS')")
        await conn.execute("UPDATE meta_signup_sessions SET token_cipher=NULL WHERE expires_at<now() AND (lease_until IS NULL OR lease_until<now()) AND state NOT IN ('QUEUED','ASSETS_DISCOVERED','PHONE_CONNECTING','WEBHOOK_CONFIGURING','VERIFYING')")
        row = await (await conn.execute("SELECT * FROM meta_signup_sessions WHERE token_cipher IS NOT NULL AND phone_id IS NOT NULL AND (state='QUEUED' OR (lease_until<now() AND state IN ('ASSETS_DISCOVERED','PHONE_CONNECTING','WEBHOOK_CONFIGURING','VERIFYING'))) AND (lease_until IS NULL OR lease_until<now()) ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1")).fetchone()
        if not row:
            return False
        lease = uuid4()
        await conn.execute("UPDATE meta_signup_sessions SET state='ASSETS_DISCOVERED',lease_id=%s,lease_until=now()+interval '5 minutes',updated_at=now() WHERE id=%s", (lease, row['id']))
        row['lease_id'] = lease
    linked = None
    try:
        token = unseal(row['token_cipher'], aad(row))
        expires = await client.validate(token, row['waba_id'])
        phone = await client.phone(token, row['waba_id'], row['phone_id'])
        async with pool.connection() as conn, conn.transaction():
            current = await (await conn.execute('SELECT state,lease_id FROM meta_signup_sessions WHERE id=%s FOR UPDATE', (row['id'],))).fetchone()
            if current['state'] == 'CANCELLED' or current['lease_id'] != lease:
                return True
            await conn.execute('INSERT INTO meta_waba_owners(waba_id,app_id,owner_id) VALUES(%s,%s,%s) ON CONFLICT DO NOTHING', (row['waba_id'],row['app_id'],row['owner_id']))
            owner = await (await conn.execute('SELECT * FROM meta_waba_owners WHERE waba_id=%s FOR UPDATE', (row['waba_id'],))).fetchone()
            if owner['app_id'] != row['app_id'] or owner['owner_id'] != row['owner_id']:
                raise MetaError('ASSET_ALREADY_CONNECTED')
            existing = await (await conn.execute('SELECT * FROM meta_connections WHERE phone_id=%s FOR UPDATE', (row['phone_id'],))).fetchone()
            legacy = await (await conn.execute('SELECT * FROM whatsapp_channels WHERE phone_id=%s', (row['phone_id'],))).fetchone()
            if any(x and (x['app_id'] != row['app_id'] or x['owner_id'] != row['owner_id']) for x in (existing, legacy)):
                raise MetaError('ASSET_ALREADY_CONNECTED')
            if existing and existing['state'] == 'DISCONNECTING':
                raise MetaError('PROVIDER_UNAVAILABLE')
            ident = existing['id'] if existing else uuid4()
            linked = {**row, 'id': ident}
            encrypted = seal(token, aad(linked))
            pin = existing['pin_cipher'] if existing else seal(f'{secrets.randbelow(1000000):06}', aad(linked, 'pin'))
            await conn.execute("INSERT INTO meta_connections(id,app_id,owner_id,waba_id,phone_id,business_id,state,token_cipher,token_expires_at,pin_cipher) VALUES(%s,%s,%s,%s,%s,%s,'VERIFYING',%s,%s,%s) ON CONFLICT(phone_id) DO UPDATE SET state='VERIFYING',token_cipher=EXCLUDED.token_cipher,token_expires_at=EXCLUDED.token_expires_at,error_code=NULL,updated_at=now()", (ident,row['app_id'],row['owner_id'],row['waba_id'],row['phone_id'],row['business_id'],encrypted,expires,pin))
            linked = await (await conn.execute('SELECT * FROM meta_connections WHERE id=%s', (ident,))).fetchone()
            await conn.execute("UPDATE meta_signup_sessions SET connection_id=%s,state='PHONE_CONNECTING',updated_at=now() WHERE id=%s AND lease_id=%s", (ident,row['id'],lease))
            if not legacy:
                await conn.execute('''INSERT INTO whatsapp_channels
                    (id,app_id,owner_id,phone_id,name,documents,display_number)
                    VALUES(%s,%s,%s,%s,%s,%s,%s)''',
                    (uuid4(),row['app_id'],row['owner_id'],row['phone_id'],
                     phone.get('verified_name') or 'WhatsApp support',[],display_number(phone)))
        await verify_connection(pool, client, linked, token, provision=True)
        await update_session(pool, row, 'CONNECTED')
    except MetaError as exc:
        await update_session(pool, row, exc.code, exc.code)
        if linked:
            async with pool.connection() as conn:
                await conn.execute("UPDATE meta_connections SET state=%s,error_code=%s,checked_at=now() WHERE id=%s AND state NOT IN ('DISCONNECTING','DISCONNECTED') AND token_cipher=%s", (exc.code,exc.code,linked['id'],linked['token_cipher']))
    except Exception:  # noqa: BLE001 -- do not leak tokens or provider data
        await update_session(pool,row,'PROVIDER_UNAVAILABLE','PROVIDER_UNAVAILABLE')
    return True


async def managed_config(pool, phone, app, owner=None, provider=None):
    from .whatsapp import connection
    if provider == 'chakra':
        from .chakra_onboarding import stored_config
        chakra = await stored_config(pool, phone, app, owner)
        if chakra:
            return {**chakra, 'provider': 'chakra'}
        try:
            return connection(phone, app, provider='chakra')
        except HTTPException as exc:
            if exc.status_code == 503:
                raise HTTPException(404, 'Chakra connection not found') from None
            raise
    if provider == 'meta':
        async with pool.connection() as conn:
            row = await (await conn.execute('SELECT * FROM meta_connections WHERE phone_id=%s', (phone,))).fetchone()
        if not row:
            try:
                return connection(phone, app, provider='meta')
            except HTTPException as exc:
                if exc.status_code == 503:
                    raise HTTPException(404, 'Meta connection not found') from None
                raise
        if str(row['app_id']) != str(app) or (owner and str(row['owner_id']) != str(owner)):
            raise HTTPException(404, 'Business connection not found')
        now = datetime.now(UTC)
        if row['state'] != 'CONNECTED' or not row['checked_at'] or (now-row['checked_at']).total_seconds()>900 or (row['token_expires_at'] and row['token_expires_at'] <= now):
            raise HTTPException(409, 'Reconnect or verify your WhatsApp connection before messaging')
        cfg = config()
        return {'provider': 'meta', 'phone_id': row['phone_id'], 'access_token': unseal(row['token_cipher'],aad(row)), 'app_secret':cfg['app_secret'],
                'verify_token':cfg['verify_token'],'graph_version':cfg['graph_version'],'waba_id':row['waba_id'],
                'display_phone_number': row.get('display_phone_number')}

    from .chakra_onboarding import stored_config
    chakra = await stored_config(pool, phone, app, owner)
    if chakra:
        return {**chakra, 'provider': 'chakra'}
    async with pool.connection() as conn:
        row = await (await conn.execute('SELECT * FROM meta_connections WHERE phone_id=%s', (phone,))).fetchone()
    if not row:
        try:
            return connection(phone, app)
        except HTTPException as exc:
            if exc.status_code==503:
                raise HTTPException(404,'Business connection not found') from None
            raise
    if str(row['app_id']) != str(app) or (owner and str(row['owner_id']) != str(owner)):
        raise HTTPException(404, 'Business connection not found')
    now = datetime.now(UTC)
    if row['state'] != 'CONNECTED' or not row['checked_at'] or (now-row['checked_at']).total_seconds()>900 or (row['token_expires_at'] and row['token_expires_at'] <= now):
        raise HTTPException(409, 'Reconnect or verify your WhatsApp connection before messaging')
    cfg = config()
    return {'provider': 'meta', 'phone_id': row['phone_id'], 'access_token': unseal(row['token_cipher'],aad(row)), 'app_secret':cfg['app_secret'],
            'verify_token':cfg['verify_token'],'graph_version':cfg['graph_version'],'waba_id':row['waba_id'],
            'display_phone_number': row.get('display_phone_number')}


async def tick_health(pool, client=None):
    try:
        client=client or MetaClient()
    except MetaError:
        return False
    async with pool.connection() as conn:
        row=await (await conn.execute("SELECT * FROM meta_connections WHERE state IN ('DISCONNECTING','CONNECTED') AND (checked_at IS NULL OR checked_at<now()-interval '5 minutes') ORDER BY checked_at NULLS FIRST LIMIT 1")).fetchone()
    if not row:
        return False
    try:
        token=unseal(row['token_cipher'],aad(row)) if row['token_cipher'] else None
        if row['state']=='DISCONNECTING':
            async with account_lock(pool, row['waba_id']):
                async with pool.connection() as conn:
                    others=await (await conn.execute("SELECT id FROM meta_connections WHERE waba_id=%s AND id<>%s AND state NOT IN ('DISCONNECTING','DISCONNECTED')", (row['waba_id'],row['id']))).fetchone()
                if token and not others and await client.subscribed(token,row['waba_id']):
                    await client.request(row['waba_id']+'/subscribed_apps',token,'DELETE')
                    if await client.subscribed(token,row['waba_id']):
                        raise MetaError('WEBHOOK_PENDING')
                async with pool.connection() as conn:
                    await conn.execute("UPDATE meta_connections SET state='DISCONNECTED',token_cipher=NULL,error_code=NULL,updated_at=now() WHERE id=%s AND state='DISCONNECTING'",(row['id'],))
            return True
        await verify_connection(pool,client,row,token)
    except MetaError as exc:
        async with pool.connection() as conn:
            if row['state']=='DISCONNECTING' and exc.code=='REAUTH_REQUIRED':
                await conn.execute("UPDATE meta_connections SET state='DISCONNECTED',token_cipher=NULL,error_code=NULL WHERE id=%s AND state='DISCONNECTING'", (row['id'],))
            else:
                await conn.execute('UPDATE meta_connections SET state=%s,error_code=%s,checked_at=now() WHERE id=%s AND state=%s AND token_cipher=%s', ('DISCONNECTING' if row['state']=='DISCONNECTING' else exc.code,exc.code,row['id'],row['state'],row['token_cipher']))
    return True


async def serve():
    import asyncio
    import logging
    import os

    from psycopg.rows import dict_row
    from psycopg_pool import AsyncConnectionPool

    from .db_scope import worker_connection
    async with AsyncConnectionPool(os.environ.get('AI_PLATFORM_WORKER_DATABASE_URL') or os.environ['AI_PLATFORM_DATABASE_URL'], configure=worker_connection, min_size=1,max_size=4,open=False,kwargs={'row_factory':dict_row}) as pool:
        from .db_scope import assert_runtime_role
        await assert_runtime_role(pool,'worker')
        while True:
            try:
                await tick_onboarding(pool)
                await tick_health(pool)
            except Exception:  # noqa: BLE001 -- never include provider/credential data in logs
                logging.getLogger(__name__).warning('Meta connection worker unavailable')
            await asyncio.sleep(2)


if __name__=='__main__':
    import asyncio
    asyncio.run(serve())
