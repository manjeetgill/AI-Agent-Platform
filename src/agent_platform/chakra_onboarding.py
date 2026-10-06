"""Chakra signup and encrypted per-business bindings.

A browser callback is a hint, not proof of plugin ownership. New bindings require
operator confirmation until Chakra exposes authenticated clientReferenceId lookup.
"""
import argparse
import asyncio
import json
import os
import re
from typing import Literal
from urllib.parse import urlsplit
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import httpx
from fastapi import APIRouter, HTTPException, Request, Response
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from pydantic import Field

from .meta_crypto import keyring, seal, unseal
from .service_identity import authenticate
from .tasks import Strict


class Completed(Strict):
    plugin_id: UUID


class ChannelSetup(Strict):
    # Preserve older clinic clients which send no body. Other apps set these explicitly.
    name: str = Field(default='Clinic appointment assistant', min_length=1, max_length=80)
    execution_mode: Literal['RAG', 'SAHAYAK'] = 'SAHAYAK'
    documents: list[UUID] = Field(default_factory=list, max_length=50)


def settings():
    values = {name: os.environ.get('AI_CHAKRA_'+name, '').strip()
              for name in ('PARTNER_TOKEN', 'WEBHOOK_SECRET', 'GRAPH_VERSION')}
    public = os.environ.get('AI_PLATFORM_PUBLIC_URL', '').rstrip('/')
    if (not values['PARTNER_TOKEN'] or len(values['WEBHOOK_SECRET'])<32
        or not re.fullmatch(r'v\d+\.\d+', values['GRAPH_VERSION'])
        or urlsplit(public).scheme != 'https'):
        raise HTTPException(503, 'Chakra partner onboarding requires operator configuration')
    return values, public


def context(row):
    return ':'.join(map(str, ('chakra', row['app_id'],row['owner_id'],row['id'])))


async def provider(method, path, token, body=None):
    async with httpx.AsyncClient(timeout=httpx.Timeout(15, connect=3), follow_redirects=False) as client:
        try:
            response = await client.request(method, 'https://api.chakrahq.com/v1/ext/'+path,
                                            headers={'Authorization':'Bearer '+token}, json=body)
            response.raise_for_status()
            return response.json()['_data']
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            raise HTTPException(503, 'Chakra request unavailable; verify the saved state before retrying') from None


async def stored_config(pool, phone, app, owner=None):
    async with pool.connection() as conn:
        row = await (await conn.execute('SELECT * FROM chakra_customer_connections WHERE phone_id=%s', (phone,))).fetchone()
    if not row:
        return None
    if str(row['app_id']) != str(app) or (owner and str(row['owner_id']) != str(owner)):
        raise HTTPException(404, 'Business connection not found')
    if row['state'] != 'CONNECTED':
        raise HTTPException(409, 'Business connection is disconnected')
    return json.loads(unseal(row['config_cipher'],context(row)))


