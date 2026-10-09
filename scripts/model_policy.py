"""Operator-only app model policy setup; requires the private worker DB environment."""
import argparse
import asyncio
import json
import os
from uuid import UUID

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from agent_platform.db_scope import assert_runtime_role, worker_connection
from agent_platform.provider_chain import configuration


async def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--app-id',type=UUID,required=True)
    parser.add_argument('--daily-microusd',type=int)
    parser.add_argument('--daily-calls',type=int)
    parser.add_argument('--sensitivity',choices=['public','internal','restricted'],default='restricted')
    parser.add_argument('--provider',action='append',default=[])
    parser.add_argument('--report',action='store_true')
    args=parser.parse_args()
    async with AsyncConnectionPool(os.environ['AI_PLATFORM_DATABASE_URL'],open=False,configure=worker_connection,kwargs={'row_factory':dict_row}) as pool:
        await assert_runtime_role(pool,'worker')
        async with pool.connection() as conn:
            if args.report:
                rows=await (await conn.execute('''SELECT provider,model,status,count(*) AS calls,
                    sum(total_tokens) AS tokens,sum(estimated_microusd) AS estimated_microusd,
                    sum(reserved_microusd) FILTER(WHERE estimated_microusd IS NULL) AS unresolved_reserved_microusd
                    FROM provider_chain_attempts WHERE app_id=%s GROUP BY 1,2,3 ORDER BY 1,2,3''',(args.app_id,))).fetchall()
                print(json.dumps(rows,default=str))
                return
            if (args.daily_microusd is None or args.daily_microusd<0 or args.daily_calls is None
                    or args.daily_calls<0 or not args.provider):
                parser.error('Explicit nonnegative daily limits and providers are required')
            allowed={e.provider for e in configuration()}
            if not set(args.provider)<=allowed:
                parser.error('Each provider must exist in AI_MODEL_PROVIDER_CHAIN')
            if not await (await conn.execute('SELECT 1 FROM platform_apps WHERE id=%s',(args.app_id,))).fetchone():
                parser.error('Unknown application')
            await conn.execute('''INSERT INTO platform_model_policies(app_id,daily_budget_microusd,daily_calls,allowed_providers,sensitivity)
                VALUES(%s,%s,%s,%s,%s) ON CONFLICT(app_id) DO UPDATE SET daily_budget_microusd=excluded.daily_budget_microusd,
                daily_calls=excluded.daily_calls,allowed_providers=excluded.allowed_providers,sensitivity=excluded.sensitivity''',
                (args.app_id,args.daily_microusd,args.daily_calls,args.provider,args.sensitivity))
    print('Application model policy saved.')


if __name__=='__main__':
    asyncio.run(main())
