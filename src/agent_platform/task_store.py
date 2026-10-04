"""PostgreSQL task state and queue, changed together under row locks."""
from uuid import uuid4

from psycopg.types.json import Jsonb

from .tasks import TaskError


class TaskStore:
    def __init__(self, pool):
        self.pool = pool

    async def event(self, conn, task, name, details=None):
        await conn.execute('INSERT INTO chore_events(task_id,event,details) VALUES(%s,%s,%s)',
                           (task, name, Jsonb(details or {})))

    async def owned(self, conn, tenant, user, task):
        row = await (await conn.execute('''SELECT * FROM chore_tasks
            WHERE id=%s AND tenant_id=%s AND owner_id=%s FOR UPDATE''', (task, tenant, user))).fetchone()
        if row is None:
            raise TaskError('Task not found')
        return row

    async def create(self, tenant, user, body):
        async with self.pool.connection() as conn, conn.transaction():
            row = await (await conn.execute('''INSERT INTO chore_tasks(id,tenant_id,owner_id,request,request_key)
                VALUES(%s,%s,%s,%s,%s) ON CONFLICT(tenant_id,owner_id,request_key) DO NOTHING RETURNING *''',
                (uuid4(), tenant, user, body.request, body.idempotency_key))).fetchone()
            if row:
                await self.event(conn, row['id'], 'CREATED')
            else:
                row = await (await conn.execute('''SELECT * FROM chore_tasks WHERE tenant_id=%s AND owner_id=%s
                    AND request_key=%s''', (tenant, user, body.idempotency_key))).fetchone()
                if row['request'] != body.request:
                    raise TaskError('Idempotency key belongs to another request')
            return row

    async def get(self, tenant, user, task):
        async with self.pool.connection() as conn, conn.transaction():
            row = await self.owned(conn, tenant, user, task)
            row['steps'] = await (await conn.execute('SELECT * FROM chore_steps WHERE task_id=%s ORDER BY position',
                                                    (task,))).fetchall()
            row['events'] = await (await conn.execute('''SELECT id,event,details,created_at FROM chore_events
                WHERE task_id=%s ORDER BY id DESC LIMIT 100''', (task,))).fetchall()
            # Lease tokens are execution authority, never part of the browser-facing representation.
            row.pop('lease_token', None)
            return row

    async def list(self, tenant, user):
        async with self.pool.connection() as conn:
            return await (await conn.execute('''SELECT id,request,status,current_step,revision,created_at,updated_at
                FROM chore_tasks WHERE tenant_id=%s AND owner_id=%s ORDER BY created_at DESC,id LIMIT 100''',
                (tenant, user))).fetchall()

    async def plan(self, tenant, user, task, actions):
        async with self.pool.connection() as conn, conn.transaction():
            row = await self.owned(conn, tenant, user, task)
            if row['status'] != 'DRAFT':
                raise TaskError('Only draft plans can be edited')
            await conn.execute('DELETE FROM chore_steps WHERE task_id=%s', (task,))
            for position, action in enumerate(actions):
                await conn.execute('''INSERT INTO chore_steps(task_id,position,tool,tool_version,arguments,
                    action_hash,summary,risk) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)''',
                    (task, position, action.tool, action.version, Jsonb(action.arguments), action.digest,
                     action.summary, action.risk))
            await conn.execute('UPDATE chore_tasks SET revision=revision+1,updated_at=now() WHERE id=%s', (task,))
            await self.event(conn, task, 'PLAN_SAVED', {'steps': len(actions)})

    async def queue(self, conn, task, mode='RUN', delay=0):
        await conn.execute('''INSERT INTO chore_jobs(task_id,mode,available_at)
            VALUES(%s,%s,now()+%s*interval '1 second') ON CONFLICT(task_id)
            DO UPDATE SET mode=excluded.mode,available_at=excluded.available_at''', (task, mode, delay))
        await conn.execute("UPDATE chore_tasks SET status='READY',updated_at=now() WHERE id=%s", (task,))

    async def next(self, conn, task, position):
        step = await (await conn.execute('SELECT * FROM chore_steps WHERE task_id=%s AND position=%s',
                                        (task, position))).fetchone()
        status = 'DONE' if step is None else {'READ': 'READY', 'SEND': 'WAITING_APPROVAL',
                                             'USER_ONLY': 'WAITING_USER'}[step['risk']]
        await conn.execute('''UPDATE chore_tasks SET status=%s,current_step=%s,lease_token=NULL,
            lease_until=NULL,updated_at=now() WHERE id=%s''', (status, position, task))
        if status == 'READY':
            await self.queue(conn, task)
        await self.event(conn, task, status, {'position': position})

    async def start(self, tenant, user, task):
        async with self.pool.connection() as conn, conn.transaction():
            row = await self.owned(conn, tenant, user, task)
            if row['status'] != 'DRAFT' or row['revision'] == 0:
                raise TaskError('A saved draft plan is required')
            await self.next(conn, task, 0)

    async def approve(self, tenant, user, task, digest):
        async with self.pool.connection() as conn, conn.transaction():
            row = await self.owned(conn, tenant, user, task)
            if row['status'] != 'WAITING_APPROVAL':
                raise TaskError('Task is not waiting for approval')
            step = await (await conn.execute('SELECT * FROM chore_steps WHERE task_id=%s AND position=%s',
                                            (task, row['current_step']))).fetchone()
            if step['risk'] != 'SEND' or step['action_hash'] != digest:
                raise TaskError('Approval does not match the pending action')
            await conn.execute('''INSERT INTO chore_approvals(id,task_id,position,action_hash,approved_by,expires_at)
                VALUES(%s,%s,%s,%s,%s,now()+interval '10 minutes')''',
                (uuid4(), task, row['current_step'], digest, user))
            await self.event(conn, task, 'APPROVED', {'action_hash': digest, 'approved_by': str(user)})
            await self.queue(conn, task)

    async def control(self, tenant, user, task, command):
        async with self.pool.connection() as conn, conn.transaction():
            row = await self.owned(conn, tenant, user, task)
            if row['status'] in {'DONE', 'FAILED', 'CANCELLED', 'DRAFT'}:
                raise TaskError('Task cannot accept this command')
            step = await (await conn.execute('SELECT * FROM chore_steps WHERE task_id=%s AND position=%s',
                                            (task, row['current_step']))).fetchone()
            if command in {'pause', 'cancel'}:
                uncertain = row['status'] == 'OUTCOME_UNKNOWN' or (
                    row['status'] in {'RUNNING', 'WAITING_USER'} and step['risk'] != 'READ')
                status = 'OUTCOME_UNKNOWN' if uncertain else ('PAUSED' if command == 'pause' else 'CANCELLED')
                await conn.execute('''UPDATE chore_tasks SET status=%s,stop_requested=%s,
                    lease_token=NULL,lease_until=NULL,updated_at=now() WHERE id=%s''',
                    (status, 'PAUSED' if command == 'pause' else 'CANCELLED', task))
                await conn.execute('DELETE FROM chore_jobs WHERE task_id=%s', (task,))
                await conn.execute('DELETE FROM chore_approvals WHERE task_id=%s AND consumed_at IS NULL', (task,))
                await conn.execute('UPDATE chore_steps SET status=%s WHERE task_id=%s AND position=%s',
                                   ('UNKNOWN' if uncertain else 'PENDING', task, row['current_step']))
            elif command == 'resume' and row['status'] == 'PAUSED':
                await conn.execute('UPDATE chore_tasks SET stop_requested=NULL WHERE id=%s', (task,))
                await self.next(conn, task, row['current_step'])
            elif command == 'reconcile' and row['status'] in {'OUTCOME_UNKNOWN', 'WAITING_USER'}:
                await self.queue(conn, task, 'RECONCILE')
            else:
                raise TaskError('Resume requires a paused task; uncertain or user-only actions require reconciliation')
            await self.event(conn, task, command.upper())

    async def recover(self):
        async with self.pool.connection() as conn, conn.transaction():
            rows = await (await conn.execute('''SELECT t.id,t.current_step,s.risk,s.attempts FROM chore_tasks t
                JOIN chore_steps s ON s.task_id=t.id AND s.position=t.current_step
                WHERE t.status='RUNNING' AND t.lease_until<now() FOR UPDATE OF t SKIP LOCKED''')).fetchall()
            for row in rows:
                retry = row['risk'] == 'READ' and row['attempts'] < 3
                status = 'READY' if retry else ('FAILED' if row['risk'] == 'READ' else 'OUTCOME_UNKNOWN')
                await conn.execute('''UPDATE chore_tasks SET status=%s,lease_token=NULL,lease_until=NULL,
                    updated_at=now() WHERE id=%s''', (status, row['id']))
                await conn.execute('UPDATE chore_steps SET status=%s WHERE task_id=%s AND position=%s',
                    ('PENDING' if retry else 'FAILED' if status == 'FAILED' else 'UNKNOWN',
                     row['id'], row['current_step']))
                if retry:
                    await self.queue(conn, row['id'], delay=1)
                await self.event(conn, row['id'], 'LEASE_EXPIRED', {'status': status})

    async def claim(self):
        async with self.pool.connection() as conn, conn.transaction():
            task = await (await conn.execute('''SELECT t.*,j.mode FROM chore_jobs j
                JOIN chore_tasks t ON t.id=j.task_id WHERE t.status='READY' AND j.available_at<=now()
                ORDER BY j.available_at FOR UPDATE OF t,j SKIP LOCKED LIMIT 1''')).fetchone()
            if not task:
                return None
            step = await (await conn.execute('SELECT * FROM chore_steps WHERE task_id=%s AND position=%s',
                                            (task['id'], task['current_step']))).fetchone()
            await conn.execute('DELETE FROM chore_jobs WHERE task_id=%s', (task['id'],))
            if task['mode'] == 'RUN' and step['risk'] == 'USER_ONLY':
                await self.next(conn, task['id'], task['current_step'])
                return None
            if task['mode'] == 'RUN' and step['risk'] == 'SEND':
                approval = await (await conn.execute('''UPDATE chore_approvals SET consumed_at=now()
                    WHERE id=(SELECT id FROM chore_approvals WHERE task_id=%s AND position=%s
                    AND action_hash=%s AND consumed_at IS NULL AND expires_at>now()
                    ORDER BY created_at DESC LIMIT 1) RETURNING id''',
                    (task['id'], task['current_step'], step['action_hash']))).fetchone()
                if not approval:
                    await self.next(conn, task['id'], task['current_step'])
                    return None
            token = uuid4()
            await conn.execute('''UPDATE chore_tasks SET status='RUNNING',lease_token=%s,
                lease_until=now()+interval '45 seconds',updated_at=now() WHERE id=%s''', (token, task['id']))
            await conn.execute("UPDATE chore_steps SET status='RUNNING',attempts=attempts+1 WHERE task_id=%s AND position=%s",
                               (task['id'], task['current_step']))
            await self.event(conn, task['id'], 'DISPATCHED', {'position': task['current_step'],
                'attempt_id': str(token), 'mode': task['mode'], 'action_hash': step['action_hash']})
            return {**task, 'lease_token': token, 'step': step}

    async def authorized(self, task, token):
        async with self.pool.connection() as conn:
            return await (await conn.execute('''SELECT 1 FROM chore_tasks WHERE id=%s AND lease_token=%s
                AND status='RUNNING' AND lease_until>now()''', (task, token))).fetchone() is not None

    async def finish(self, claimed, result=None, *, failed=False, not_applied=False):
        async with self.pool.connection() as conn, conn.transaction():
            task = await (await conn.execute('''SELECT * FROM chore_tasks WHERE id=%s AND lease_token=%s
                AND status='RUNNING' AND lease_until>now() FOR UPDATE''',
                (claimed['id'], claimed['lease_token']))).fetchone()
            if task is None:
                return False
            step = claimed['step']
            if not_applied:
                await conn.execute("UPDATE chore_steps SET status='PENDING',result=%s WHERE task_id=%s AND position=%s",
                                   (Jsonb(result), task['id'], task['current_step']))
                await self.next(conn, task['id'], task['current_step'])
                await self.event(conn, task['id'], 'VERIFIED_NOT_APPLIED')
            elif failed or result is None:
                retry = claimed['mode'] == 'RUN' and step['risk'] == 'READ' and step['attempts'] < 2
                status = 'READY' if retry else ('FAILED' if step['risk'] == 'READ' else 'OUTCOME_UNKNOWN')
                await conn.execute('''UPDATE chore_tasks SET status=%s,lease_token=NULL,lease_until=NULL,
                    updated_at=now() WHERE id=%s''', (status, task['id']))
                await conn.execute('UPDATE chore_steps SET status=%s WHERE task_id=%s AND position=%s',
                    ('PENDING' if retry else 'FAILED' if status == 'FAILED' else 'UNKNOWN',
                     task['id'], task['current_step']))
                if retry:
                    await self.queue(conn, task['id'], delay=2 ** step['attempts'])
                await self.event(conn, task['id'], status)
            else:
                await conn.execute("UPDATE chore_steps SET status='DONE',result=%s WHERE task_id=%s AND position=%s",
                                   (Jsonb(result), task['id'], task['current_step']))
                await self.event(conn, task['id'], 'STEP_VERIFIED', {'position': task['current_step']})
                await self.next(conn, task['id'], task['current_step'] + 1)
            if task['stop_requested'] and not failed and result is not None:
                await conn.execute('''UPDATE chore_tasks SET status=%s,lease_token=NULL,lease_until=NULL,
                    updated_at=now() WHERE id=%s''', (task['stop_requested'], task['id']))
                await conn.execute('DELETE FROM chore_jobs WHERE task_id=%s', (task['id'],))
                await self.event(conn, task['id'], task['stop_requested'])
            return True
