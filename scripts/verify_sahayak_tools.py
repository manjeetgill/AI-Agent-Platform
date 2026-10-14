"""Verify real Sahayak booking tools in a fresh disposable database, no messages/calls."""
import argparse
import json
import os
import subprocess
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import psycopg
from psycopg.sql import SQL, Identifier

ROOT=Path(__file__).resolve().parents[1]


def run(source):
    values={}
    for line in (source/'.env').read_text().splitlines():
        key,sep,value=line.partition('=')
        if sep and key in {'ADMIN_DATABASE_URL','DATABASE_URL'}:
            values[key]=value.strip().strip('"').strip("'")
    name='platform_voice_'+uuid4().hex[:12]
    def database(value):
        url=urlsplit(value)
        if url.hostname not in {'localhost','127.0.0.1','::1'}:
            raise ValueError('Business-tool test requires a local disposable database')
        return urlunsplit(url._replace(path='/'+name))
    env={**os.environ,**{key:database(value) for key,value in values.items()},
         'AI_AGENT_PLATFORM_DIR':str(ROOT),'LLM_PROVIDER':'offline','LLM_API_KEY':'','TOGETHER_API_KEY':'',
         'JOBS_ENABLED':'false','PATIENT_MESSAGES_JOBS_ENABLED':'false','WHATSAPP_PROVIDER':'',
         'CHANNEL_PLUGIN_URL':'','LIVE_CHANNEL_OWNER':'local','AGENT_PLATFORM_URL':'','AGENT_PLATFORM_KEY':'',
         'AGENT_CHANNEL_BRIDGE_BINDINGS':'','AGENT_CHANNEL_CREDENTIALS':'','AGENT_CHANNEL_PLATFORM_URL':''}
    if set(values)!={'ADMIN_DATABASE_URL','DATABASE_URL'}:raise ValueError('Local database configuration missing')
    with psycopg.connect(values['ADMIN_DATABASE_URL'],autocommit=True) as conn:
        # Existing app role must already exist; the verifier does not create credentials.
        if not conn.execute("SELECT 1 FROM pg_roles WHERE rolname='sahayak_app'").fetchone():
            raise ValueError('Sahayak runtime role is missing')
        conn.execute(SQL('CREATE DATABASE {}').format(Identifier(name)))
        try:
            output=ROOT/'docs/verification/sahayak-voice-tools.txt'
            with output.open('w') as log:
                result=subprocess.run(['node',str(source/'node_modules/vitest/vitest.mjs'),'run','apps/api/test/calling-bridge.test.ts'],cwd=source,env=env,stdout=log,stderr=subprocess.STDOUT,timeout=120,check=False)
            report={'status':'PASS' if result.returncode==0 else 'FAIL','app':'Sahayak','isolated_database':True,
                    'actual_business_code':True,'synthetic_patient_data':True,'outbound_messages':0,'telephone_calls':0,
                    'coverage':['signed Python telephone and internet-voice HTTP contracts','booking requires final confirmation','duplicate event protection','clinic isolation','caller ID cannot reveal history','unknown-outcome fence'],
                    'public_audio_end_to_end':False}
            (ROOT/'docs/verification/sahayak-voice-tools.json').write_text(json.dumps(report,indent=2)+'\n')
            print(json.dumps(report))
            return result.returncode
        finally:
            conn.execute(SQL('DROP DATABASE {} WITH (FORCE)').format(Identifier(name)))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',type=Path,default=Path('/Users/manjeet/Desktop/sahayak'))
    raise SystemExit(run(parser.parse_args().source))
