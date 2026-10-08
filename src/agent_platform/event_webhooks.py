"""Durable, signed, at-least-once callbacks to operator-configured destinations."""
import hashlib
import hmac
import json
import os
import time
from urllib.parse import urlparse
from uuid import uuid4

import httpx

from .automation import scope


def destination(app_id, name):
    # Exact app + name mapping; callers cannot supply a URL or a secret.
    mapping = json.loads(os.environ.get('AI_EVENT_WEBHOOK_DESTINATIONS', '{}'))
    settings = mapping.get(str(app_id), {}).get(name)
    if not isinstance(settings, dict):
        raise ValueError('Webhook destination is not configured for this application')  # noqa: TRY004
    url = settings.get('url', '')
    parsed = urlparse(url)
    key = os.environ.get(settings.get('secret_env', ''), '')
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.fragment or len(key) < 32:
        raise ValueError('HTTPS destination and private signing secret are required')
    return url, key


def sign(secret, timestamp, body):
    return 'sha256=' + hmac.new(secret.encode(), str(timestamp).encode()+b'.'+body, hashlib.sha256).hexdigest()


def verify(secret, timestamp, body, signature, *, now=None):
    try:
        if abs((time.time() if now is None else now)-int(timestamp)) > 300:
            return False
    except (ValueError, TypeError):
        return False
    return isinstance(signature, str) and hmac.compare_digest(sign(secret, timestamp, body), signature)


async def fanout(pool):
    async with pool.connection() as conn, conn.transaction():
        await scope(conn, worker=True)
        rows = await (await conn.execute('''SELECT w.* FROM automation_webhooks w WHERE enabled
            AND EXISTS(SELECT 1 FROM automation_events e WHERE e.app_id=w.app_id AND e.owner_id=w.owner_id
                AND e.event_type=w.event_type AND e.version=w.event_version AND e.sequence>w.last_sequence)
            ORDER BY created_at FOR UPDATE OF w SKIP LOCKED LIMIT 10''')).fetchall()
        for row in rows:
            events = await (await conn.execute('''SELECT * FROM automation_events WHERE app_id=%s AND owner_id=%s
                AND event_type=%s AND version=%s AND sequence>%s ORDER BY sequence LIMIT 20''',
                (row['app_id'], row['owner_id'], row['event_type'], row['event_version'], row['last_sequence']))).fetchall()
            for event in events:
                await conn.execute('''INSERT INTO automation_deliveries(id,tenant_id,app_id,owner_id,webhook_id,event_id)
                    VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT(webhook_id,event_id) DO NOTHING''',
                    (uuid4(), row['tenant_id'], row['app_id'], row['owner_id'], row['id'], event['id']))
            if events:
                await conn.execute('UPDATE automation_webhooks SET last_sequence=%s WHERE id=%s', (events[-1]['sequence'], row['id']))
        return bool(rows)


async def deliver(pool, *, transport=None):
    async with pool.connection() as conn, conn.transaction():
        await scope(conn, worker=True)
        await conn.execute("""UPDATE automation_deliveries SET status='QUEUED',lease_token=NULL,lease_until=NULL
            WHERE status='RUNNING' AND lease_until<now()""")
        row = await (await conn.execute('''SELECT d.*,w.destination,w.enabled,e.event_type,e.version,e.source,
            e.correlation_id,e.created_at,e.payload FROM automation_deliveries d
            JOIN automation_webhooks w ON w.id=d.webhook_id JOIN automation_events e ON e.id=d.event_id
            WHERE d.status='QUEUED' AND d.next_at<=now() ORDER BY d.next_at
            FOR UPDATE OF d SKIP LOCKED LIMIT 1''')).fetchone()
        if row is None:
            return False
        if not row['enabled'] or row['attempts'] >= 5:
            await conn.execute('UPDATE automation_deliveries SET status=%s WHERE id=%s',
                               ('CANCELLED' if not row['enabled'] else 'DEAD', row['id']))
            return True
        token = uuid4()
        await conn.execute("""UPDATE automation_deliveries SET status='RUNNING',attempts=attempts+1,
            lease_token=%s,lease_until=now()+interval '30 seconds' WHERE id=%s""", (token, row['id']))
    status = None
    try:
        url, secret = destination(row['app_id'], row['destination'])
        body = json.dumps({'event_id': str(row['event_id']), 'event_type': row['event_type'],
            'version': row['version'], 'tenant_id': row['tenant_id'], 'application_id': str(row['app_id']),
            'source': row['source'], 'timestamp': row['created_at'].isoformat(),
            'correlation_id': row['correlation_id'], 'payload': row['payload']}, separators=(',', ':')).encode()
        timestamp = str(int(time.time()))
        async with httpx.AsyncClient(transport=transport, timeout=8, follow_redirects=False, trust_env=False) as client:
            response = await client.post(url, content=body, headers={'Content-Type': 'application/json',
                'X-Agent-Timestamp': timestamp, 'X-Agent-Signature': sign(secret, timestamp, body),
                'Idempotency-Key': str(row['event_id'])})
        status = response.status_code
    except (ValueError, httpx.HTTPError):
        pass  # Credentials, receiver bodies and network internals are never logged.
    success = status is not None and 200 <= status < 300
    async with pool.connection() as conn, conn.transaction():
        await scope(conn, worker=True)
        await conn.execute('''UPDATE automation_deliveries SET status=%s,http_status=%s,
            delivered_at=CASE WHEN %s THEN now() ELSE NULL END,lease_token=NULL,lease_until=NULL,
            next_at=now()+%s*interval '1 second' WHERE id=%s AND lease_token=%s''',
            ('DELIVERED' if success else 'DEAD' if row['attempts']>=4 else 'QUEUED', status, success,
             min(3600, 2**(row['attempts']+1)*15), row['id'], token))
    return True
