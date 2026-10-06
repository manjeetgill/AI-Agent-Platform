"""Trusted operator: verify app-owned carrier evidence or set app limits. No HTTP admin bypass."""

import argparse
import asyncio
import os
from uuid import UUID

from psycopg.errors import UniqueViolation
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from agent_platform.telephony import verify_carrier


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("verify", "revoke"):
        cmd = sub.add_parser(name)
        cmd.add_argument("connection_id", type=UUID)
        cmd.add_argument(
            "--evidence-reference", required=True, help="Carrier authorization/routing ticket; no secrets"
        )
        cmd.add_argument("--operator", required=True)
    quota = sub.add_parser("limits")
    quota.add_argument("app_id", type=UUID)
    quota.add_argument("--concurrent", required=True, type=int, choices=range(1, 101))
    quota.add_argument("--daily", required=True, type=int)
    purge = sub.add_parser("purge")
    purge.add_argument("--older-than-days", type=int, default=30)
    args = parser.parse_args()
    async with AsyncConnectionPool(
        os.environ["AI_PLATFORM_DATABASE_URL"], open=False, kwargs={"row_factory": dict_row}
    ) as pool:
        if args.command in ("verify", "revoke"):
            try:
                await verify_carrier(
                    pool,
                    args.connection_id,
                    args.evidence_reference,
                    actor=args.operator,
                    verified=args.command == "verify",
                )
            except UniqueViolation:
                raise SystemExit(
                    "Number is already verified on another connection; review before transferring it."
                ) from None
            print(
                "Carrier evidence updated. Connection paused; gateway checks and explicit activation are required."
            )
        elif args.command == "limits":
            if not 1 <= args.daily <= 100000:
                parser.error("daily must be 1..100000")
            async with pool.connection() as conn:
                await conn.execute(
                    """INSERT INTO telephony_app_limits(app_id,concurrent_calls,daily_calls)
                    VALUES(%s,%s,%s) ON CONFLICT(app_id) DO UPDATE SET
                    concurrent_calls=EXCLUDED.concurrent_calls,daily_calls=EXCLUDED.daily_calls""",
                    (args.app_id, args.concurrent, args.daily),
                )
            print("App-wide limits updated.")
        else:
            if args.older_than_days < 1:
                parser.error("Retention must be at least one day (usage quotas use a rolling 24-hour window)")
            async with pool.connection() as conn:
                result = await conn.execute(
                    "DELETE FROM telephony_sessions WHERE created_at<now()-(%s * interval '1 day')",
                    (args.older_than_days,),
                )
            print(f"Deleted {result.rowcount} expired call records and their transcripts.")


if __name__ == "__main__":
    asyncio.run(main())
