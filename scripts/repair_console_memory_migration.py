"""Reconcile only the known pre-alert migration 030 in the private local console DB.

The original source differs only by the allowed alerts:send scope. Back up first,
apply that exact DDL difference, and record the verified current checksum. Never
accept an arbitrary changed migration or apply this to another database.
"""
import hashlib
import json
import os
import subprocess
from datetime import UTC, datetime
from urllib.parse import unquote, urlsplit, urlunsplit

import psycopg
from local import ROOT, load, pg

OLD = 'a2b085a88711c8335757895b1fd1295d06868838812c3a195fddaa4eefb8455a'
NAME = '030_validated_memory.sql'
source = (ROOT/'src/agent_platform/migrations'/NAME).read_text()
if hashlib.sha256(source.replace(",'alerts:send'", '').encode()).hexdigest() != OLD:
    raise SystemExit('Source differs from the reviewed scope-only change; stopped')
new = hashlib.sha256(source.encode()).hexdigest()
parts = urlsplit(load()['AI_PLATFORM_DATABASE_URL'])._replace(path='/agent_console')
url = urlunsplit(parts)
with psycopg.connect(url) as conn:
    prior = conn.execute('SELECT sha256 FROM agent_schema_migrations WHERE name=%s', (NAME,)).fetchone()
    if prior == (new,):
        print('Known reconciliation already applied.')
        raise SystemExit(0)
    if prior != (OLD,):
        raise SystemExit('Console migration is not the reviewed legacy variant; stopped')
folder = ROOT/'.runtime/local/backups'
folder.mkdir(mode=0o700, parents=True, exist_ok=True)
backup = folder/('console-before-feed-'+datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')+'.dump')
env = {**os.environ, 'PGHOST': parts.hostname, 'PGPORT': str(parts.port or 5432),
       'PGDATABASE': 'agent_console', 'PGUSER': unquote(parts.username or ''),
       'PGPASSWORD': unquote(parts.password or '')}
with os.fdopen(os.open(backup, os.O_CREAT|os.O_EXCL|os.O_WRONLY, 0o600), 'wb') as out:
    result = subprocess.run([pg('pg_dump'), '--format=custom'], env=env, stdout=out, stderr=subprocess.PIPE, check=False)
if result.returncode or backup.stat().st_size == 0:
    raise SystemExit('Private console backup failed; database unchanged')
with psycopg.connect(url) as conn, conn.transaction():
    conn.execute('SELECT pg_advisory_xact_lock(79342011)')
    prior = conn.execute('SELECT sha256 FROM agent_schema_migrations WHERE name=%s FOR UPDATE', (NAME,)).fetchone()
    if prior != (OLD,):
        raise SystemExit('Migration changed during backup; stopped')
    # Only the reviewed two statements; all memory tables/data remain untouched.
    conn.execute(source[source.index('ALTER TABLE platform_service_keys'):source.index('ALTER TABLE agent_memory')])
    conn.execute('UPDATE agent_schema_migrations SET sha256=%s WHERE name=%s AND sha256=%s', (new, NAME, OLD))
receipt = backup.with_suffix('.json')
with os.fdopen(os.open(receipt, os.O_CREAT|os.O_EXCL|os.O_WRONLY, 0o600), 'w') as out:
    json.dump({'database':'agent_console', 'migration':NAME, 'before':OLD, 'after':new,
               'change':'Add alerts:send to allowed scopes; existing key grants unchanged', 'backup':str(backup)}, out)
print('Known scope-only migration reconciled; private backup and receipt saved.')
