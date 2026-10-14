import hashlib
import json
import sqlite3

import pytest

pa = pytest.importorskip('pyarrow')
from test_financial_collector import HTML

from agent_platform import financial_collector as fc
from agent_platform.financial_parquet import ParquetStore, hydrate, migrate


def test_parquet_collection_no_database_and_selective_hydration(tmp_path, monkeypatch):
    root = tmp_path / 'archive'
    store = ParquetStore(root)
    monkeypatch.setattr(fc, 'fetch', lambda client, url, hosts, limit: (HTML.encode(), url))
    first = fc.collect(None, store, root, 'INFY', 0)
    assert fc.collect(None, store, root, 'INFY', 0)['status'] == 'unchanged'
    fc.collect(None, store, root, 'TCS', 0)
    assert not list(root.glob('*.sqlite3')) and not list(root.glob('*-knowledge.jsonl'))
    row = store.latest('INFY')
    assert row['id'] == first['snapshot']
    facts = json.loads(row['payload'])['facts']
    assert facts[0]['value'] == '1234.50' and facts[1]['value'] is None
    target = tmp_path / 'cache.sqlite3'
    assert hydrate(root, target, ['INFY']) == 1
    assert hydrate(root, target, ['INFY']) == 1
    with sqlite3.connect(target) as db:
        assert db.execute('SELECT symbol FROM financial_cache').fetchall() == [('INFY',)]
    with pytest.raises(ValueError):
        hydrate(root, target, ['TCS', 'MISSING'])
    with sqlite3.connect(target) as db:
        assert db.execute('SELECT count(*) FROM financial_cache').fetchone()[0] == 1
    with pytest.raises(ValueError):
        store.latest('../outside')


def test_migration_preserves_source_and_validates_all_rows(tmp_path, monkeypatch):
    old = tmp_path / 'old'
    monkeypatch.setattr(fc, 'fetch', lambda client, url, hosts, limit: (HTML.encode(), url))
    db = fc.initialize(old)
    fc.collect(None, db, old, 'INFY', 0)
    db.close()
    source = old / 'catalogue.sqlite3'
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    archive = tmp_path / 'archive'
    assert migrate(source, archive) == 1
    assert migrate(source, archive) == 1
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before
    store = ParquetStore(archive)
    row = store.latest('INFY')
    (store.folder('INFY') / row['id'] / 'facts.parquet').write_bytes(b'corrupt')
    with pytest.raises(pa.ArrowInvalid):
        hydrate(archive, tmp_path / 'bad.sqlite3', ['INFY'])
    assert not (tmp_path / 'bad.sqlite3').exists()


def test_retirement_preserves_every_table_then_removes_database(tmp_path, monkeypatch):
    import pyarrow.parquet as pq

    from agent_platform.financial_parquet import retire_legacy_db
    monkeypatch.setattr(fc, 'fetch', lambda client, url, hosts, limit: (HTML.encode(), url))
    old=tmp_path/'old'; db=fc.initialize(old)
    fc.collect(None, db, old, 'INFY', 0)
    db.execute('INSERT INTO documents VALUES(?,?,?,?)', ('digest','https://example.com/a.pdf','2026-10-13',123))
    db.commit()
    expected={t:db.execute(f'SELECT * FROM {t} ORDER BY 1').fetchall() for t in ['snapshots','documents','attempts']}
    db.close(); source=old/'catalogue.sqlite3'
    result=retire_legacy_db(source, tmp_path/'archive')
    assert not source.exists() and result['database_removed']
    from pathlib import Path
    for table, original in expected.items():
        output=pq.ParquetFile(Path(result['archive'])/(table+'.parquet')).read()
        assert [tuple(r[k] for k in output.schema.names) for r in output.to_pylist()]==original


def test_retirement_rejects_unknown_tables_without_deleting(tmp_path):
    from agent_platform.financial_parquet import retire_legacy_db
    db=fc.initialize(tmp_path)
    db.execute('CREATE TABLE other_data(value TEXT)');db.commit();db.close()
    source=tmp_path/'catalogue.sqlite3'
    with pytest.raises(ValueError,match='Unexpected database tables'):
        retire_legacy_db(source,tmp_path/'archive')
    assert source.exists()
