# Parquet storage verification — historical checkpoint

Checked: 2026-10-13T14:53:36.702333+00:00

Superseded by [the NSE collection/recovery report](financial-collector-nse.md). The later run
retired the SQLite collector database after exporting all snapshots, documents and attempt
records to Parquet. Counts below describe the earlier checkpoint, not current coverage.

## Storage change verified

- Parquet is the collector CLI default; SQLite is an explicit legacy opt-in.
- 508 existing snapshots representing 502 company identities migrated and compared with every original SQLite record.
- Exact values, nulls, original table payloads and provenance passed round-trip verification.
- Initial converted Parquet bytes: 9,174,371. PDFs and HTML remain separate.
- Selective database loading of INFY and TCS passed; loading INFY again created no duplicate. The temporary verification cache was removed.
- 26 focused tests, Ruff and diff whitespace checks passed.
- Legacy SQLite remained at 508 snapshots while new company snapshots were added only to Parquet.

## Collection status at this checkpoint

- NSE available-for-trading universe: 3,186 (2,606 main-board, 580 SME).
- Successfully archived in this universe: 626.
- Known unavailable source page: AGOL (HTTP 404).
- Remaining/unresolved: 2559.
- Resumed worker was running at this checkpoint, with one Screener request every five seconds shared by two workers.
- Earlier 429 responses are retained; the updated bounded queue stops scheduling on company-source denial or low disk space.
- Progress: `.runtime/financial-research-pilot/nse-parquet-resume.jsonl`; batch plan: `nse-resume-plan.json`.

This is a point-in-time report, not a claim that all NSE companies are complete. This expanded run collects financial tables and PDF links; additional PDFs are not downloaded by this run. Existing 500 original PDFs remain intact. App RAG publication, PostgreSQL tenant integration and automatic database cache expiry are not included.
