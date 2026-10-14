"""Explicit, same-application FAQ specialist routing; never a telephone transfer.

Only configured exact requests select a specialist. Tools, credentials, spending
limits, deadlines and speech/model settings remain with the original session.
A target is checked for caller visibility and current enabled status, then pinned.
"""
import re

from psycopg.types.json import Jsonb

from . import voice_events


async def select(pool, root, call, text):
    from .web_voice import action_handler, snapshot
    normalized = re.sub(r'[.!?]+$', '', text.strip().lower()).strip()
    routes = root.get('config', {}).get('specialists', [])
    matches = [r for r in routes if normalized == 'talk to '+r['name']]
    if len(matches) > 1:
        raise ValueError('Ambiguous specialist name')
    requested = matches[0]['agent_id'] if matches else None
    back = normalized == 'talk to main assistant'
    async with pool.connection() as conn, conn.transaction():
        state = await (await conn.execute('SELECT * FROM web_voice_sessions WHERE id=%s FOR UPDATE', (call['id'],))).fetchone()
        if not state or str(state['app_id']) != str(root['app_id']) or str(state['owner_id']) != str(call['owner_id']):
            raise ValueError('Voice session scope changed')
        if requested or back:
            if state['specialist_hops'] >= 3:
                raise ValueError('Specialist handoff limit reached')
            if back:
                target = None
            else:
                row = await (await conn.execute('''SELECT * FROM web_voice_agents WHERE id=%s AND app_id=%s
                    AND enabled AND deleted_at IS NULL AND (owner_id=%s OR visibility='app')''', (requested,root['app_id'],call['owner_id']))).fetchone()
                if not row or str(row['id']) == str(root['id']) or action_handler(row):
                    raise ValueError('Specialist unavailable or requires business action authority')
                target = snapshot(row)
                # Delegation changes FAQ grounding only, never runtime policy/provider costs.
                target['config'] = root['config']
                target['language'] = root.get('language', 'auto')
                target['retain_content'] = False
            await conn.execute('UPDATE web_voice_sessions SET specialist_snapshot=%s,specialist_hops=specialist_hops+1 WHERE id=%s', (Jsonb(target) if target else None, call['id']))
            hop = state['specialist_hops'] + 1
        else:
            target = state['specialist_snapshot']
            if target:
                allowed = await (await conn.execute('''SELECT 1 FROM web_voice_agents WHERE id=%s AND app_id=%s AND enabled
                    AND deleted_at IS NULL AND (owner_id=%s OR visibility='app')''', (target['id'],root['app_id'],call['owner_id']))).fetchone()
                if not allowed or action_handler(target):
                    raise ValueError('Specialist access has been revoked')
    if requested or back:
        await voice_events.record(pool, call['id'], 'specialist_handoff', {'target_agent_id':str(target['id'] if target else root['id']), 'hop':hop})
        name = matches[0]['name'] if matches else 'main assistant'
        return target or root, {'action':'continue', 'text':f'You are now speaking with the {name} AI assistant. How can I help?', 'reset_history':True}
    return target or root, None
