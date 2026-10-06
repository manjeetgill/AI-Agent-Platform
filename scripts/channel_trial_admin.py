"""Set explicit pilot expiry and per-UTC-day operation caps. No provider recharge."""
import argparse
import asyncio
import os
from datetime import UTC, datetime, timedelta
from uuid import UUID

from psycopg_pool import AsyncConnectionPool


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('app_id', type=UUID)
    parser.add_argument('--days', type=int, default=7)
    parser.add_argument('--whatsapp-turns', type=int, default=25)
    parser.add_argument('--whatsapp-sends', type=int, default=25)
    parser.add_argument('--voice-calls', type=int, default=5)
    args = parser.parse_args()
    if not 1 <= args.days <= 30 or any(not 0 <= v <= 100000 for v in
        (args.whatsapp_turns, args.whatsapp_sends, args.voice_calls)):
        parser.error('days must be 1..30 and limits 0..100000')
    async with AsyncConnectionPool(os.environ['AI_PLATFORM_DATABASE_URL'], open=False) as pool, pool.connection() as conn:
        await conn.execute('''INSERT INTO channel_trial_policies VALUES(%s,%s,%s,%s,%s)
          ON CONFLICT(app_id) DO UPDATE SET expires_at=EXCLUDED.expires_at,
          whatsapp_turns=EXCLUDED.whatsapp_turns,whatsapp_sends=EXCLUDED.whatsapp_sends,
          voice_calls=EXCLUDED.voice_calls''',
          (args.app_id, datetime.now(UTC)+timedelta(days=args.days),args.whatsapp_turns,args.whatsapp_sends,args.voice_calls))
    print('Pilot limits saved. Existing usage retained; expiry requires explicit operator renewal.')


if __name__ == '__main__':
    asyncio.run(main())
