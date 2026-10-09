"""App-owned browser pairing. Device secrets cannot access other platform APIs."""
import hashlib
import hmac
import secrets
from uuid import UUID, uuid4

from fastapi import APIRouter, Header, HTTPException, Request
from psycopg.types.json import Jsonb
from pydantic import Field

from .automation import emit, scope
from .flags import enabled
from .retailer_browser import TOOL, Capture, Retailer, as_probe, job_allowed
from .service_identity import authenticate
from .task_store import TaskStore
from .tasks import Strict, TaskError


class Pair(Strict):
    retailers: list[Retailer] = Field(min_length=1,max_length=4)


class Poll(Strict):
    retailers: list[Retailer] = Field(default_factory=list,max_length=4)


def install(app):
    router=APIRouter(prefix='/v1/retailer-browser')

    def pool_for(request):
        if not enabled('AI_RETAILER_BROWSER') or not enabled('AI_AUTOMATIONS'):
            raise HTTPException(503,'Retailer browser is disabled')
        pool=getattr(request.app.state.repository,'pool',None)
        if pool is None:raise HTTPException(503,'Durable storage unavailable')
        return pool

    async def claims_for(request,authorization):
        pool=pool_for(request)
        claims=await authenticate(pool,authorization,request.headers.get('X-External-User-ID'),'tasks:write')
        async with pool.connection() as conn:
            if not await (await conn.execute('SELECT 1 FROM platform_tool_grants WHERE app_id=%s AND tool=%s',(claims['app_id'],TOOL))).fetchone():
                raise HTTPException(403,'Retailer tool is not granted')
        return pool,claims

    async def device_for(request,authorization):
        pool=pool_for(request)
        try:
            if not (authorization or '').startswith('Bearer rbr_'):raise ValueError()
            identifier,secret=authorization.removeprefix('Bearer rbr_').split('.')
            identifier=UUID(identifier)
            if len(secret)!=43:raise ValueError()
        except ValueError:
            raise HTTPException(401,'Pair this browser again') from None
        async with pool.connection() as conn,conn.transaction():
            await scope(conn,worker=True)
            device=await (await conn.execute('''SELECT d.* FROM retailer_browser_devices d
                JOIN platform_service_keys k ON k.id=d.key_id AND k.app_id=d.app_id
                WHERE d.id=%s AND d.revoked_at IS NULL AND d.expires_at>now()
                AND k.revoked_at IS NULL AND k.expires_at>now() AND 'tasks:write'=ANY(k.scopes)
                AND EXISTS(SELECT 1 FROM platform_tool_grants g WHERE g.app_id=d.app_id AND g.tool=%s)''',(identifier,TOOL))).fetchone()
            if not device or not hmac.compare_digest(device['secret_hash'],hashlib.sha256(secret.encode()).hexdigest()):
                raise HTTPException(401,'Browser pairing expired or revoked')
            await conn.execute('UPDATE retailer_browser_devices SET last_seen_at=now() WHERE id=%s',(identifier,))
        return pool,device

    @router.post('/devices',status_code=201)
    async def pair(body:Pair,request:Request,authorization:str|None=Header(default=None)):
        pool,claims=await claims_for(request,authorization)
        identifier,secret=uuid4(),secrets.token_urlsafe(32)
        async with pool.connection() as conn,conn.transaction():
            await scope(conn,claims)
            await conn.execute('SELECT id FROM platform_apps WHERE id=%s FOR UPDATE',(claims['app_id'],))
            # One active device per app/user avoids accidental parallel browsers.
            await conn.execute('UPDATE retailer_browser_devices SET revoked_at=now() WHERE app_id=%s AND owner_id=%s AND revoked_at IS NULL',(claims['app_id'],claims['user']))
            await conn.execute('''INSERT INTO retailer_browser_devices(id,tenant_id,app_id,owner_id,key_id,secret_hash,retailers)
                VALUES(%s,%s,%s,%s,%s,%s,%s)''',(identifier,claims['tenant'],claims['app_id'],claims['user'],UUID(claims['principal'].removeprefix('service:')),hashlib.sha256(secret.encode()).hexdigest(),sorted(set(body.retailers))))
            await emit(conn,claims,'RetailerBrowserPaired',{'device_id':str(identifier),'retailers':sorted(set(body.retailers))},key='browser-pair:'+str(identifier),correlation=str(identifier))
        return {'device_id':str(identifier),'pairing_code':f'rbr_{identifier}.{secret}','expires_in_hours':24,'retailers':sorted(set(body.retailers))}

    @router.get('/devices')
    async def devices(request:Request,authorization:str|None=Header(default=None)):
        pool,claims=await claims_for(request,authorization)
        async with pool.connection() as conn,conn.transaction():
            await scope(conn,claims)
            rows=await (await conn.execute('SELECT id,retailers,expires_at,revoked_at,last_seen_at FROM retailer_browser_devices WHERE app_id=%s AND owner_id=%s ORDER BY expires_at DESC LIMIT 20',(claims['app_id'],claims['user']))).fetchall()
        return {'devices':rows}

    @router.delete('/devices/{device_id}')
    async def revoke(device_id:UUID,request:Request,authorization:str|None=Header(default=None)):
        pool,claims=await claims_for(request,authorization)
        async with pool.connection() as conn,conn.transaction():
            await scope(conn,claims)
            await conn.execute('UPDATE retailer_browser_devices SET revoked_at=now() WHERE id=%s AND app_id=%s AND owner_id=%s',(device_id,claims['app_id'],claims['user']))
        return {'revoked':True}

    @router.post('/poll')
    async def poll(body:Poll,request:Request,authorization:str|None=Header(default=None)):
        pool,device=await device_for(request,authorization)
        selected=sorted(set(body.retailers)&set(device['retailers']))
        async with pool.connection() as conn,conn.transaction():
            await scope(conn,worker=True)
            live=await (await conn.execute('SELECT id FROM retailer_browser_devices WHERE id=%s AND revoked_at IS NULL AND expires_at>now() FOR UPDATE',(device['id'],))).fetchone()
            if not live:raise HTTPException(401,'Browser pairing revoked')
            if await (await conn.execute("SELECT 1 FROM retailer_browser_jobs WHERE device_id=%s AND state='STARTED' AND expires_at>now()",(device['id'],))).fetchone():
                return {'command':None}
            rows=await (await conn.execute('''SELECT j.* FROM retailer_browser_jobs j
                WHERE j.app_id=%s AND j.owner_id=%s AND j.state='PENDING' AND j.executor='paired_browser' AND j.expires_at>now()
                AND j.source=ANY(%s) ORDER BY j.created_at FOR UPDATE SKIP LOCKED LIMIT 10''',(device['app_id'],device['owner_id'],selected))).fetchall()
            for job in rows:
                if not await job_allowed(conn,job):continue
                await conn.execute("UPDATE retailer_browser_jobs SET state='STARTED',device_id=%s,started_at=now() WHERE id=%s",(device['id'],job['id']))
                return {'command':{'id':str(job['id']),'operation':'retailer.observe','expires_at':job['expires_at'].isoformat(),**job['arguments']}}
        return {'command':None}

    async def active_job(conn,device,identifier):
        if not await (await conn.execute('SELECT id FROM retailer_browser_devices WHERE id=%s AND revoked_at IS NULL AND expires_at>now() FOR UPDATE',(device['id'],))).fetchone():
            raise HTTPException(401,'Browser pairing revoked')
        job=await (await conn.execute("SELECT * FROM retailer_browser_jobs WHERE id=%s AND device_id=%s AND app_id=%s AND owner_id=%s AND state='STARTED' AND expires_at>now() FOR UPDATE",(identifier,device['id'],device['app_id'],device['owner_id']))).fetchone()
        if not job or not await job_allowed(conn,job):raise HTTPException(409,'Browser job expired, paused or revoked')
        return job

    @router.post('/{job_id}/authorize')
    async def authorize(job_id:UUID,request:Request,authorization:str|None=Header(default=None)):
        pool,device=await device_for(request,authorization)
        async with pool.connection() as conn,conn.transaction():
            await scope(conn,worker=True)
            await active_job(conn,device,job_id)
        return {'authorized':True}

    @router.post('/{job_id}/result')
    async def result(job_id:UUID,body:Capture,request:Request,authorization:str|None=Header(default=None)):
        pool,device=await device_for(request,authorization)
        async with pool.connection() as conn,conn.transaction():
            await scope(conn,worker=True)
            job=await active_job(conn,device,job_id)
            try:probe=as_probe(job,body)
            except (ValueError,TypeError):raise HTTPException(422,'Invalid browser observation') from None
            await conn.execute("UPDATE retailer_browser_jobs SET state='DONE',result=%s WHERE id=%s",(Jsonb(probe.model_dump()),job_id))
        return {'recorded':True}

    @router.post('/stop')
    async def stop(request:Request,authorization:str|None=Header(default=None)):
        pool,device=await device_for(request,authorization)
        async with pool.connection() as conn,conn.transaction():
            await scope(conn,worker=True)
            await conn.execute('UPDATE retailer_browser_devices SET revoked_at=now() WHERE id=%s',(device['id'],))
            jobs=await (await conn.execute("SELECT DISTINCT task_id FROM retailer_browser_jobs WHERE app_id=%s AND owner_id=%s AND executor='paired_browser' AND state!='DONE' AND expires_at>now()",(device['app_id'],device['owner_id']))).fetchall()
        for job in jobs:
            try:await TaskStore(pool,principal='browser:'+str(device['id'])).control(device['tenant_id'],device['owner_id'],job['task_id'],'pause')
            except TaskError:pass
        return {'paused':True,'pairing_revoked':True}

    app.include_router(router)
