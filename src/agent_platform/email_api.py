"""Scoped transactional notification API; never returns recipient, OTP or email content."""
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from .automation import scope
from .email_config import api_key, config, digest, global_limits, keys
from .email_delivery import webhook_ready
from .email_service import PUBLIC, counts, enqueue
from .email_templates import TEMPLATES, EmailRequest
from .flags import enabled
from .service_identity import authenticate
from .task_api import credential_contract


def install(app):
    router=APIRouter(prefix='/v1/notifications/email',dependencies=[Depends(credential_contract)])

    async def context(request, permission):
        if not enabled('AI_EMAIL_NOTIFICATIONS'):
            raise HTTPException(503,'Email notifications are disabled')
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
                raise HTTPException(413,'Email request is too large')
        try:
            body=EmailRequest.model_validate_json(raw)
        except ValueError:
            raise HTTPException(422,'Invalid email request; check template fields, recipient and expiry. Sensitive values are not returned.') from None
        try:
            return await enqueue(pool,claims,body)
        except HTTPException:
            raise
        except ValueError:
            raise HTTPException(422,'Email template, sender configuration or private encryption settings are invalid. No fallback provider was used.') from None
        except Exception:  # noqa: BLE001 -- no raw database/crypto errors
            raise HTTPException(503,'Email queue unavailable; retry with the same idempotency key.') from None

    @router.get('/templates')
    async def templates(request: Request):
        await context(request,'notifications:read')
        return {'templates':[{'name':name,'version':1,**value} for name,value in TEMPLATES.items()],
                'category':'transactional','marketing_supported':False}

    @router.get('/configuration')
    async def configuration(request: Request):
        _,claims=await context(request,'notifications:read')
        try:
            settings=config(claims['app_id']);api_key(settings);keys();digest({})
            return {'configured':True,'provider':settings.provider,'mock':settings.provider=='mock',
                    'limits':settings.limits.model_dump(),'global_limits':global_limits().model_dump(),
                    'delivery_confirmation_supported':settings.provider in {'resend','brevo'},
                    'submission_meaning':'SMTP relay accepted the email; recipient delivery is unconfirmed' if settings.provider=='smtp' else None,
                    'webhook_configured':webhook_ready(claims['app_id'],settings.provider),
                    'delivery_meaning':'Recipient mail server accepted the email; reading and inbox placement are not confirmed','automatic_fallback':False}
        except (ValueError,TypeError,AttributeError):
            return {'configured':False,'next_action':'Operator: configure this app, sender, provider key reference and private encryption keys.'}

    @router.get('/usage')
    async def usage(request: Request):
        pool,claims=await context(request,'notifications:read')
        async with pool.connection() as conn, conn.transaction():
            await scope(conn,worker=True)
            requests=await counts(conn,'notification_email_jobs','created_at',claims['app_id'])
            attempts=await counts(conn,'notification_email_attempts','started_at',claims['app_id'])
            statuses=await (await conn.execute('''SELECT status,count(*) AS count FROM notification_email_jobs
                WHERE app_id=%s AND owner_id=%s GROUP BY status''',(claims['app_id'],claims['user']))).fetchall()
            deliveries=await (await conn.execute('SELECT delivery_status,count(*) AS count FROM notification_email_jobs WHERE app_id=%s AND owner_id=%s GROUP BY delivery_status',(claims['app_id'],claims['user']))).fetchall()
            suppressions=await (await conn.execute('SELECT count(*) AS count FROM notification_email_suppressions WHERE app_id=%s',(claims['app_id'],))).fetchone()
        try:
            limits=config(claims['app_id']).limits.model_dump()
        except (ValueError,TypeError,AttributeError):
            limits=None
        return {'app_requests':requests,'app_attempts':attempts,'user_statuses':{r['status']:r['count'] for r in statuses},
                'user_delivery_statuses':{r['delivery_status']:r['count'] for r in deliveries},
                'app_suppressed_recipients':suppressions['count'],
                'limits':limits,'timezone':'UTC','cost':None,
                'note':'Request and attempt reservations count against limits. Retries consume attempts. Counts are not provider billing or inbox-delivery confirmation.'}

    @router.get('')
    async def listing(request: Request,limit: int=Query(default=25,ge=1,le=100),before:UUID|None=None):
        pool,claims=await context(request,'notifications:read')
        async with pool.connection() as conn, conn.transaction():
            await scope(conn,claims)
            rows=await (await conn.execute(f'''SELECT {PUBLIC} FROM notification_email_jobs
                WHERE app_id=%s AND owner_id=%s AND (%s::uuid IS NULL OR (created_at,id)<
                (SELECT created_at,id FROM notification_email_jobs WHERE id=%s AND app_id=%s AND owner_id=%s))
                ORDER BY created_at DESC,id DESC LIMIT %s''',(claims['app_id'],claims['user'],before,before,claims['app_id'],claims['user'],limit+1))).fetchall()
            return {'emails':rows[:limit],'next_cursor':str(rows[limit-1]['id']) if len(rows)>limit else None}

    @router.get('/{email_id}')
    async def detail(email_id:UUID,request:Request):
        pool,claims=await context(request,'notifications:read')
        async with pool.connection() as conn,conn.transaction():
            await scope(conn,claims)
            row=await (await conn.execute(f'SELECT {PUBLIC} FROM notification_email_jobs WHERE id=%s AND app_id=%s AND owner_id=%s',
                (email_id,claims['app_id'],claims['user']))).fetchone()
            if not row:
                raise HTTPException(404,'Notification not found')
            row['history']=await (await conn.execute('''SELECT attempt,provider,started_at,finished_at,outcome,failure_code,http_status,provider_id
                FROM notification_email_attempts WHERE job_id=%s AND app_id=%s AND owner_id=%s ORDER BY attempt''',
                (email_id,claims['app_id'],claims['user']))).fetchall()
            row['delivery_events']=await (await conn.execute('SELECT provider,outcome,occurred_at,received_at FROM notification_email_events WHERE job_id=%s AND app_id=%s AND owner_id=%s ORDER BY occurred_at,id',(email_id,claims['app_id'],claims['user']))).fetchall()
            return row

    app.include_router(router)
    from .email_delivery import install as install_delivery
    install_delivery(app)
