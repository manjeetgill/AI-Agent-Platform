"""Operator-reviewed renewal of an expired signup, retaining its app and session ID."""
from datetime import UTC, datetime
from uuid import UUID

from psycopg.types.json import Jsonb


def view(row):
    expired = row['expires_at'] <= datetime.now(UTC)
    review = row.get('recovery_review')
    reviewed = bool(review and not row.get('recovery_consumed_at'))
    state = row['state']
    action = ('connected' if state == 'CONNECTED' else
              'ownership_review' if state == 'OWNERSHIP_CHECK_REQUIRED' else
              'resume' if state == 'CONNECTING' and not expired else
              'renew' if reviewed and expired else 'operator_review')
    return {'id': row['id'], 'state': state, 'expires_at': row['expires_at'],
            'authorization_expired': expired, 'next_action': action,
            'message': {
                'connected': 'Number connected. Continue channel setup.',
                'ownership_review': 'Authorization received. The setup team must verify number ownership.',
                'resume': 'Continue the saved authorization. No new provider signup is needed.',
                'renew': 'The setup team reviewed the provider state. Renew authorization for this saved signup.',
                'operator_review': 'Authorization expired or its outcome is uncertain. The setup team must check Chakra before renewing; do not create another signup.',
            }[action]}


async def review(pool, session_id, *, reference, plugin_id=None, no_connection=False):
    """Only the operator CLI calls this, after inspecting the actual Chakra account."""
    if not 8 <= len(reference) <= 200 or any(ord(c) < 32 for c in reference):
        raise ValueError('Provide a short non-secret review reference')
    if (plugin_id is None) == (not no_connection):
        raise ValueError('Select a verified existing plugin or confirm no provider connection exists')
    plugin = str(UUID(str(plugin_id))) if plugin_id else None
    async with pool.connection() as conn, conn.transaction():
        row = await (await conn.execute('SELECT * FROM chakra_signup_sessions WHERE id=%s FOR UPDATE', (session_id,))).fetchone()
        if not row or row['expires_at'] > datetime.now(UTC) or row['state'] not in {'CONNECTING','CHECK_REQUIRED','CREATING'}:
            raise ValueError('Only expired, unresolved signups can be reviewed')
        if row['candidate_plugin_id'] and str(row['candidate_plugin_id']) != plugin:
            raise ValueError('Review must preserve the recorded provider plugin')
        if await (await conn.execute('SELECT id FROM chakra_customer_connections WHERE id=%s OR plugin_id=%s', (session_id,plugin))).fetchone():
            raise ValueError('Existing connected bindings require connection management, not signup renewal')
        result = {'reference':reference,'plugin_id':plugin,'no_connection':no_connection,
                  'reviewed_at':datetime.now(UTC).isoformat()}
        await conn.execute('UPDATE chakra_signup_sessions SET recovery_review=%s,recovery_consumed_at=NULL,recovery_history=recovery_history || %s WHERE id=%s',
                           (Jsonb(result),Jsonb([result]),session_id))
