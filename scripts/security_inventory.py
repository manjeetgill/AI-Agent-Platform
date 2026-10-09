"""Generate a schema-only isolation inventory; never export row data or credentials."""
import argparse
import ast
import json
import os
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

ROOT = Path(__file__).resolve().parents[1]
GLOBAL = {
    'agent_schema_migrations': 'Immutable schema checksums; migration operator only.',
    'platform_worker_heartbeats': 'Global worker liveness and tool versions; no business rows.',
    'whatsapp_worker_health': 'Global worker liveness.',
    'whatsapp_call_worker_health': 'Global media runtime liveness.',
    'meta_webhook_receipts': 'Global duplicate webhook digest and timestamp, no payload.',
    'meta_platform_webhook': 'Operator Meta developer-app webhook configuration, not a customer app ID.',
}


def inventory(conn):
    rows = conn.execute('''SELECT t.relname AS name,t.relrowsecurity AS rls,t.relforcerowsecurity AS forced,
        array_agg(a.attname ORDER BY a.attnum) AS columns
        FROM pg_class t JOIN pg_namespace n ON n.oid=t.relnamespace
        JOIN pg_attribute a ON a.attrelid=t.oid AND a.attnum>0 AND NOT a.attisdropped
        WHERE n.nspname='public' AND t.relkind='r' GROUP BY t.oid ORDER BY t.relname''').fetchall()
    edges = conn.execute('''SELECT c.conrelid::regclass::text AS child,c.confrelid::regclass::text AS parent
        FROM pg_constraint c JOIN pg_namespace n ON n.oid=c.connamespace
        WHERE n.nspname='public' AND c.contype='f' ORDER BY 1,2''').fetchall()
    tables = {r['name']: r for r in rows}
    for row in rows:
        columns = row.pop('columns')
        row['scope_columns'] = [c for c in columns if c in {'tenant_id','app_id','owner_id','source_app_id','parent_app_id'}]
        row['parents'] = sorted({e['parent'] for e in edges if e['child']==row['name']})
        if row['name']=='whatsapp_dispatches':
            row['parents']=['whatsapp_messages']
            row['logical_reference_note']='message_id has no declared foreign key; ledger retention must be reviewed.'
        if row['name'] in GLOBAL:
            row.update(classification='global/operator', reason=GLOBAL[row['name']])
        elif 'tenant_id' in columns or row['name']=='platform_organisations':
            row.update(classification='tenant', reason='Direct tenant identity; owner/app filters also required where present.')
        elif 'app_id' in columns or 'parent_app_id' in columns:
            row.update(classification='app', reason='Resolve app ownership through platform_apps; preserve owner/private sharing rules.')
    for _ in rows:
        for row in rows:
            parents=[p for p in row['parents'] if tables.get(p,{}).get('classification') in {'tenant','app'}]
            if 'classification' not in row and parents:
                row.update(classification=tables[parents[0]]['classification'],
                           reason='Inherited ownership through foreign-key parent '+parents[0]+'.')
    for row in rows:
        if 'classification' not in row:
            row.update(classification='UNCLASSIFIED', reason='Requires explicit operator review.')
    return rows


def connection_sites():
    result=[]
    for path in sorted((ROOT/'src/agent_platform').glob('*.py')):
        tree=ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr=='connection':
                result.append({'file':str(path.relative_to(ROOT)), 'line':node.lineno})
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, help='Private existing local configuration; URL is never printed')
    parser.add_argument('--output', type=Path, default=ROOT/'docs/security/table-inventory.json')
    args=parser.parse_args()
    url=os.environ.get('AI_PLATFORM_DATABASE_URL')
    if args.config:
        if args.config.is_symlink() or args.config.stat().st_mode & 0o077:
            raise SystemExit('Configuration must be a private regular file (0600).')
        url=json.loads(args.config.read_text())['AI_PLATFORM_DATABASE_URL']
    with psycopg.connect(url, row_factory=dict_row) as conn:
        rows=inventory(conn)
    report={'tables':rows,'connection_sites_to_audit':connection_sites(),
            'summary':{'tables':len(rows),'without_rls':sum(not r['rls'] for r in rows),
                       'unclassified':sum(r['classification']=='UNCLASSIFIED' for r in rows)},
            'note':'Classification is an inventory, not proof of runtime isolation. Connection sites require scope review.'}
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(report['summary']))
    return 1 if report['summary']['unclassified'] else 0


if __name__=='__main__':
    raise SystemExit(main())
