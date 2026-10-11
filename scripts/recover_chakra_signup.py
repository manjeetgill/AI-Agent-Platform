"""Record an independently checked provider outcome. Never pass secrets as evidence."""
import argparse
import asyncio
import os
from uuid import UUID

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from agent_platform.chakra_recovery import review
from agent_platform.db_scope import assert_runtime_role


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('session_id', type=UUID)
    parser.add_argument('--reference', required=True, help='Non-secret support ticket or operator review reference')
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--verified-plugin', type=UUID)
    group.add_argument('--verified-no-connection', action='store_true')
    args = parser.parse_args()
    async with AsyncConnectionPool(os.environ['AI_PLATFORM_DATABASE_URL'], open=False, kwargs={'row_factory':dict_row}) as pool:
        await assert_runtime_role(pool, 'worker')
        await review(pool, args.session_id, reference=args.reference, plugin_id=args.verified_plugin,
                     no_connection=args.verified_no_connection)
    print('Review saved. The owning app may renew this signup once; no token or number was created.')


if __name__ == '__main__':
    asyncio.run(main())
