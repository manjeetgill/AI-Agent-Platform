"""Private Parquet archive; explicit, bounded hydration into a local SQLite cache."""
import argparse
import hashlib
import json
import re
import shutil
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path
from uuid import uuid4

import pyarrow as pa
import pyarrow.parquet as pq

FACT_FIELDS = ['section', 'metric', 'period', 'period_end', 'raw', 'value', 'unit', 'basis', 'validation']
FACT_SCHEMA = pa.schema([(k, pa.string()) for k in FACT_FIELDS])
META_SCHEMA = pa.schema([(k, pa.string()) for k in ['id', 'symbol', 'collected_at', 'source_url', 'html_hash', 'payload']])


class ParquetStore:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        for name in ['objects', 'parquet', 'document-index', 'audit']:
            (self.root / name).mkdir(exist_ok=True)

    def close(self):
        pass

    def folder(self, symbol):
        if not re.fullmatch(r'[A-Z0-9][A-Z0-9&.-]{0,29}', symbol):
            raise ValueError('Invalid company symbol')
        return self.root / 'parquet' / symbol

    def save_snapshot(self, snapshot, symbol, stamp, source, html_hash, payload):
        if hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest() != snapshot:
            raise ValueError('Snapshot hash mismatch')
        parent = self.folder(symbol)
        parent.mkdir(exist_ok=True)
        target = parent / snapshot
        if target.exists():
            self.read_snapshot(target)  # Never treat corrupted cached files as success.
            return False
        if shutil.disk_usage(self.root).free < 2_000_000_000 + len(json.dumps(payload).encode()) * 3:
            raise OSError('Disk reserve reached: keeping 2 GB free')
        temp = Path(tempfile.mkdtemp(prefix='.staging-', dir=parent))
        try:
            row = dict(zip(META_SCHEMA.names, [snapshot, symbol, stamp, source, html_hash, json.dumps(payload)]))
            pq.write_table(pa.Table.from_pylist([row], schema=META_SCHEMA), temp / 'snapshot.parquet', compression='zstd')
            # Decimal strings preserve exact source precision; null stays null.
            pq.write_table(pa.Table.from_pylist(payload['facts'], schema=FACT_SCHEMA), temp / 'facts.parquet', compression='zstd')
            self.read_snapshot(temp)
            temp.rename(target)
        finally:
            if temp.exists():
                shutil.rmtree(temp)
        return True

    def read_snapshot(self, folder):
        row = pq.ParquetFile(folder / 'snapshot.parquet').read().to_pylist()[0]
        payload = json.loads(row['payload'])
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        facts = pq.ParquetFile(folder / 'facts.parquet').read().to_pylist()
        if digest != row['id'] or facts != payload['facts'] or payload['symbol'] != row['symbol']:
            raise ValueError('Parquet snapshot integrity mismatch')
        return row

    def latest(self, symbol):
        rows = [self.read_snapshot(p) for p in self.folder(symbol).glob('*') if p.is_dir() and not p.name.startswith('.')]
        if not rows:
            raise ValueError(f'No archived snapshot for {symbol}')
        return max(rows, key=lambda row: (row['collected_at'], row['id']))

    def cache_document(self, url, digest, size, stamp):
        key = hashlib.sha256(url.encode()).hexdigest()
        self._json(self.root / 'document-index' / (key + '.json'), [digest, size, stamp])

    def cached_document(self, url):
        path = self.root / 'document-index' / (hashlib.sha256(url.encode()).hexdigest() + '.json')
        return json.loads(path.read_text()) if path.exists() else None

    def record(self, symbol, url, status, detail, stamp):
        self._json(self.root / 'audit' / (uuid4().hex + '.json'),
                   {'symbol': symbol, 'url': url, 'status': status, 'detail': detail, 'checked_at': stamp})

    @staticmethod
    def _json(path, value):
        temporary = path.with_name('.' + uuid4().hex + '.tmp')
        try:
            temporary.write_text(json.dumps(value))
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


def migrate(legacy_db, root, *, immutable=False):
    store = ParquetStore(root)
    # Read-only connection, never modifies or removes the original database.
    uri = Path(legacy_db).resolve().as_uri() + '?mode=ro' + ('&immutable=1' if immutable else '')
    with closing(sqlite3.connect(uri, uri=True)) as db:
        count = 0
        for row in db.execute('SELECT id,symbol,collected_at,source_url,html_hash,payload FROM snapshots'):
            store.save_snapshot(*row[:5], json.loads(row[5]))
            count += 1
        for digest, url, stamp, size in db.execute('SELECT hash,source_url,fetched_at,bytes FROM documents'):
            store.cache_document(url, digest, size, stamp)
    return count


