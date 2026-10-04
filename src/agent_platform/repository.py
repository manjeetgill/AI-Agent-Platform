"""Platform-owned PostgreSQL persistence. Every user read has ownership predicates."""
from uuid import NAMESPACE_URL, uuid4, uuid5

from psycopg.errors import UniqueViolation
from psycopg.types.json import Jsonb


class Repository:
    def __init__(self, pool):
        self.pool = pool

    async def reserve_idea(self, tenant, user):
        async with self.pool.connection() as connection:
            cursor = await connection.execute('''INSERT INTO agent_idea_usage(tenant_id,owner_id)
                VALUES(%s,%s) ON CONFLICT(tenant_id,owner_id,usage_day) DO UPDATE SET calls=agent_idea_usage.calls+1
                WHERE agent_idea_usage.calls<20 RETURNING calls''', (tenant, user))
            return await cursor.fetchone() is not None

    async def list_preferences(self, tenant, user):
        async with self.pool.connection() as connection:
            cursor = await connection.execute('''SELECT content FROM agent_memory WHERE tenant_id=%s AND owner_id=%s
                AND kind='PREFERENCE' ORDER BY created_at DESC LIMIT 5''', (tenant, user))
            return [row['content'] for row in await cursor.fetchall()]

    async def list_preference_rows(self, tenant, user):
        async with self.pool.connection() as connection:
            cursor = await connection.execute('''SELECT id,content,created_at FROM agent_memory WHERE tenant_id=%s
                AND owner_id=%s AND kind='PREFERENCE' ORDER BY created_at DESC LIMIT 20''', (tenant, user))
            return await cursor.fetchall()

    async def add_preference(self, tenant, user, content):
        memory_id = uuid4()
        async with self.pool.connection() as connection:
            cursor = await connection.execute('''INSERT INTO agent_memory(id,tenant_id,owner_id,kind,content)
                SELECT %s,%s,%s,'PREFERENCE',%s WHERE
                (SELECT count(*) FROM agent_memory WHERE tenant_id=%s AND owner_id=%s AND kind='PREFERENCE')<20
                RETURNING id''', (memory_id, tenant, user, content, tenant, user))
            return str(memory_id) if await cursor.fetchone() else None

    async def remove_preference(self, tenant, user, memory_id):
        async with self.pool.connection() as connection:
            cursor = await connection.execute('''DELETE FROM agent_memory WHERE id=%s AND tenant_id=%s
                AND owner_id=%s AND kind='PREFERENCE' RETURNING id''', (memory_id, tenant, user))
            return await cursor.fetchone() is not None

    async def record_observations(self, tenant, user, run_id, report):
        observations = [item['text'] for item in report.get('supporting', []) + report.get('conflicting', [])
                        if item.get('category') == 'OBSERVATION'][:5]
        if not observations:
            return
        async with self.pool.connection() as connection, connection.transaction():
            for content in observations:
                await connection.execute('''INSERT INTO agent_memory(id,tenant_id,owner_id,kind,content,source_run)
                    SELECT %s,%s,%s,'OBSERVATION',%s,%s FROM agent_runs
                    WHERE id=%s AND tenant_id=%s AND owner_id=%s AND status='COMPLETED'
                    AND (SELECT count(*) FROM agent_memory WHERE tenant_id=%s AND owner_id=%s
                    AND kind='OBSERVATION')<500 ''',
                    (uuid4(), tenant, user, content, run_id, run_id, tenant, user, tenant, user))

    async def search_memory(self, tenant, user, query):
        async with self.pool.connection() as connection:
            cursor = await connection.execute('''SELECT id,kind,content,source_run,created_at FROM agent_memory
                WHERE tenant_id=%s AND owner_id=%s AND to_tsvector('english',content) @@ plainto_tsquery('english',%s)
                ORDER BY created_at DESC LIMIT 20''', (tenant, user, query))
            return await cursor.fetchall()

    async def ensure_builtins(self, tenant, user, definitions):
        async with self.pool.connection() as connection, connection.transaction():
            for slug, definition in definitions.items():
                agent_id = uuid5(NAMESPACE_URL, f'agent-platform:{tenant}:{user}:{slug}')
                await connection.execute('''INSERT INTO agent_definitions(id,tenant_id,owner_id,name,description,kind)
                    VALUES(%s,%s,%s,%s,%s,'builtin') ON CONFLICT(id) DO NOTHING''',
                    (agent_id, tenant, user, definition['name'], definition['description']))
                await connection.execute('''INSERT INTO agent_versions(agent_id,version,instructions,allowed_tools)
                    VALUES(%s,1,%s,%s) ON CONFLICT(agent_id,version) DO NOTHING''',
                    (agent_id, definition['instructions'], Jsonb(definition['allowed_tools'])))

    async def list_agents(self, tenant, user):
        async with self.pool.connection() as connection:
            cursor = await connection.execute('''SELECT d.id,d.name,d.description,d.kind,d.status,
                d.current_version AS version,v.instructions,v.allowed_tools FROM agent_definitions d JOIN agent_versions v ON v.agent_id=d.id
                AND v.version=d.current_version WHERE d.tenant_id=%s AND d.owner_id=%s ORDER BY d.updated_at DESC''',
                (tenant, user))
            return await cursor.fetchall()

    async def create_agent(self, tenant, user, draft):
        agent_id = uuid4()
        async with self.pool.connection() as connection, connection.transaction():
            await connection.execute('''INSERT INTO agent_definitions(id,tenant_id,owner_id,name,description)
                VALUES(%s,%s,%s,%s,%s)''', (agent_id, tenant, user, draft.name, draft.description))
            await connection.execute('''INSERT INTO agent_versions(agent_id,version,instructions,allowed_tools)
                VALUES(%s,1,%s,%s)''', (agent_id, draft.instructions, Jsonb(draft.allowed_tools)))
        return str(agent_id)

    async def get_agent(self, tenant, user, agent_id):
        async with self.pool.connection() as connection:
            cursor = await connection.execute('''SELECT d.id,d.name,d.description,d.status,d.current_version AS version,
                d.kind,v.instructions,v.allowed_tools FROM agent_definitions d JOIN agent_versions v ON v.agent_id=d.id
                AND v.version=d.current_version WHERE d.tenant_id=%s AND d.owner_id=%s AND d.id=%s''',
                (tenant, user, agent_id))
            return await cursor.fetchone()

    async def update_agent(self, tenant, user, agent_id, draft):
        async with self.pool.connection() as connection, connection.transaction():
            cursor = await connection.execute('''UPDATE agent_definitions SET name=%s,description=%s,
                current_version=current_version+1,updated_at=now() WHERE tenant_id=%s AND owner_id=%s AND id=%s
                AND kind='custom'
                RETURNING current_version''', (draft.name, draft.description, tenant, user, agent_id))
            row = await cursor.fetchone()
            if not row:
                return None
            await connection.execute('''INSERT INTO agent_versions(agent_id,version,instructions,allowed_tools)
                VALUES(%s,%s,%s,%s)''', (agent_id, row['current_version'], draft.instructions,
                                           Jsonb(draft.allowed_tools)))
            return row['current_version']

    async def set_status(self, tenant, user, agent_id, status):
        async with self.pool.connection() as connection:
            cursor = await connection.execute('''UPDATE agent_definitions SET status=%s,updated_at=now()
                WHERE tenant_id=%s AND owner_id=%s AND id=%s AND kind='custom' RETURNING id''',
                (status, tenant, user, agent_id))
            return await cursor.fetchone() is not None

    async def create_run(self, tenant, user, agent_id, version, payload, key):
        run_id = uuid4()
        try:
            async with self.pool.connection() as connection:
                cursor = await connection.execute('''INSERT INTO agent_runs(id,tenant_id,owner_id,agent_id,agent_version,
                    idempotency_key,status,input) VALUES(%s,%s,%s,%s,%s,%s,'CREATED',%s)
                    ON CONFLICT(tenant_id,owner_id,idempotency_key) DO NOTHING RETURNING id''',
                    (run_id, tenant, user, agent_id, version, key, Jsonb(payload)))
                row = await cursor.fetchone()
                if row:
                    return str(row['id']), True
                cursor = await connection.execute('''SELECT id FROM agent_runs WHERE tenant_id=%s AND owner_id=%s
                    AND idempotency_key=%s''', (tenant, user, key))
                return str((await cursor.fetchone())['id']), False
        except UniqueViolation:
            return None, False

    async def run_by_key(self, tenant, user, key):
        async with self.pool.connection() as connection:
            cursor = await connection.execute('''SELECT id,agent_id,agent_version,status,input,report,evidence_refs,
                error_code,provider,model,tokens,created_at,started_at,finished_at FROM agent_runs
                WHERE tenant_id=%s AND owner_id=%s AND idempotency_key=%s''', (tenant, user, key))
            return await cursor.fetchone()

    async def recent_count(self, tenant, user):
        async with self.pool.connection() as connection:
            cursor = await connection.execute('''SELECT count(*) AS n FROM agent_runs WHERE tenant_id=%s AND owner_id=%s
                AND created_at>now()-interval '24 hours' ''', (tenant, user))
            return (await cursor.fetchone())['n']

    async def usage(self, tenant, user):
        async with self.pool.connection() as connection:
            cursor = await connection.execute('''SELECT count(*) AS runs_24h,coalesce(sum(tokens),0) AS tokens_24h,
                count(*) FILTER (WHERE status='COMPLETED') AS completed_24h,
                count(*) FILTER (WHERE status IN ('FAILED','TIMEOUT')) AS failed_24h
                FROM agent_runs WHERE tenant_id=%s AND owner_id=%s
                AND created_at>now()-interval '24 hours' ''', (tenant, user))
            return await cursor.fetchone()

    async def reap_workflows(self, tenant, user):
        async with self.pool.connection() as connection:
            await connection.execute('''UPDATE agent_workflow_runs SET status='TIMEOUT',finished_at=now(),
                lease_until=NULL WHERE tenant_id=%s AND owner_id=%s AND status IN ('CREATED','RUNNING')
                AND ((lease_until IS NOT NULL AND lease_until<now()) OR created_at<now()-interval '3 minutes')''',
                (tenant, user))

    async def create_workflow(self, tenant, user, kind, key):
        workflow_id = uuid4()
        try:
            async with self.pool.connection() as connection:
                cursor = await connection.execute('''INSERT INTO agent_workflow_runs(id,tenant_id,owner_id,kind,
                    idempotency_key,status,lease_until) VALUES(%s,%s,%s,%s,%s,'CREATED',now()+interval '2 minutes')
                    ON CONFLICT(tenant_id,owner_id,idempotency_key) DO NOTHING RETURNING id''',
                    (workflow_id, tenant, user, kind, key))
                row = await cursor.fetchone()
                if row:
                    return str(row['id']), True
                cursor = await connection.execute('''SELECT id FROM agent_workflow_runs WHERE tenant_id=%s
                    AND owner_id=%s AND idempotency_key=%s''', (tenant, user, key))
                return str((await cursor.fetchone())['id']), False
        except UniqueViolation:
            return None, False

    async def workflow_transition(self, workflow_id, old, new, step_id=None):
        updates = ['status=%s']
        values = [new]
        if step_id:
            updates.append('step_run_ids=step_run_ids || %s')
            values.append(Jsonb([str(step_id)]))
        if new in {'COMPLETED', 'FAILED', 'TIMEOUT'}:
            updates += ['finished_at=now()', 'lease_until=NULL']
        async with self.pool.connection() as connection:
            cursor = await connection.execute('UPDATE agent_workflow_runs SET ' + ','.join(updates) +
                ' WHERE id=%s AND status=%s RETURNING id', (*values, workflow_id, old))
            return await cursor.fetchone() is not None

    async def workflow_step(self, workflow_id, step_id):
        async with self.pool.connection() as connection:
            await connection.execute('''UPDATE agent_workflow_runs SET step_run_ids=step_run_ids || %s
                WHERE id=%s AND status='RUNNING' ''', (Jsonb([str(step_id)]), workflow_id))

    async def get_workflow(self, tenant, user, workflow_id):
        async with self.pool.connection() as connection:
            cursor = await connection.execute('''SELECT id,kind,status,step_run_ids,created_at,finished_at
                FROM agent_workflow_runs WHERE tenant_id=%s AND owner_id=%s AND id=%s''',
                (tenant, user, workflow_id))
            return await cursor.fetchone()

    async def list_workflows(self, tenant, user):
        async with self.pool.connection() as connection:
            cursor = await connection.execute('''SELECT id,kind,status,step_run_ids,created_at,finished_at
                FROM agent_workflow_runs WHERE tenant_id=%s AND owner_id=%s
                ORDER BY created_at DESC LIMIT 50''', (tenant, user))
            return await cursor.fetchall()

    async def active_count(self, tenant, user):
        async with self.pool.connection() as connection:
            cursor = await connection.execute('''SELECT count(*) AS n FROM agent_runs WHERE tenant_id=%s AND owner_id=%s
                AND status IN ('CREATED','QUEUED','RUNNING')''', (tenant, user))
            return (await cursor.fetchone())['n']

    async def reap_stale(self, tenant, user):
        async with self.pool.connection() as connection:
            cursor = await connection.execute('''UPDATE agent_runs SET status='TIMEOUT',error_code='RUN_LEASE_EXPIRED',
                finished_at=now(),lease_until=NULL WHERE tenant_id=%s AND owner_id=%s AND
                ((status='RUNNING' AND lease_until<now()) OR
                 (status='CREATED' AND created_at<now()-interval '2 minutes')) RETURNING id''',
                (tenant, user))
            return len(await cursor.fetchall())

    async def transition(self, run_id, from_status, to_status, **fields):
        allowed = {'report', 'evidence_refs', 'error_code', 'provider', 'model', 'tokens'}
        if set(fields) - allowed:
            raise ValueError('Invalid run fields')
        updates = ['status=%s']
        values = [to_status]
        if to_status == 'RUNNING':
            updates.append('started_at=now()')
            updates.append("lease_until=now()+interval '60 seconds'")
        if to_status in {'COMPLETED', 'FAILED', 'CANCELLED', 'TIMEOUT'}:
            updates.append('finished_at=now()')
            updates.append('lease_until=NULL')
        for key, value in fields.items():
            updates.append(key + '=%s')
            values.append(Jsonb(value) if key in {'report', 'evidence_refs'} else value)
        async with self.pool.connection() as connection:
            cursor = await connection.execute('UPDATE agent_runs SET ' + ','.join(updates) +
                ' WHERE id=%s AND status=%s RETURNING id', (*values, run_id, from_status))
            return await cursor.fetchone() is not None

    async def get_run(self, tenant, user, run_id):
        async with self.pool.connection() as connection:
            cursor = await connection.execute('''SELECT id,agent_id,agent_version,status,report,evidence_refs,error_code,
                provider,model,tokens,created_at,started_at,finished_at FROM agent_runs
                WHERE tenant_id=%s AND owner_id=%s AND id=%s''', (tenant, user, run_id))
            return await cursor.fetchone()

    async def list_runs(self, tenant, user):
        async with self.pool.connection() as connection:
            cursor = await connection.execute('''SELECT id,agent_id,agent_version,status,error_code,created_at,
                finished_at FROM agent_runs WHERE tenant_id=%s AND owner_id=%s ORDER BY created_at DESC LIMIT 100''',
                (tenant, user))
            return await cursor.fetchall()
