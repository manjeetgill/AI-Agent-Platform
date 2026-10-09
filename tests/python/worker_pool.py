from contextlib import asynccontextmanager

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from test_durable_tasks import database


class WorkerPool:
    @asynccontextmanager
    async def connection(self, *args, **kwargs):
        async with AsyncConnectionPool(database(),open=False,kwargs={'row_factory':dict_row}) as pool, pool.connection(*args,**kwargs) as conn:
            yield conn