def install_chakra_onboarding(app):
    router = APIRouter(prefix='/v1/whatsapp/chakra')

    async def auth(request, scope='channels:write'):
        pool = request.app.state.repository.pool
        claims = await authenticate(pool,request.headers.get('authorization',''),request.headers.get('X-External-User-ID'),scope)
        return pool,claims

    @router.get('/config')
    async def configuration(request: Request):
        await auth(request,'channels:read')
        try:
            settings()
        except HTTPException:
            return {'available':False,'reason':'PARTNER_CONFIGURATION_REQUIRED'}
        return {'available':True,'ownership_verification':'operator',
                'sdk_url':'https://embed.chakrahq.com/whatsapp-partner-connect/v1_0_1/sdk.js'}

    @router.post('/onboarding', status_code=201)
    async def start(request: Request, response: Response):
        response.headers['Cache-Control'] = 'no-store'
        keyring()
        pool,claims = await auth(request)
        cfg,_ = settings()
        ident=uuid4()
        async with pool.connection() as conn,conn.transaction():
            await conn.execute('SELECT id FROM platform_apps WHERE id=%s FOR UPDATE',(claims['app_id'],))
            count=await(await conn.execute("SELECT count(*) AS n FROM chakra_signup_sessions WHERE app_id=%s AND created_at>now()-interval '1 hour'",(claims['app_id'],))).fetchone()
            if count['n']>=10:
                raise HTTPException(429,'Too many connection attempts')
            row=await(await conn.execute('INSERT INTO chakra_signup_sessions(id,app_id,owner_id) VALUES(%s,%s,%s) RETURNING *',(ident,claims['app_id'],claims['user']))).fetchone()
        try:
            result=await provider('POST','whatsapp-partner/create-connect-token',cfg['PARTNER_TOKEN'],
                                  {'clientReferenceId':str(ident),'newPluginName':'Business '+str(ident)})
            token=result.get('connectToken')
            if not isinstance(token,str) or not 20<len(token)<16000:
                raise HTTPException(502,'Invalid Chakra signup response')
            cipher=seal(token,context(row))
        except Exception:
            async with pool.connection() as conn:
                await conn.execute("UPDATE chakra_signup_sessions SET state='CHECK_REQUIRED' WHERE id=%s",(ident,))
            raise
        async with pool.connection() as conn:
            await conn.execute("UPDATE chakra_signup_sessions SET state='CONNECTING',token_cipher=%s WHERE id=%s",(cipher,ident))
        return {'id':ident,'connect_token':token,'expires_at':row['expires_at'],
                'sdk_url':'https://embed.chakrahq.com/whatsapp-partner-connect/v1_0_1/sdk.js'}

    @router.get('/onboarding/{session_id}')
    async def status(session_id: UUID,request: Request):
        pool,claims=await auth(request,'channels:read')
        async with pool.connection() as conn:
            row=await(await conn.execute('SELECT id,state,expires_at FROM chakra_signup_sessions WHERE id=%s AND app_id=%s AND owner_id=%s',(session_id,claims['app_id'],claims['user']))).fetchone()
        if not row:
            raise HTTPException(404,'Signup not found')
        return row

    @router.post('/onboarding/{session_id}/resume')
    async def resume(session_id: UUID, request: Request, response: Response):
        response.headers['Cache-Control'] = 'no-store'
        pool, claims = await auth(request)
        async with pool.connection() as conn:
            row = await (await conn.execute("SELECT * FROM chakra_signup_sessions WHERE id=%s AND app_id=%s AND owner_id=%s AND state='CONNECTING' AND expires_at>now()", (session_id, claims['app_id'], claims['user']))).fetchone()
        if not row or not row['token_cipher']:
            raise HTTPException(409, 'Saved authorization cannot resume; operator reconciliation required')
        return {'id': row['id'], 'connect_token': unseal(row['token_cipher'], context(row))}

    @router.post('/onboarding/{session_id}/channel')
    async def ensure_channel(session_id: UUID, request: Request, body: ChannelSetup | None = None):
        body = body or ChannelSetup()
        pool, claims = await auth(request)
        async with pool.connection() as conn, conn.transaction():
            connection = await (await conn.execute("SELECT c.* FROM chakra_customer_connections c JOIN chakra_signup_sessions s ON s.id=c.id WHERE c.id=%s AND c.app_id=%s AND c.owner_id=%s AND c.state='CONNECTED' AND s.state='CONNECTED' FOR UPDATE OF c", (session_id, claims['app_id'], claims['user']))).fetchone()
            if not connection:
                raise HTTPException(409, 'Number ownership must be verified before preparing the agent')
            if body.execution_mode == 'RAG' and not body.documents:
                raise HTTPException(422, 'Select business FAQs before preparing a knowledge agent')
            for document in body.documents:
                allowed = await (await conn.execute("SELECT id FROM knowledge_documents WHERE id=%s AND app_id=%s AND visibility='app'", (document, claims['app_id']))).fetchone()
                if not allowed:
                    raise HTTPException(422, 'Document is not published for this business')
            ident = uuid5(NAMESPACE_URL, 'sahayak-channel:'+str(session_id))
            await conn.execute("INSERT INTO whatsapp_channels(id,app_id,owner_id,phone_id,name,documents,execution_mode) VALUES(%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(phone_id) DO NOTHING", (ident,claims['app_id'],claims['user'],connection['phone_id'],body.name,body.documents,body.execution_mode))
            row = await (await conn.execute('SELECT * FROM whatsapp_channels WHERE phone_id=%s', (connection['phone_id'],))).fetchone()
            if row['id'] != ident or str(row['app_id']) != str(claims['app_id']) or str(row['owner_id']) != str(claims['user']) or row['execution_mode'] != body.execution_mode or row['name'] != body.name or set(row['documents']) != set(body.documents):
                raise HTTPException(409, 'Existing number configuration needs operator review')
            return {k: row[k] for k in ('id','app_id','phone_id','enabled','execution_mode','webhook_verified_at')}

    @router.post('/onboarding/{session_id}/complete')
    async def complete(session_id: UUID,body: Completed,request: Request):
        pool,claims=await auth(request)
        async with pool.connection() as conn:
            row=await(await conn.execute("""UPDATE chakra_signup_sessions SET candidate_plugin_id=%s,state='OWNERSHIP_CHECK_REQUIRED'
                WHERE id=%s AND app_id=%s AND owner_id=%s AND state='CONNECTING' AND expires_at>now() RETURNING id,state""",
                (body.plugin_id,session_id,claims['app_id'],claims['user']))).fetchone()
        if not row:
            raise HTTPException(409,'Signup expired or already submitted; read its saved status')
        return {**row,'message':'Authorization received. The platform operator must verify the customer-to-plugin binding.'}

    @router.get('/connections')
    async def connections(request: Request):
        pool,claims=await auth(request,'channels:read')
        async with pool.connection() as conn:
            return await(await conn.execute('SELECT id,phone_id,waba_id,state,created_at FROM chakra_customer_connections WHERE app_id=%s AND owner_id=%s ORDER BY created_at DESC LIMIT 100',
                (claims['app_id'],claims['user']))).fetchall()

    @router.post('/connections/{connection_id}/disconnect')
    async def disconnect(connection_id: UUID,request: Request):
        pool,claims=await auth(request)
        async with pool.connection() as conn,conn.transaction():
            row=await(await conn.execute('SELECT * FROM chakra_customer_connections WHERE id=%s AND app_id=%s AND owner_id=%s FOR UPDATE',
                (connection_id,claims['app_id'],claims['user']))).fetchone()
            if not row:
                raise HTTPException(404,'Business connection not found')
            # Same channel lock as dispatch: after this acknowledges, no new send can start.
            channels=await(await conn.execute('SELECT id FROM whatsapp_channels WHERE phone_id=%s FOR UPDATE',(row['phone_id'],))).fetchall()
            for channel in channels:
                await conn.execute('UPDATE whatsapp_channels SET enabled=false WHERE id=%s',(channel['id'],))
                await conn.execute("UPDATE whatsapp_messages SET status='CANCELLED' WHERE status IN ('PENDING','GENERATING','READY') AND conversation_id IN (SELECT id FROM whatsapp_conversations WHERE channel_id=%s)",(channel['id'],))
                from .whatsapp import audit
                await audit(conn,channel['id'],None,'connection:disconnected',claims['principal'])
            await conn.execute("UPDATE chakra_customer_connections SET state='DISCONNECTED',config_cipher='',updated_at=now() WHERE id=%s",(connection_id,))
        return {'state':'DISCONNECTED','provider_authorization_revoked':False,
                'message':'Platform messaging stopped. Remove provider authorization in Chakra/Meta if required.'}

    @router.post('/channels/{channel_id}/webhook')
    async def configure_webhook(channel_id: UUID, request: Request):
        pool,claims=await auth(request)
        _,public=settings()
        async with pool.connection() as conn:
            row=await(await conn.execute('SELECT * FROM whatsapp_channels WHERE id=%s AND app_id=%s AND owner_id=%s',
                (channel_id,claims['app_id'],claims['user']))).fetchone()
        if not row:
            raise HTTPException(404,'Channel not found')
        cfg=await stored_config(pool,row['phone_id'],claims['app_id'],claims['user'])
        if not cfg:
            raise HTTPException(409,'Verified Chakra customer binding required')
        path='plugin/whatsapp/'+cfg['plugin_id']
        callback=public+'/webhooks/whatsapp/'+str(channel_id)
        remote=await provider('GET',path+'/config',cfg['access_token'])
        existing=remote.get('passThroughWebhookUrl')
        if existing and existing!=callback:
            raise HTTPException(409,'Plugin already forwards to a different endpoint; operator review required')
        if existing!=callback:
            await provider('POST',path+'/update-config',cfg['access_token'],{'passThroughWebhookUrl':callback})
            remote=await provider('GET',path+'/config',cfg['access_token'])
        if remote.get('passThroughWebhookUrl')!=callback:
            raise HTTPException(409,'Webhook configuration not confirmed; check again before activation')
        return {'configured':True,'webhook_url':callback,'verified':bool(row['webhook_verified_at']),
                'next_step':'Send a test message to verify the signed webhook before activation'}

    app.include_router(router)