def hydrate(root, destination, symbols):
    if not symbols or len(symbols) > 100:
        raise ValueError('Select between 1 and 100 companies per on-demand load')
    store = ParquetStore(root)
    rows = [store.latest(s) for s in dict.fromkeys(symbols)]  # Validate before touching destination.
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(destination) as db:
        db.execute('CREATE TABLE IF NOT EXISTS financial_cache (symbol TEXT PRIMARY KEY, snapshot_id TEXT NOT NULL, collected_at TEXT NOT NULL, source_url TEXT NOT NULL, payload TEXT NOT NULL)')
        for r in rows:
            db.execute('INSERT INTO financial_cache VALUES(?,?,?,?,?) ON CONFLICT(symbol) DO UPDATE SET snapshot_id=excluded.snapshot_id,collected_at=excluded.collected_at,source_url=excluded.source_url,payload=excluded.payload',
                       (r['symbol'], r['id'], r['collected_at'], r['source_url'], r['payload']))
    return len(rows)


def retire_legacy_db(legacy_db, root):
    """Explicitly remove an idle collector DB only after lossless Parquet export."""
    path = Path(legacy_db).resolve()
    if any(Path(str(path) + suffix).exists() for suffix in ('-wal', '-shm', '-journal')):
        raise ValueError('Stop database writers and checkpoint SQLite before retirement')
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    migrate(path, root, immutable=True)
    destination = Path(root) / 'legacy-records' / before
    destination.parent.mkdir(parents=True, exist_ok=True)
    schemas = {
        'snapshots': META_SCHEMA,
        'documents': pa.schema([('hash', pa.string()), ('source_url', pa.string()),
                                ('fetched_at', pa.string()), ('bytes', pa.int64())]),
        'attempts': pa.schema([('id', pa.int64()), ('symbol', pa.string()), ('url', pa.string()),
                               ('checked_at', pa.string()), ('status', pa.string()), ('detail', pa.string())]),
    }
    temporary = Path(tempfile.mkdtemp(prefix='.retire-', dir=destination.parent))
    counts = {}
    try:
        with closing(sqlite3.connect(path.as_uri() + '?mode=ro&immutable=1', uri=True)) as db:
            db.execute('BEGIN')
            actual = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if actual != set(schemas):
                raise ValueError('Unexpected database tables; refusing retirement')
            for table, schema in schemas.items():
                columns = [r[1] for r in db.execute(f'PRAGMA table_info("{table}")')]
                if columns != schema.names:
                    raise ValueError('Unexpected database schema; refusing retirement')
                original = db.execute(f'SELECT * FROM "{table}" ORDER BY 1').fetchall()
                output = temporary / (table + '.parquet')
                pq.write_table(pa.Table.from_pylist([dict(zip(columns, r)) for r in original], schema=schema),
                               output, compression='zstd')
                restored = pq.ParquetFile(output).read().to_pylist()
                if [tuple(r[c] for c in columns) for r in restored] != original:
                    raise ValueError('Legacy Parquet round-trip mismatch')
                counts[table] = len(original)
        if destination.exists():
            for table in schemas:
                if (destination / (table + '.parquet')).read_bytes() != (temporary / (table + '.parquet')).read_bytes():
                    raise ValueError('Existing retirement archive differs')
        else:
            temporary.rename(destination)
        if hashlib.sha256(path.read_bytes()).hexdigest() != before or any(
                Path(str(path) + suffix).exists() for suffix in ('-wal', '-shm', '-journal')):
            raise ValueError('Database changed during export; database was retained')
        path.unlink()
        return {'archive': str(destination), 'source_sha256': before, 'rows': counts, 'database_removed': True}
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['migrate', 'retire', 'load'])
    parser.add_argument('--root', type=Path, default=Path('.runtime/financial-research-pilot'))
    parser.add_argument('--legacy-db', type=Path)
    parser.add_argument('--database', type=Path)
    parser.add_argument('--symbols', nargs='+')
    args = parser.parse_args()
    if args.action in {'migrate', 'retire'}:
        if not args.legacy_db:
            parser.error('--legacy-db is required')
        result = (retire_legacy_db(args.legacy_db, args.root) if args.action == 'retire'
                  else {'snapshots_archived': migrate(args.legacy_db, args.root)})
        print(json.dumps(result))
    else:
        if not args.database or not args.symbols:
            parser.error('--database and --symbols are required')
        print(json.dumps({'companies_loaded': hydrate(args.root, args.database, args.symbols)}))


if __name__ == '__main__':
    main()
