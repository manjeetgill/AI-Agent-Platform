"""Configure one existing app for mock SMS or SMSIndiaHub. Never sends a message."""
import argparse
import base64
import fcntl
import getpass
import json
import os
import secrets
import sys
from pathlib import Path
from uuid import UUID

from local import CONFIG, ROOT, STATE, load

sys.path.insert(0,str(ROOT/'src'))
from agent_platform.email_config import Limits, digest, keys
from agent_platform.sms_config import Config


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--app-id',required=True,type=UUID)
    parser.add_argument('--provider',choices=['mock','smsindiahub'],default='mock')
    parser.add_argument('--templates',required=True,type=Path,help='Template JSON; exact approved DLT text for live sending')
    parser.add_argument('--sender-id')
    parser.add_argument('--pe-id')
    parser.add_argument('--daily',type=int,default=10)
    parser.add_argument('--monthly',type=int,default=100)
    parser.add_argument('--per-minute',type=int,default=5)
    parser.add_argument('--max-segments',type=int,default=1)
    args=parser.parse_args()
    app=str(args.app_id)
    key_env='AI_SMS_KEY_'+args.app_id.hex.upper() if args.provider=='smsindiahub' else None
    settings=Config(provider=args.provider,key_env=key_env,sender_id=args.sender_id,pe_id=args.pe_id,
                    templates=json.loads(args.templates.read_text()),max_segments=args.max_segments,
                    limits=Limits(daily=args.daily,monthly=args.monthly,per_minute=args.per_minute))
    import psycopg
    # Existence checked with the local worker role, never a public caller-selected DB URL.
    roles=json.loads((STATE/'database-roles.json').read_text())
    with psycopg.connect(roles['agent_worker']) as conn:
        conn.execute("SELECT set_config('agent.worker','on',true)")
        if not conn.execute('SELECT id FROM platform_apps WHERE id=%s',(app,)).fetchone():
            raise ValueError('Register the app first')
    key=None
    if key_env:
        if not sys.stdin.isatty():
            raise ValueError('Enter live credentials privately in a terminal')
        print('Live route selected. Charges may apply after restart and an authorised send request.')
        key=getpass.getpass('SMSIndiaHub API key (hidden): ').strip()
        if not key or any(c in key for c in '\r\n'):
            raise ValueError('Invalid provider credential')
    descriptor=os.open(str(CONFIG)+'.sms.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
    temporary=CONFIG.with_name('.sms-config-'+secrets.token_hex(8)+'.json')
    try:
        fcntl.flock(descriptor,fcntl.LOCK_EX)
        values=load()
        names=['AI_NOTIFICATION_ENCRYPTION_KEY_ID','AI_NOTIFICATION_ENCRYPTION_KEYS','AI_NOTIFICATION_HASH_KEY']
        if not any(values.get(n) for n in names):
            values.update(AI_NOTIFICATION_ENCRYPTION_KEY_ID='initial',
                AI_NOTIFICATION_ENCRYPTION_KEYS=json.dumps({'initial':base64.b64encode(secrets.token_bytes(32)).decode()}),
                AI_NOTIFICATION_HASH_KEY=base64.b64encode(secrets.token_bytes(32)).decode())
        os.environ.update(values)
        keys(); digest({})  # Never replace an existing or partial keyring.
        routes=json.loads(values.get('AI_SMS_APPS','{}'))
        routes[app]=settings.model_dump(exclude_none=True)
        values['AI_SMS_APPS']=json.dumps(routes)
        values['AI_SMS_NOTIFICATIONS']='1'
        if key_env:
            values[key_env]=key
        fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
        with os.fdopen(fd,'w') as stream:
            json.dump(values,stream,indent=2); stream.write('\n'); stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary,CONFIG)
    finally:
        os.close(descriptor)
        temporary.unlink(missing_ok=True)
    print('Saved privately for this app. Restart API and worker together. No SMS sent.')
    print('Grant notifications:send/read to the consuming backend and open /monitor/sms.')


if __name__=='__main__':
    try:
        main()
    except Exception:  # noqa: BLE001 -- private config/provider input never appears in errors
        raise SystemExit('SMS setup failed. Check app registration, role file, templates, DLT settings and encryption configuration. No SMS sent.') from None
