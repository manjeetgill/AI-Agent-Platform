"""Operator-only rate/limit configuration; no payment is made by this command.

Read connection settings from the private process environment. Rates must be
verified against the provider contract. Use a quote/reference, never credentials.
"""
import argparse
import asyncio
import json
import os
from decimal import Decimal
from pathlib import Path
from uuid import UUID

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from agent_platform.voice_reconciliation import Charge, reconcile


def amount(value):
    result = Decimal(value)
    if not result.is_finite() or result < 0:
        raise argparse.ArgumentTypeError('Use a finite nonnegative amount')
    return result


async def run(args):
    async with AsyncConnectionPool(os.environ['AI_PLATFORM_DATABASE_URL'], open=False, kwargs={'row_factory': dict_row}) as pool, pool.connection() as conn:
        if args.command == 'reconcile':
            lines=json.loads(Path(args.file).read_text())
            result=await reconcile(conn,[Charge.model_validate(line) for line in lines])
            print(json.dumps(result))
            return
        if args.command == 'rate':
            if args.provider == 'local':
                raise ValueError('Local external API fees are automatically zero; infrastructure is separate')
            await conn.execute('''INSERT INTO voice_rate_cards(provider,model,stage,currency,per_unit,reference,expires_at)
                VALUES(%s,%s,%s,%s,%s,%s,now()+(%s*interval '1 day'))
                ON CONFLICT(provider,model,stage) DO UPDATE SET currency=excluded.currency,per_unit=excluded.per_unit,
                reference=excluded.reference,expires_at=excluded.expires_at,verified_at=now()''',
                (args.provider,args.model,args.stage,args.currency,args.per_unit,args.reference,args.valid_days))
        else:
            if args.monthly < args.daily:
                raise ValueError('Monthly limit must be at least daily limit')
            # Match the lock order used before dispatch. No currency change with historical rows.
            await conn.execute('SELECT id FROM platform_apps WHERE id=%s FOR UPDATE', (args.app,))
            old = await (await conn.execute('SELECT 1 FROM voice_usage_ledger WHERE app_id=%s AND currency<>%s LIMIT 1', (args.app,args.currency))).fetchone()
            if old:
                raise ValueError('Existing usage has another currency; reconciliation is required')
            await conn.execute('''INSERT INTO voice_spend_limits(app_id,currency,daily,monthly) VALUES(%s,%s,%s,%s)
                ON CONFLICT(app_id) DO UPDATE SET currency=excluded.currency,daily=excluded.daily,monthly=excluded.monthly''',
                (args.app,args.currency,args.daily,args.monthly))
    print('Voice accounting configuration saved. No payment or provider request made.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    reconciliation = commands.add_parser('reconcile', help='Import reviewed provider charges; no payment or provider request')
    reconciliation.add_argument('--file', required=True, help='Private JSON array of Charge records; never include credentials')
    rate = commands.add_parser('rate')
    rate.add_argument('--provider', required=True, choices=['gemini','sarvam','nvidia','openrouter','together'])
    rate.add_argument('--model', required=True)
    rate.add_argument('--stage', required=True, choices=['asr','tts','reasoning'])
    rate.add_argument('--per-unit', required=True, type=amount, help='Per audio second / character / total token. Use a conservative contract rate.')
    rate.add_argument('--reference', required=True)
    rate.add_argument('--valid-days', type=int, choices=range(1,32), default=7)
    limits = commands.add_parser('limits')
    limits.add_argument('--app', required=True, type=UUID)
    limits.add_argument('--daily', required=True, type=amount)
    limits.add_argument('--monthly', required=True, type=amount)
    for cmd in (rate, limits):
        cmd.add_argument('--currency', required=True, choices=['INR','USD','EUR','GBP'])
    asyncio.run(run(parser.parse_args()))
