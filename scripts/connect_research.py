"""Provision a private NRAIAlgo research task connection; no credentials are printed."""
import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

from local import CONFIG, ROOT, load, save

sys.path.insert(0, str(ROOT / 'src'))
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from agent_platform.service_identity import provision


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--consumer-config', type=Path, required=True)
    parser.add_argument('--connector-url', required=True)
    args = parser.parse_args()
    path = args.consumer_config
    if path.is_symlink() or not path.is_file() or path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o077:
        raise SystemExit('Consumer settings must be an existing private file (0600)')
    parsed = urlsplit(args.connector_url)
    if parsed.scheme != 'http' or parsed.hostname not in {'127.0.0.1', 'localhost'} or parsed.path not in {'', '/'}:
        raise SystemExit('This local setup command requires a loopback connector URL')
    values, consumer = load(), json.loads(path.read_text())
    if consumer.get('AI_PLATFORM_SIGNING_KEY') != values['AI_PLATFORM_SIGNING_KEY']:
        raise SystemExit('Consumer signing key does not match this platform')
    async with AsyncConnectionPool(values['AI_PLATFORM_DATABASE_URL'], open=False, kwargs={'row_factory': dict_row}) as pool:
        credential = await provision(pool, tenant='nraialgo', organisation='NRAIAlgo',
            app_name='Research Lab', app_id=values.get('AI_PLATFORM_RESEARCH_APP_ID'),
            scopes=['agents:read', 'agents:write', 'agents:run', 'tasks:read', 'tasks:write'], days=90)
        async with pool.connection() as conn:
            await conn.execute('INSERT INTO platform_tool_grants(app_id,tool) VALUES(%s,%s) ON CONFLICT DO NOTHING',
                               (credential['app_id'], 'research.explain'))
        consumer['AI_PLATFORM_RESEARCH_SERVICE_KEY'] = credential['credential']
        values.update(AI_PLATFORM_RESEARCH_APP_ID=credential['app_id'], AI_DURABLE_TASKS='1',
                      AI_TASK_AGENTS='1', AI_PLATFORM_CONNECTOR_URL=args.connector_url.rstrip('/'))
        save(path, consumer)
        save(CONFIG, values)
    print('Research Lab connection saved privately. Restart the platform API/worker and NRAIAlgo API. Key expires in 90 days.')


if __name__ == '__main__':
    asyncio.run(main())
