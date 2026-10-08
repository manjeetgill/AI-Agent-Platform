"""Create a disposable database on the local platform cluster; never print credentials."""
import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import psycopg
from psycopg.conninfo import make_conninfo
from psycopg.sql import SQL, Identifier

ROOT = Path(__file__).resolve().parents[1]


async def main():
    values = json.loads((ROOT / '.runtime/local/config.json').read_text())
    name = 'orchestration_test_' + uuid4().hex[:12]
    base = values['AI_PLATFORM_DATABASE_URL']
    url = make_conninfo(base, dbname=name)
    async with await psycopg.AsyncConnection.connect(base, autocommit=True) as conn:
        await conn.execute(SQL('CREATE DATABASE {}').format(Identifier(name)))
        try:
            env = {**os.environ, 'AI_PLATFORM_DATABASE_URL': url, 'AGENT_TEST_DATABASE_URL': url,
                   'PYTHONPATH': str(ROOT / 'src')}
            await asyncio.to_thread(subprocess.run, [sys.executable, '-m', 'agent_platform.migrate'], env=env, cwd=ROOT, check=True)
            result = await asyncio.to_thread(subprocess.run, [sys.executable, '-m', 'pytest', '-q',
                'tests/python/test_automation.py', 'tests/python/test_adaptive_runtime.py', 'tests/python/test_task_agents.py',
                'tests/python/test_durable_tasks.py', 'tests/python/test_app_catalogue.py'], env=env, cwd=ROOT, check=False)
            return result.returncode
        finally:
            await conn.execute(SQL('DROP DATABASE {} WITH (FORCE)').format(Identifier(name)))


if __name__ == '__main__':
    raise SystemExit(asyncio.run(main()))
