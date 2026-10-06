"""Operator-bound clinic workflow transport; never falls back to a generic model."""
import hashlib
import hmac
import json
import os
import time
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import httpx


def binding(channel, value=None):
    if value is None:
        value = json.loads(os.environ.get('AI_SAHAYAK_BRIDGES', '{}')).get(str(channel['id']))
    if not isinstance(value, dict) or value.get('app_id') != str(channel['app_id']):
        raise ValueError('Clinic bridge not configured')
    url = urlsplit(value.get('url', ''))
    if (url.scheme != 'https' and not (url.scheme == 'http' and url.hostname in {'127.0.0.1', 'localhost'})) or url.username or url.password or url.query or url.fragment:
        raise ValueError('Invalid bridge endpoint')
    UUID(value['clinic_id'])
    if not isinstance(value.get('secret'), str) or len(value['secret']) < 32:
        raise ValueError('Invalid bridge credential')
    return value


def context(channel):
    return ':'.join(map(str, ('sahayak', channel['app_id'], channel['owner_id'], channel['id'])))


async def configured(pool, channel):
    from .meta_crypto import unseal
    async with pool.connection() as conn:
        row = await (await conn.execute('SELECT config_cipher FROM sahayak_channel_bridges WHERE channel_id=%s', (channel['id'],))).fetchone()
    return binding(channel, json.loads(unseal(row['config_cipher'], context(channel)))) if row else binding(channel)


async def request(channel, event_id, customer, text, *, probe=False, transport=None, pool=None, voice_available=False, voice_resume=False, voice_turn=False):
    cfg = await configured(pool, channel) if pool is not None else binding(channel)
    payload = {'event_id': str(event_id), 'channel_id': str(channel['id']),
               'app_id': str(channel['app_id']), 'clinic_id': cfg['clinic_id'],
               'from': customer, 'text': text}
    if probe:
        payload['probe'] = True
    if voice_available:
        payload['voice_available'] = True
    if voice_resume:
        payload['voice_resume'] = True
    if voice_turn:
        payload['voice_turn'] = True
    raw = json.dumps(payload, separators=(',', ':'), ensure_ascii=False).encode()
    stamp = str(int(time.time()))
    signature = hmac.new(cfg['secret'].encode(), stamp.encode()+b'.'+raw, hashlib.sha256).hexdigest()
    async with httpx.AsyncClient(timeout=httpx.Timeout(40, connect=3), follow_redirects=False, transport=transport) as client:
        response = await client.post(cfg['url'], content=raw, headers={
            'Content-Type': 'application/json', 'X-Agent-Timestamp': stamp, 'X-Agent-Signature': signature})
        response.raise_for_status()
        result = response.json()
    if probe:
        if result.get('ready') is not True or result.get('clinic_id') != cfg['clinic_id']:
            raise ValueError('Clinic bridge not ready')
        return result
    if result.get('event_id') != str(event_id) or result.get('handledBy') not in {'agent', 'human'} or not isinstance(result.get('redFlag'), bool):
        raise ValueError('Invalid bridge response')
    replies = result.get('replies')
    if not isinstance(replies, list) or len(replies) > 10 or any(not isinstance(x, str) for x in replies):
        raise ValueError('Invalid bridge replies')
    if len('\n\n'.join(replies)) > 4000:
        raise ValueError('Bridge reply too long')
    return result


async def probe(channel, pool=None):
    return await request(channel, uuid4(), '10000000000', 'connection check', probe=True, pool=pool)
