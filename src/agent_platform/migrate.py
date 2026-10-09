"""Explicit, checksum-verified migrations for the platform-owned database."""
import asyncio
import hashlib
import os
from pathlib import Path

import psycopg

MIGRATIONS = Path(__file__).with_name('migrations')


def manifest():
    files = sorted(MIGRATIONS.glob('[0-9]*.sql'))
    return {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in files}


async def schema_ready(connection):
    expected = manifest()
    if not expected:
        return False
    rows = await (await connection.execute('SELECT name,sha256 FROM agent_schema_migrations')).fetchall()
    applied = {row['name']: row['sha256'] for row in rows}
    return all(applied.get(name) == digest for name, digest in expected.items())


async def migrate():
    url = os.environ.get('AI_PLATFORM_DATABASE_URL', '')
    if not url:
        raise RuntimeError('AI_PLATFORM_DATABASE_URL is required')
    async with await psycopg.AsyncConnection.connect(url, connect_timeout=5) as connection, connection.transaction():
        await connection.execute('SELECT pg_advisory_xact_lock(79342011)')
        await connection.execute('''CREATE TABLE IF NOT EXISTS agent_schema_migrations (
            name text PRIMARY KEY, sha256 text NOT NULL, applied_at timestamptz NOT NULL DEFAULT now())''')
        rows = await (await connection.execute('SELECT name,sha256 FROM agent_schema_migrations')).fetchall()
        applied = dict(rows)
        files = sorted(MIGRATIONS.glob('[0-9]*.sql'))
        if not files or set(applied) - {path.name for path in files}:
            raise RuntimeError('Platform migration manifest is missing or older than database')
        for path in files:
            source = path.read_bytes()
            digest = hashlib.sha256(source).hexdigest()
            if path.name in applied:
                if applied[path.name] != digest:
                    raise RuntimeError('Applied platform migration was modified: ' + path.name)
                continue
            await connection.execute(source.decode())
            await connection.execute('INSERT INTO agent_schema_migrations(name,sha256) VALUES(%s,%s)',
                                     (path.name, digest))
        # Runtime checks can read the manifest; only the migrator can change it.
        await connection.execute('REVOKE INSERT,UPDATE,DELETE ON agent_schema_migrations FROM agent_runtime_api,agent_runtime_worker')


if __name__ == '__main__':
    asyncio.run(migrate())
