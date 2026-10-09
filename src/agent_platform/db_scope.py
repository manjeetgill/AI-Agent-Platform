"""Transaction-local request scope. Never promote an authenticated API to worker."""
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar

from psycopg_pool import AsyncConnectionPool

model_identity = ContextVar('model_identity',default=None)
operator_access = ContextVar('operator_access',default=False)
identity = ContextVar('agent_identity', default=None)
verified_delegation = ContextVar('verified_delegation', default=None)


async def scope(conn, claims=None, *, worker=False):
    request = identity.get()
    if request is not None:
        claims, worker = request, False
    for key, value in {'agent.worker': 'on' if worker else 'off',
                       'agent.tenant': (claims or {}).get('tenant', ''),
                       'agent.app': (claims or {}).get('app_id', ''),
                       'agent.owner': (claims or {}).get('user', ''),
                       'agent.voice_channel':(claims or {}).get('voice_channel',''),
                       'agent.voice_agent':(claims or {}).get('voice_agent',''),
                       'agent.voice_conversation':(claims or {}).get('voice_conversation',''),
                       'agent.email_callback': (claims or {}).get('app_id','') if (claims or {}).get('email_callback') else ''}.items():
        await conn.execute('SELECT set_config(%s,%s,true)', (key, str(value or '')))


class APIPool(AsyncConnectionPool):
    operator_pool = None

    @asynccontextmanager
    async def connection(self, timeout=None):
        if operator_access.get() and identity.get() is None and self.operator_pool is not None:
            async with self.operator_pool.connection(timeout=timeout) as conn:
                yield conn
            return
        async with super().connection(timeout=timeout) as conn, conn.transaction():
            await scope(conn, identity.get())
            yield conn


async def worker_connection(conn):
    await conn.execute("SELECT set_config('agent.worker','on',false)")
    await conn.commit()


async def assert_runtime_role(pool, kind):
    async with pool.connection() as conn:
        row = await (await conn.execute("""SELECT rolsuper,rolbypassrls,rolcreaterole,rolcreatedb,
            pg_has_role(current_user,%s,'member') AS member,
            coalesce(pg_has_role(current_user,to_regrole('agent_runtime_worker'),'member'),false) AS worker_member,
            EXISTS(SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
                   WHERE n.nspname='public' AND c.relkind='r' AND pg_has_role(current_user,c.relowner,'member')) AS owns_tables
            FROM pg_roles WHERE rolname=current_user""", ('agent_runtime_'+kind,))).fetchone()
        if not row or any(row[k] for k in ('rolsuper','rolbypassrls','rolcreaterole','rolcreatedb')) or not row['member'] or row['owns_tables'] or (kind=='api' and row['worker_member']):
            raise RuntimeError('Use the restricted '+kind+' database credential')


async def connection_identity(pool,kind,ident,conn=None):
    if not isinstance(pool,APIPool):
        return
    if conn is None:
        async with pool.connection() as connection:
            row=await (await connection.execute('SELECT agent_connection_identity(%s,%s) AS claims',(kind,str(ident)))).fetchone()
    else:
        row=await (await conn.execute('SELECT agent_connection_identity(%s,%s) AS claims',(kind,str(ident)))).fetchone()
    if row and row['claims']:
        identity.set(row['claims'])
        if conn is not None:
            await scope(conn,row['claims'])


@contextmanager
def bound_identity(claims):
    marker=identity.set(claims)
    try:
        yield
    finally:
        identity.reset(marker)
