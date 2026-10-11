"""Private voice-media ASGI service. Public clients use the main platform API only."""
import hashlib
import os
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from psycopg.rows import dict_row
from pydantic import ValidationError

from . import calling_audio, media_contract, migrate, voice_config
from .db_scope import APIPool as AsyncConnectionPool
from .db_scope import assert_runtime_role, bound_identity
from .voice_runtime import VoiceRuntime


def audio_ready():
    try:
        import aiortc  # noqa: F401
        import webrtcvad  # noqa: F401
        voice_config.gateway()
        calling_audio.speech_key()
        calling_audio.speech_key('tts')
        return os.environ.get('AI_WEB_VOICE_ENABLED') == '1'
    except (ValueError, ImportError):
        return False


def create_media_app(pool=None):
    runtime = VoiceRuntime()
    draining = False
    started = time.monotonic()

    @asynccontextmanager
    async def lifespan(app):
        media_contract.key()  # Fail startup rather than serving unsigned control requests.
        own = pool is None
        app.state.pool = pool or AsyncConnectionPool(os.environ.get('AI_PLATFORM_API_DATABASE_URL') or os.environ['AI_PLATFORM_DATABASE_URL'],min_size=1,max_size=6,
            open=False,kwargs={'row_factory':dict_row,'connect_timeout':3,'options':'-c statement_timeout=5000'})
        if own:
            await app.state.pool.open()
            await app.state.pool.wait(timeout=10)
            await assert_runtime_role(app.state.pool, "api")
        app.state.runtime = runtime
        try:
            yield
        finally:
            await runtime.close()
            if own:
                await app.state.pool.close()

    app = FastAPI(title='Private voice media service',docs_url=None,redoc_url=None,openapi_url=None,lifespan=lifespan)

    @app.get('/health')
    async def health():
        return {'status':'up'}  # No configuration, credentials or session identities.

    @app.post('/internal/v1/readiness')
    async def readiness(request: Request):
        await media_contract.authenticate(request, request.app.state.pool)
        async with request.app.state.pool.connection() as conn:
            schema = await migrate.schema_ready(conn)
        if draining or not schema or not audio_ready():
            raise HTTPException(503, 'Media runtime not ready')
        return {'status':'ready','contract':'agent-platform-media-v1'}

    @app.post('/internal/v1/status')
    async def status(request: Request):
        await media_contract.authenticate(request, request.app.state.pool)
        return {'contract':'agent-platform-media-v1','draining':draining,
                'active_sessions':len(runtime.sessions),'uptime_seconds':int(time.monotonic()-started)}

    @app.post('/internal/v1/drain')
    async def drain(request: Request):
        nonlocal draining
        await media_contract.authenticate(request, request.app.state.pool)
        draining = True
        return {'draining':True,'active_sessions':len(runtime.sessions)}

    @app.post('/internal/v1/sessions',status_code=201)
    async def start(request: Request):
        db = request.app.state.pool
        payload = await media_contract.authenticate(request, db)
        if draining:
            raise HTTPException(503,'Media runtime draining')
        try:
            claim = media_contract.SessionClaim.model_validate(payload)
        except ValidationError:
            raise HTTPException(422, 'Invalid media claim') from None
        async with db.connection() as conn:
            authorized = await (await conn.execute('SELECT agent_media_identity(%s,%s,%s) AS claims', (claim.session_id, claim.app_id, claim.owner_id))).fetchone()
        if not authorized or not authorized['claims']:
            raise HTTPException(404, 'Session not found')
        with bound_identity(authorized['claims']):
            async with db.connection() as conn, conn.transaction():
                call = await (await conn.execute('SELECT * FROM web_voice_sessions WHERE id=%s AND app_id=%s AND owner_id=%s FOR UPDATE',
                    (claim.session_id,claim.app_id,claim.owner_id))).fetchone()
                if not call:
                    raise HTTPException(404,'Session not found')
                agent = await (await conn.execute('SELECT enabled,deleted_at FROM web_voice_agents WHERE id=%s AND app_id=%s', (call['agent_id'], claim.app_id))).fetchone()
                if (not call['media_remote'] or call['state']!='STARTING' or call['media_claimed_at'] or not agent or
                    not agent['enabled'] or agent['deleted_at'] or hashlib.sha256(claim.sdp.encode()).hexdigest()!=call['offer_hash']):
                    raise HTTPException(409,'Session cannot be claimed')
                allowed = await (await conn.execute("UPDATE web_voice_sessions SET media_claimed_at=now(),media_heartbeat=now(),runtime_id=%s WHERE id=%s AND expires_at>now() AND created_at>now()-interval '30 seconds' RETURNING id", (runtime.id,claim.session_id))).fetchone()
                if not allowed:
                    raise HTTPException(409,'Session claim expired')
            # Agent settings and consent are read from the reserved snapshot, never supplied by the peer.
            return await runtime.start(db,call['agent_snapshot'],call,claim.sdp)

    return app


app = create_media_app()
