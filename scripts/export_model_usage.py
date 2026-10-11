"""Aggregate real migration-039 usage; never export prompts, user IDs or secrets."""
import argparse
import json
import os
from datetime import UTC, datetime
from pathlib import Path

import psycopg
from psycopg.rows import dict_row


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    with psycopg.connect(os.environ['AI_PLATFORM_DATABASE_URL'],row_factory=dict_row) as conn:
        conn.execute('SET TRANSACTION READ ONLY')
        rows=conn.execute("""SELECT (created_at AT TIME ZONE 'UTC')::date AS day,coalesce(provider,'Unknown') AS provider,
            coalesce(model,'Unknown') AS model,count(*) AS calls,count(*) FILTER(WHERE status='FAILED') AS failures,
            sum(total_tokens) AS tokens,count(*) FILTER(WHERE total_tokens IS NULL) AS unknown_tokens,
            sum(estimated_cost) AS estimated_cost,count(*) FILTER(WHERE estimated_cost IS NULL) AS unknown_cost
            FROM platform_model_calls GROUP BY 1,2,3 ORDER BY 1,2,3""").fetchall()
    output={'exported_at':datetime.now(UTC).isoformat(),'source':'platform_model_calls, migration 039','currency':None,'rows':rows}
    fd=os.open(args.output,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'w') as stream:
        json.dump(output,stream,default=str,indent=2)
    print('Usage aggregates exported; unknown cost remains unknown.')


if __name__=='__main__':
    main()
