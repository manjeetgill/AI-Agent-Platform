"""Disposable backup/restore drill for the isolated, synthetic local voice pilot only."""
import json
import os
import subprocess
import tempfile
from pathlib import Path
from uuid import uuid4

import psycopg
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.rows import dict_row
from psycopg.sql import SQL, Identifier

os.environ.setdefault('AGENT_LOCAL_PROFILE', 'voice')
if os.environ['AGENT_LOCAL_PROFILE'] != 'voice':
    raise SystemExit('Recovery drill is limited to the isolated voice profile')

from local import ROOT, load, pg

TABLES = ('web_voice_agents', 'web_voice_versions', 'web_voice_sessions', 'web_voice_events')


def counts(connection):
    return {name: connection.execute(SQL('SELECT count(*) AS n FROM {}').format(Identifier(name))).fetchone()['n'] for name in TABLES}


def main():
    source = load()['AI_PLATFORM_DATABASE_URL']
    info = conninfo_to_dict(source)
    if info.get('host') != '127.0.0.1' or info.get('port') != '55560':
        raise SystemExit('Refusing any database except the isolated loopback voice database')
    target = 'voice_restore_' + uuid4().hex[:12]
    env = {**os.environ, 'PGHOST': info['host'], 'PGPORT': info['port'],
           'PGUSER': info['user'], 'PGPASSWORD': info['password'], 'PGDATABASE': info['dbname']}
    with psycopg.connect(source, autocommit=True, row_factory=dict_row) as admin:
        if admin.execute("SELECT count(*) AS n FROM web_voice_sessions WHERE state IN ('STARTING','ACTIVE') AND expires_at>now()").fetchone()['n']:
            raise SystemExit('Finish active pilot calls before taking the comparison snapshot')
        expected = counts(admin)
        admin.execute(SQL('CREATE DATABASE {} TEMPLATE template0').format(Identifier(target)))
        try:
            # Private temporary backup is deleted on both success and failure; no customer export artifact.
            with tempfile.TemporaryDirectory(prefix='voice-recovery-') as folder:
                archive = Path(folder)/'backup.dump'
                subprocess.run([pg('pg_dump'), '--format=custom', '--no-owner', '--no-acl', '--file', str(archive)],
                    env=env, check=True, stdout=subprocess.DEVNULL)
                archive.chmod(0o600)
                subprocess.run([pg('pg_restore'), '--exit-on-error', '--no-owner', '--no-acl', '--dbname', target, str(archive)],
                    env=env, check=True, stdout=subprocess.DEVNULL)
            with psycopg.connect(make_conninfo(source, dbname=target), row_factory=dict_row) as restored:
                actual = counts(restored)
                assert actual == expected, 'Voice record counts differ after restore'
                # Restored versions must still resolve to the correct app/owner and session.
                invalid = restored.execute("""SELECT count(*) AS n FROM web_voice_sessions s
                    JOIN web_voice_agents a ON a.id=s.agent_id
                    WHERE a.app_id<>s.app_id OR
                    (s.agent_snapshot<>'{}'::jsonb AND (s.agent_snapshot->>'app_id')<>s.app_id::text)""").fetchone()['n']
                assert invalid == 0, 'Restored ownership does not match'
            report = {'status': 'PASS', 'scope': 'isolated synthetic voice database',
                      'tables': actual, 'ownership_mismatches': invalid,
                      'backup_deleted': True, 'external_secret_recovery_tested': False,
                      'media_resumption': False}
        finally:
            admin.execute(SQL('DROP DATABASE {} WITH (FORCE)').format(Identifier(target)))
    (ROOT/'docs/verification/voice-recovery.json').write_text(json.dumps(report, indent=2)+'\n')
    print(json.dumps(report))


if __name__ == '__main__':
    main()
