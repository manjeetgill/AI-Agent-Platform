"""Short-lived worker capabilities, without business data or host information."""
import asyncio
import logging
from contextlib import asynccontextmanager
from uuid import uuid4

from psycopg.types.json import Jsonb

from .automation import scope

FRESH_SECONDS = 45


async def heartbeat(pool, instance, registry, renderer=False):
    versions = {name: str(adapter.version) for name, adapter in registry.adapters.items()}
    async with pool.connection() as conn, conn.transaction():
        await scope(conn, worker=True)
        await conn.execute('''INSERT INTO platform_worker_heartbeats(instance_id,tool_versions,server_renderer)
            VALUES(%s,%s,%s) ON CONFLICT(instance_id) DO UPDATE SET tool_versions=excluded.tool_versions,
            server_renderer=excluded.server_renderer,last_seen_at=now()''', (instance, Jsonb(versions), renderer))
        await conn.execute("DELETE FROM platform_worker_heartbeats WHERE last_seen_at<now()-interval '1 day'")


@asynccontextmanager
async def reporting(pool, registry, renderer_available=lambda: False):
    instance = uuid4()
    # Do not advertise a worker until its adapters and database are initialized.
    await heartbeat(pool, instance, registry, renderer_available())

    async def refresh():
        while True:
            await asyncio.sleep(5)
            try:
                await heartbeat(pool, instance, registry, renderer_available())
            except Exception:  # noqa: BLE001 -- no database details or credentials in logs
                logging.getLogger(__name__).warning('Worker heartbeat unavailable; readiness will expire')

    task = asyncio.create_task(refresh())
    try:
        yield
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        try:
            async with pool.connection() as conn, conn.transaction():
                await scope(conn, worker=True)
                await conn.execute('DELETE FROM platform_worker_heartbeats WHERE instance_id=%s', (instance,))
        except Exception:  # noqa: BLE001 -- expiry handles database/network failure on shutdown
            logging.getLogger(__name__).warning('Worker shutdown heartbeat will expire automatically')
