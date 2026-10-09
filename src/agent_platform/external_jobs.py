"""Adapter contracts for acknowledged external work. References must contain no secrets."""
import json
from dataclasses import dataclass

from .tasks import TaskError


@dataclass(frozen=True)
class DeferredJob:
    reference: dict
    poll_after_seconds: int = 10
    timeout_seconds: int = 900

    def __post_init__(self):
        if (not isinstance(self.reference, dict) or not self.reference
                or len(json.dumps(self.reference, allow_nan=False).encode()) > 3000
                or type(self.poll_after_seconds) is not int or not 1 <= self.poll_after_seconds <= 300
                or type(self.timeout_seconds) is not int or not 1 <= self.timeout_seconds <= 3600):
            raise TaskError('Invalid bounded external-job acknowledgement')


@dataclass(frozen=True)
class JobFailure:
    evidence: dict

    def __post_init__(self):
        if (not isinstance(self.evidence, dict) or not self.evidence
                or len(json.dumps(self.evidence, allow_nan=False).encode()) > 16000):
            raise TaskError('External failure requires bounded domain evidence')


class ExternalJobs:
    """TaskStore mixin: every acknowledgement/checkpoint commits with its queue entry."""

    async def defer(self, claimed, pending):
        from psycopg.types.json import Jsonb
        async with self.pool.connection() as conn, conn.transaction():
            task = await (await conn.execute('''SELECT * FROM chore_tasks WHERE id=%s AND lease_token=%s
                AND status='RUNNING' AND lease_until>now() FOR UPDATE''', (claimed['id'], claimed['lease_token']))).fetchone()
            if task is None:
                return False
            step = await (await conn.execute('SELECT * FROM chore_steps WHERE task_id=%s AND position=%s',
                                            (task['id'], task['current_step']))).fetchone()
            if step['external_job_reference'] is not None and step['external_job_reference'] != pending.reference:
                raise TaskError('External job reference changed while waiting')
            await conn.execute('''UPDATE chore_steps SET external_job_reference=%s,
                external_job_deadline=coalesce(external_job_deadline,now()+%s*interval '1 second'),
                external_job_state='RUNNING',external_job_failures=0,status='PENDING',outcome_pending=false
                WHERE task_id=%s AND position=%s''',
                (Jsonb(pending.reference), pending.timeout_seconds, task['id'], task['current_step']))
            await conn.execute('UPDATE chore_tasks SET lease_token=NULL,lease_until=NULL WHERE id=%s', (task['id'],))
            await self.queue(conn, task['id'], mode='POLL', delay=pending.poll_after_seconds)
            await self.event(conn, task['id'], 'JOB_WAITING', {'position': task['current_step'],
                             'next_poll_seconds': pending.poll_after_seconds})
            return True

    async def poll_failed(self, claimed):
        async with self.pool.connection() as conn, conn.transaction():
            task = await (await conn.execute('''SELECT * FROM chore_tasks WHERE id=%s AND lease_token=%s
                AND status='RUNNING' AND lease_until>now() FOR UPDATE''', (claimed['id'], claimed['lease_token']))).fetchone()
            if task is None:
                return False
            row = await (await conn.execute('''UPDATE chore_steps SET external_job_failures=external_job_failures+1
                WHERE task_id=%s AND position=%s RETURNING external_job_failures,external_job_deadline>now() AS timely''',
                (task['id'], task['current_step']))).fetchone()
            await conn.execute('UPDATE chore_tasks SET lease_token=NULL,lease_until=NULL WHERE id=%s', (task['id'],))
            if row['external_job_failures']<3 and row['timely']:
                await conn.execute("UPDATE chore_steps SET status='PENDING' WHERE task_id=%s AND position=%s",
                                   (task['id'], task['current_step']))
                await self.queue(conn, task['id'], mode='POLL', delay=2**row['external_job_failures'])
                await self.event(conn, task['id'], 'JOB_POLL_RETRY')
            else:
                await self.job_unknown(conn, task['id'], task['current_step'], 'UNAVAILABLE' if row['timely'] else 'TIMED_OUT')
            return True

    async def job_unknown(self, conn, task, position, state):
        await conn.execute("""UPDATE chore_steps SET external_job_state=%s,status='UNKNOWN'
            WHERE task_id=%s AND position=%s""", (state, task, position))
        await conn.execute("""UPDATE chore_tasks SET status='OUTCOME_UNKNOWN',lease_token=NULL,lease_until=NULL,
            updated_at=now() WHERE id=%s""", (task,))
        await conn.execute('DELETE FROM chore_jobs WHERE task_id=%s', (task,))
        await self.event(conn, task, 'JOB_'+state, {'position': position, 'resubmitted': False})

    async def job_failed(self, claimed, failure):
        from psycopg.types.json import Jsonb
        async with self.pool.connection() as conn, conn.transaction():
            task = await (await conn.execute('''SELECT * FROM chore_tasks WHERE id=%s AND lease_token=%s
                AND status='RUNNING' AND lease_until>now() FOR UPDATE''', (claimed['id'], claimed['lease_token']))).fetchone()
            if task is None:
                return False
            await conn.execute("""UPDATE chore_steps SET external_job_state='FAILED',status='FAILED',
                outcome_pending=false,result=%s WHERE task_id=%s AND position=%s""",
                (Jsonb(failure.evidence), task['id'], task['current_step']))
            await conn.execute("""UPDATE chore_tasks SET status='FAILED',lease_token=NULL,lease_until=NULL,
                updated_at=now() WHERE id=%s""", (task['id'],))
            await self.event(conn, task['id'], 'JOB_FAILED', {'position': task['current_step']})
            return True
