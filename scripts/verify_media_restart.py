"""Real synthetic audio survives a control-plane restart. Does not restart the user's API."""
import asyncio
import json
import os
import socket
import sys
from pathlib import Path

import httpx
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from verify_voice_local import main as audio_call

from agent_platform import media_client, service_identity
from agent_platform.db_scope import worker_connection

ROOT=Path(__file__).resolve().parents[1]


async def run(log):
    values=json.loads((ROOT/'.runtime/voice/runtime-config.json').read_text())
    os.environ.update(values)
    if values.get('AI_VOICE_MEDIA_URL')!='http://127.0.0.1:4162':
        raise ValueError('Use isolated media pilot only')
    await media_client.check_ready()
    with socket.socket() as probe:
        probe.bind(('127.0.0.1',4190))  # Never replace another listener.
    process=None
    key=None
    roles=json.loads((ROOT/'.runtime/voice/database-roles.json').read_text())
    async with AsyncConnectionPool(roles['agent_worker'],open=False,configure=worker_connection,kwargs={'row_factory':dict_row}) as pool:
        async def launch():
            child=await asyncio.create_subprocess_exec(sys.executable,'-m','uvicorn','agent_platform.app:app','--host','127.0.0.1','--port','4190','--no-access-log',
                cwd=ROOT,env={**os.environ,**values},stdout=log,stderr=log)
            async with httpx.AsyncClient(timeout=1) as client:
                for _ in range(50):
                    try:
                        if (await client.get('http://127.0.0.1:4190/readiness')).status_code==200:
                            return child
                    except httpx.HTTPError:
                        pass
                    await asyncio.sleep(.1)
            child.terminate();await child.wait()
            raise RuntimeError('Test API did not become ready')
        try:
            key=await service_identity.provision(pool,tenant='voice-restart-fixture',organisation='Synthetic restart verification',app_name='Synthetic restart verification',days=1,
                scopes=['agents:read','agents:write','agents:run','knowledge:read','knowledge:write','knowledge:publish'])
            headers={'Authorization':'Bearer '+key['credential'],'X-External-User-ID':'synthetic-caller'}
            process=await launch()
            async def restart(session):
                nonlocal process
                process.terminate();await process.wait()
                async with pool.connection() as conn:
                    row=await (await conn.execute('SELECT state,media_connected FROM web_voice_sessions WHERE id=%s',(session,))).fetchone()
                    assert row['state']=='ACTIVE' and row['media_connected']
                process=await launch()
                async with httpx.AsyncClient(timeout=5) as client:
                    response=await client.get('http://127.0.0.1:4190/v1/voice/sessions/'+session,headers=headers)
                    assert response.status_code==200 and response.json()['audio_connected']
            result=await audio_call(streaming=True,api_url='http://127.0.0.1:4190/v1/',headers=headers,save_report=False,after_connected=restart)
            report={k:v for k,v in result.items() if k not in {'events','spoken_reply_transcription'}}
            report.update(control_plane_restarted=True,media_process_restarted=False,original_api_restarted=False)
            (ROOT/'docs/verification/voice-media-restart.json').write_text(json.dumps(report,indent=2)+'\n')
            print(json.dumps(report,indent=2))
        finally:
            if process and process.returncode is None:
                process.terminate();await process.wait()
            if key:
                async with pool.connection() as conn:
                    await conn.execute('UPDATE platform_service_keys SET revoked_at=now() WHERE id=%s',(key['key_id'],))


if __name__=='__main__':
    with (ROOT/'.runtime/voice/restart-verification.log').open('wb') as logfile:
        asyncio.run(run(logfile))
