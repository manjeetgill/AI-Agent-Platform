"""Atomic, expiring pilot quotas. Reserve before any external action; never refund uncertainty."""
from datetime import UTC, datetime

RESOURCES = {'whatsapp_turns', 'whatsapp_sends', 'voice_calls'}


async def reserve(conn, app_id, resource, operation_id):
    """Call inside a transaction committed before external dispatch. False means no dispatch."""
    if resource not in RESOURCES:
        raise ValueError('Unknown trial resource')
    policy = await (await conn.execute(
        'SELECT * FROM channel_trial_policies WHERE app_id=%s FOR UPDATE', (app_id,)
    )).fetchone()
    if not policy:
        return True  # Non-pilot: existing product quotas still apply.
    if policy['expires_at'] <= datetime.now(UTC):
        return False
    prior = await (await conn.execute(
        'SELECT 1 FROM channel_trial_reservations WHERE app_id=%s AND resource=%s AND operation_id=%s',
        (app_id, resource, operation_id),
    )).fetchone()
    if prior:
        return False
    await conn.execute('INSERT INTO channel_trial_usage(app_id,resource) VALUES(%s,%s) ON CONFLICT DO NOTHING',
                       (app_id, resource))
    admitted = await (await conn.execute(
        """UPDATE channel_trial_usage SET units=units+1
        WHERE app_id=%s AND resource=%s AND day=(now() AT TIME ZONE 'UTC')::date AND units<%s
        RETURNING units""", (app_id, resource, policy[resource]),
    )).fetchone()
    if not admitted:
        return False
    await conn.execute('INSERT INTO channel_trial_reservations(app_id,resource,operation_id) VALUES(%s,%s,%s)',
                       (app_id, resource, operation_id))
    return True


async def reserve_committed(pool, app_id, resource, operation_id):
    async with pool.connection() as conn, conn.transaction():
        return await reserve(conn, app_id, resource, operation_id)
