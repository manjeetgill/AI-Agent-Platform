"""Separate migration, API and worker credentials; common settings contain no DB passwords."""
import argparse
import json
import os
import re
from pathlib import Path

DATABASE_KEYS={'AI_PLATFORM_DATABASE_URL','AI_PLATFORM_API_DATABASE_URL','AI_PLATFORM_WORKER_DATABASE_URL',
               'AI_PLATFORM_OPERATOR_DATABASE_URL','AI_PLATFORM_MIGRATION_DATABASE_URL','AGENT_DB_ADMIN_URL',
               'AGENT_DB_PASSWORD','POSTGRES_PASSWORD','PGPASSWORD'}


def process_env(values,url):
    return {**{k:v for k,v in values.items() if k not in DATABASE_KEYS},'AI_PLATFORM_DATABASE_URL':url}


def save(path,text):
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_TRUNC|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'w') as stream:
        os.fchmod(stream.fileno(),0o600)
        stream.write(text)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--config',type=Path,default=Path('.env'))
    parser.add_argument('--roles',type=Path,required=True)
    parser.add_argument('--output',type=Path,default=Path('.runtime/database'))
    args=parser.parse_args()
    if args.roles.stat().st_mode&0o077:
        raise SystemExit('Role credentials must have permissions 0600')
    roles=json.loads(args.roles.read_text())
    lines=[]
    for line in args.config.read_text().splitlines():
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        match=re.match(r'^([A-Z][A-Z0-9_]*)=',line)
        if not match:
            raise SystemExit('Use one KEY=value per line; multiline environment values are unsupported')
        if match[1] not in DATABASE_KEYS:
            lines.append(line)
    args.output.mkdir(parents=True,exist_ok=True,mode=0o700)
    save(args.output/'common.env','\n'.join(lines)+'\n')
    for kind,role in [('migrate','agent_migrator'),('api','agent_api'),('worker','agent_worker')]:
        # JSON quoting is a dotenv double-quoted value; no shell evaluation occurs.
        save(args.output/(kind+'.env'),'AI_PLATFORM_DATABASE_URL='+json.dumps(roles[role])+'\n')
    print('Separate private runtime environment files prepared.')


if __name__=='__main__':
    main()
