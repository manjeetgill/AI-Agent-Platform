"""Start the local console and worker without modifying the existing local configuration."""
import asyncio
import os
from urllib.parse import urlsplit, urlunsplit

import psycopg
import uvicorn
from local import ROOT, database, load

values = load()
database(values)
os.environ.update(values)
os.environ.update(AI_DURABLE_TASKS='1', AI_TASK_AGENTS='1')
import sys

sys.path.insert(0, str(ROOT/'src'))
from agent_platform.console_app import create_console
from agent_platform.decision_tool import default_registry
from agent_platform.migrate import migrate
from agent_platform.task_worker import serve


async def main():
    # Keep the console pilot isolated from the existing research/report database.
    original = os.environ['AI_PLATFORM_DATABASE_URL']
    async with await psycopg.AsyncConnection.connect(original, autocommit=True) as conn:
        if not await (await conn.execute("SELECT 1 FROM pg_database WHERE datname='agent_console'")).fetchone():
            await conn.execute("CREATE DATABASE agent_console TEMPLATE template0 ENCODING 'UTF8'")
    parts = urlsplit(original)
    os.environ['AI_PLATFORM_DATABASE_URL'] = urlunsplit(parts._replace(path='/agent_console'))
    await migrate()
    registry = default_registry()
    server = uvicorn.Server(uvicorn.Config(create_console(registry=registry), host='127.0.0.1', port=4061,
                                           access_log=False))
    worker = asyncio.create_task(serve(registry))
    try:
        await server.serve()
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)


if __name__ == '__main__':
    asyncio.run(main())
