"""Scoped transactional notification API; never returns recipient, OTP or sms content."""
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from .automation import scope
from .email_config import digest, keys
from .flags import enabled
from .service_identity import authenticate
from .sms_config import SMSRequest, api_key, config, global_limits
from .sms_service import PUBLIC, counts, enqueue
from .task_api import credential_contract


def install(app):
    router=APIRouter(prefix='/v1/notifications/sms',dependencies=[Depends(credential_contract)])

    async def context(request, permission):
        if not enabled('AI_SMS_NOTIFICATIONS'):
            raise HTTPException(503,'SMS notifications are disabled')
        pool=request.app.state.repository.pool
        claims=await authenticate(pool,request.headers.get('authorization'),request.headers.get('X-External-User-ID'),permission)
        return pool,claims

    @router.post('',status_code=202)
    async def send(request: Request):
        pool,claims=await context(request,'notifications:send')
        # Avoid FastAPI's standard validation response echoing input OTPs/tokens.
        raw=bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw)>16000:
                raise HTTPException(413,'SMS request is too large')
        try:
            body=SMSRequest.model_validate_json(raw)
        except ValueError:
            raise HTTPException(422,'Invalid sms request; check template fields, recipient and expiry. Sensitive values are not returned.') from None
        try:
            return await enqueue(pool,claims,body)
        except HTTPException:
            raise
        except ValueError:
            raise HTTPException(422,'SMS template, sender configuration or private encryption settings are invalid. No fallback provider was used.') from None
        except Exception:  # noqa: BLE001 -- no raw database/crypto errors
            raise HTTPException(503,'SMS queue unavailable; retry with the same idempotency key.') from None

    @router.get('/templates')
    async def templates(request: Request):
        _,claims=await context(request,'notifications:read')
        try:
            settings=config(claims['app_id'])
            return {'templates':[{'name':name, 'version':1, 'variables':sorted(t.variables)}
                for name,t in settings.templates.items()], 'category':'transactional', 'marketing_supported':False}
        except (ValueError,TypeError):
            return {'templates':[], 'next_action':'Operator: configure SMS templates for this application.'}

    @router.get('/configuration')
    async def configuration(request: Request):
        _,claims=await context(request,'notifications:read')
        try:
            settings=config(claims['app_id']);api_key(settings);keys();digest({})
            return {'configured':True, 'provider':settings.provider, 'mock':settings.provider=='mock',
                    'limits':settings.limits.model_dump(), 'global_limits':global_limits().model_dump(),
                    'limit_unit':'SMS segments', 'max_segments':settings.max_segments,
                    'delivery_confirmation_supported':False, 'automatic_fallback':False,
                    'next_action':'Run a mock queue test.' if settings.provider=='mock' else
                        'Confirm provider authorisation and credits, then request one recipient-approved receipt test.',
                    'delivery_meaning':'Provider acceptance is not handset delivery. Automated delivery receipts are not configured.'}
        except (ValueError,TypeError,AttributeError):
            return {'configured':False,'next_action':'Operator: configure this app, SMSIndiaHub key, sender header, PE ID, approved templates and private encryption keys.'}

    @router.get('/usage')
    async def usage(request: Request):
        pool,claims=await context(request,'notifications:read')
        async with pool.connection() as conn, conn.transaction():
            await scope(conn,worker=True)
            requests=await counts(conn,'notification_sms_jobs','created_at',claims['app_id'])
            attempts=await counts(conn,'notification_sms_attempts','started_at',claims['app_id'])
            statuses=await (await conn.execute('''SELECT status,count(*) AS count FROM notification_sms_jobs
                WHERE app_id=%s AND owner_id=%s GROUP BY status''',(claims['app_id'],claims['user']))).fetchall()
        try:
            limits=config(claims['app_id']).limits.model_dump()
        except (ValueError,TypeError,AttributeError):
            limits=None
        return {'app_requests':requests, 'app_attempts':attempts, 'user_statuses':{r['status']:r['count'] for r in statuses},
                'limits':limits, 'timezone':'UTC', 'unit':'SMS segments', 'cost':None,
                'note':'Counts reserve segments for requests and attempts. Retries consume limits; these are not provider billing totals.'}

    @router.get('')
    async def listing(request: Request,limit: int=Query(default=25,ge=1,le=100),before:UUID|None=None):
        pool,claims=await context(request,'notifications:read')
        async with pool.connection() as conn, conn.transaction():
            await scope(conn,claims)
            rows=await (await conn.execute(f'''SELECT {PUBLIC} FROM notification_sms_jobs
                WHERE app_id=%s AND owner_id=%s AND (%s::uuid IS NULL OR (created_at,id)<
                (SELECT created_at,id FROM notification_sms_jobs WHERE id=%s AND app_id=%s AND owner_id=%s))
                ORDER BY created_at DESC,id DESC LIMIT %s''',(claims['app_id'],claims['user'],before,before,claims['app_id'],claims['user'],limit+1))).fetchall()
            return {'messages':rows[:limit],'next_cursor':str(rows[limit-1]['id']) if len(rows)>limit else None}

    @router.get('/{sms_id}')
    async def detail(sms_id:UUID,request:Request):
        pool,claims=await context(request,'notifications:read')
        async with pool.connection() as conn,conn.transaction():
            await scope(conn,claims)
            row=await (await conn.execute(f'SELECT {PUBLIC} FROM notification_sms_jobs WHERE id=%s AND app_id=%s AND owner_id=%s',
                (sms_id,claims['app_id'],claims['user']))).fetchone()
            if not row:
                raise HTTPException(404,'Notification not found')
            row['history']=await (await conn.execute('''SELECT attempt,provider,started_at,finished_at,outcome,failure_code,http_status,provider_id,provider_job_id,segments
                FROM notification_sms_attempts WHERE job_id=%s AND app_id=%s AND owner_id=%s ORDER BY attempt''',
                (sms_id,claims['app_id'],claims['user']))).fetchall()
            return row

    app.include_router(router)
