"""Restart-safe local embedding indexing. Validated unindexed rows are the durable queue."""

import asyncio
import logging
import os

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from .db_scope import worker_connection
from .memory_embeddings import configuration, index_owned


async def tick(pool):
    config = configuration()
    if not config:
        return 0
    async with pool.connection() as conn:
        row = await (
            await conn.execute(
                """SELECT m.app_id,m.owner_id FROM platform_memories m
            LEFT JOIN memory_vectors v ON v.memory_id=m.id
            WHERE m.status='VALIDATED' AND m.expires_at>now()
            AND (v.memory_id IS NULL OR v.version<>m.version OR v.model<>%s)
            ORDER BY m.updated_at,m.id LIMIT 1""",
                (config[1],),
            )
        ).fetchone()
    if not row:
        return 0
    return await index_owned(pool, row["app_id"], row["owner_id"])


async def serve():
    async with AsyncConnectionPool(
        os.environ.get("AI_PLATFORM_WORKER_DATABASE_URL") or os.environ["AI_PLATFORM_DATABASE_URL"],
        configure=worker_connection,
        open=False,
        kwargs={"row_factory": dict_row},
        min_size=1,
        max_size=2,
    ) as pool:
        from .db_scope import assert_runtime_role
        await assert_runtime_role(pool,'worker')
        while True:
            try:
                count = await tick(pool)
            except Exception:  # noqa: BLE001 - supervise optional index work; never log memory content
                logging.getLogger(__name__).warning(
                    "Memory indexing deferred; lexical retrieval remains active"
                )
                count = 0
            await asyncio.sleep(2 if count else 30)


if __name__ == "__main__":
    asyncio.run(serve())
