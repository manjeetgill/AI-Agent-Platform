"""Bounded, real local audio load test across two isolated apps. No carrier/paid API.

Creates labelled synthetic apps, holds scoped credentials only in memory, and
revokes keys at completion. Keeps fixture audit records for verification.
"""
import argparse
import asyncio
import json
import math
import os
import time
from pathlib import Path
from uuid import uuid4

import httpx
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from verify_voice_local import main as audio_call

from agent_platform import service_identity, voice_config

ROOT=Path(__file__).resolve().parents[1]


async def run(waves):
    os.environ.update(json.loads((ROOT/'.runtime/voice/config.json').read_text()))
    cfg=voice_config.defaults()
    if (cfg.asr,cfg.tts,cfg.reasoning)!=('local','local','local'):
        raise ValueError('Load test permits only local providers')
    started=time.monotonic()
    keys=[]
    results=[]
    overload=[]
    async with AsyncConnectionPool(os.environ['AI_PLATFORM_DATABASE_URL'],open=False,kwargs={'row_factory':dict_row}) as pool:
        try:
            async with pool.connection() as conn:
                active=await (await conn.execute("SELECT count(*) n FROM web_voice_sessions WHERE state IN ('STARTING','ACTIVE') AND expires_at>now()")).fetchone()
                if active['n']:
                    raise ValueError('Wait until existing voice calls end before load testing')
            async def capacity_probe():
                deadline=time.monotonic()+25
                while time.monotonic()<deadline:
                    async with pool.connection() as conn:
                        active=await (await conn.execute("SELECT agent_id FROM web_voice_sessions WHERE app_id=%s AND state='ACTIVE' AND expires_at>now()",(keys[0]['app_id'],))).fetchone()
                        count=await (await conn.execute("SELECT count(*) n FROM web_voice_sessions WHERE state='ACTIVE' AND expires_at>now() AND app_id=ANY(%s::uuid[])",([k['app_id'] for k in keys],))).fetchone()
                    if active and count['n']==2:
                        async with httpx.AsyncClient(timeout=10) as client:
                            response=await client.post('http://127.0.0.1:4160/v1/voice/agents/'+str(active['agent_id'])+'/sessions',headers=headers[0],json={
                                'sdp':'v=0\r\n'+'fixture '*5,'request_key':uuid4().hex,'audio_consent':True})
                            return response.status_code==429
                    await asyncio.sleep(.2)
                return False
            for index in range(2):
                keys.append(await service_identity.provision(pool,tenant='voice-load-'+uuid4().hex,
                    organisation='Synthetic voice load test',app_name=f'Voice load fixture {index}',days=1,
                    scopes=['agents:read','agents:write','agents:run','knowledge:read','knowledge:write','knowledge:publish']))
            headers=[{'Authorization':'Bearer '+k['credential'],'X-External-User-ID':'synthetic-caller'} for k in keys]
            for wave in range(waves):
                combined=await asyncio.gather(*(audio_call(streaming=True,api_url='http://127.0.0.1:4160/v1/',headers=h,save_report=False) for h in headers),capacity_probe(),return_exceptions=True)
                pair=combined[:2]
                overload.append(combined[2] is True)
                for index,value in enumerate(pair):
                    if isinstance(value,BaseException):
                        results.append({'wave':wave+1,'app_index':index,'status':'FAIL','error_type':type(value).__name__,
                            'assertion':str(value)[:200] if isinstance(value,AssertionError) else None})
                    else:
                        results.append({'wave':wave+1,'app_index':index,**{k:v for k,v in value.items() if k not in {'events','spoken_reply_transcription'}}})
                if any(isinstance(value,BaseException) for value in pair):
                    break  # No blind retries or extra pressure after a failed wave.
            isolation=[]
            async with httpx.AsyncClient(base_url='http://127.0.0.1:4160/v1/',timeout=10) as client:
                for result in results:
                    if result['status']=='PASS':
                        response=await client.get('voice/sessions/'+result['session_id'],headers=headers[1-result['app_index']])
                        isolation.append(response.status_code==404)
            times=sorted(r['last_input_frame_to_first_received_audio_ms'] for r in results if r['status']=='PASS')
            def percentile(p):
                return times[max(0,math.ceil(len(times)*p)-1)] if times else None
            report={'status':'PASS' if len(results)==waves*2 and all(r['status']=='PASS' for r in results) and all(isolation) and all(overload) else 'FAIL',
                'synthetic_audio':True,'human_microphone_tested':False,'public_network_tested':False,
                'concurrent_callers':2,'waves':waves,'duration_seconds':round(time.monotonic()-started,2),
                'first_reply_audio_ms':{'p50':percentile(.5),'p95':percentile(.95),'p99':percentile(.99)},
                'sample_warning':'Small local capacity sample, not a production SLO or soak certification.',
                'overload_rejected':all(overload),'cross_app_reads_denied':all(isolation) and bool(isolation),'runs':results}
            (ROOT/'docs/verification/voice-load.json').write_text(json.dumps(report,indent=2)+'\n')
            print(json.dumps(report,indent=2))
            return report
        finally:
            for key in keys:
                async with pool.connection() as conn:
                    await conn.execute('UPDATE platform_service_keys SET revoked_at=now() WHERE id=%s',(key['key_id'],))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--waves',type=int,choices=range(1,6),default=3)
    args=parser.parse_args()
    raise SystemExit(0 if asyncio.run(run(args.waves))['status']=='PASS' else 1)