async def bind(pool, session_id, phone, waba):
    """Trusted operator command after independently verifying the Chakra customer record."""
    cfg,_=settings()
    async with pool.connection() as conn:
        row=await(await conn.execute("SELECT * FROM chakra_signup_sessions WHERE id=%s AND state='OWNERSHIP_CHECK_REQUIRED'",(session_id,))).fetchone()
    if not row:
        raise ValueError('No pending ownership check')
    plugin=str(row['candidate_plugin_id'])
    remote=await provider('GET',f'plugin/whatsapp/{plugin}/config',cfg['PARTNER_TOKEN'])
    if (not any(str(v.get('id'))==phone and v.get('status')=='CONNECTED' for v in remote.get('whatsappPhoneNumbers',[]))
        or not any(str(v.get('id'))==waba for v in remote.get('whatsappBusinessAccounts',[]))):
        raise ValueError('Provider did not confirm the selected number/account')
    if len(remote['whatsappBusinessAccounts']) != 1:
        raise ValueError('Multiple accounts require an independently verified number/account mapping')
    config={'provider':'chakra','app_id':str(row['app_id']),'owner_id':str(row['owner_id']),
            'phone_id':phone,'waba_id':waba,'plugin_id':plugin,'access_token':cfg['PARTNER_TOKEN'],
            'webhook_secret':cfg['WEBHOOK_SECRET'],'graph_version':cfg['GRAPH_VERSION']}
    async with pool.connection() as conn,conn.transaction():
        await conn.execute('SELECT id FROM chakra_signup_sessions WHERE id=%s FOR UPDATE',(session_id,))
        # Do not permit a new customer to take over an existing pilot or Meta number.
        from .whatsapp import connection_configs
        if phone in connection_configs() or await(await conn.execute('SELECT 1 FROM meta_connections WHERE phone_id=%s UNION SELECT 1 FROM whatsapp_channels WHERE phone_id=%s',(phone,phone))).fetchone():
            raise ValueError('Number already reserved')
        await conn.execute('''INSERT INTO chakra_customer_connections(id,app_id,owner_id,plugin_id,phone_id,waba_id,config_cipher)
            VALUES(%s,%s,%s,%s,%s,%s,%s)''',(row['id'],row['app_id'],row['owner_id'],plugin,phone,waba,seal(json.dumps(config),context(row))))
        await conn.execute("UPDATE chakra_signup_sessions SET state='CONNECTED',token_cipher=NULL WHERE id=%s",(session_id,))


async def main():
    parser=argparse.ArgumentParser(description='Approve an independently verified Chakra customer binding')
    parser.add_argument('session_id',type=UUID)
    parser.add_argument('--phone-id',required=True)
    parser.add_argument('--waba-id',required=True)
    parser.add_argument('--ownership-verified',required=True,action='store_true')
    args=parser.parse_args()
    if not all(re.fullmatch(r'\d{5,30}',v) for v in (args.phone_id,args.waba_id)):
        raise ValueError('Invalid provider identifiers')
    async with AsyncConnectionPool(os.environ['AI_PLATFORM_DATABASE_URL'],kwargs={'row_factory':dict_row},open=False) as pool:
        await bind(pool,args.session_id,args.phone_id,args.waba_id)
    print('Customer binding saved; configure and verify the channel webhook before activation')


if __name__=='__main__':
    asyncio.run(main())
