"""Schema classification coverage; this does not assert unfinished RLS is deployed."""
import importlib.util
import json
from pathlib import Path

import psycopg
from psycopg.rows import dict_row
from test_durable_tasks import database

ROOT=Path(__file__).resolve().parents[2]


def test_every_table_has_a_reviewed_classification_and_existing_rls_is_not_removed():
    spec=importlib.util.spec_from_file_location('security_inventory',ROOT/'scripts/security_inventory.py')
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with psycopg.connect(database(),row_factory=dict_row) as conn:
        actual={r['name']:r for r in module.inventory(conn)}
    saved={r['name']:r for r in json.loads((ROOT/'docs/security/table-inventory.json').read_text())['tables']}
    assert actual.keys()==saved.keys(), 'Classify new/removed tables in the security inventory before merging.'
    for name,row in actual.items():
        assert row['classification']==saved[name]['classification']!='UNCLASSIFIED'
        assert row['scope_columns']==saved[name]['scope_columns']
        if saved[name]['rls']:
            assert row['rls'] and row['forced'], f'Existing RLS was removed from {name}'
