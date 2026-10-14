# NSE financial archive — collection and recovery

Status: **all 3,185 companies in the saved NSE universe have collected financial snapshots**.

Verified on 14 October 2026. One additional rights entitlement retains its issuer reference.

- 0 missing company snapshots and 0 corrupt NSE snapshots.
- 1,402,529 source cells; 1,338,891 numeric observations; 48,697 filing links.
- Today's resume recovered all 121 previously missing companies with one worker and five-second source pacing.
- Existing snapshots were verified and skipped; prior versions and original source objects remain preserved.
- The resume collected available financial tables and document links; it did not download additional PDFs.

The 13 October run paused on source connection failures. That historical failure is resolved
for this saved membership universe. Completion means available statements are archived, not that
every historical filing or PDF was downloaded or that every statement cell is available.

## Scope

The downloaded official NSE available-for-trading files cover 2,606 main-board and 580 SME
securities. These represent **3,185 companies and one rights entitlement**. Suspended/delisted,
BSE-only, ETF and debt securities outside these files are not part of this run.

The archive contains available source financial statements, raw cells, exact decimal strings,
units, periods, basis, source URLs, collection timestamps and filing links. Missing values remain
null. Missing sections remain explicit. A successful collection means available statements were
saved, not that every historical period exists or that the figures were independently audited.

## Recovery work

- **AGOL:** reviewed BSE company code 542579 on Screener, matched to NSE ISIN.
- **BELDING:** reviewed BSE company code 513307, matched to NSE ISIN; collected successfully on 14 October 2026.
- **RKFAL:** reviewed Screener internal company code id/1286233, matched to issuer identity; collected successfully on 14 October 2026.
- **ARMEE:** annual-only financial statements accepted; missing quarterly data remains flagged.
- **TRIVENIPT:** original issuer Q1 FY27 and FY26 consolidated PDFs. Six core statement tables,
  308 cells, 286 numeric observations, 44 arithmetic checks. All 45 scanned pages have separately
  archived OCR, marked unverified. Every structured fact links to its original PDF and page.
  FY26 pre-demerger and Q1 restated comparatives have separate period labels.
- **NATCO-RE:** NSE rights entitlement linked to NATCOPHARM using the original NSE circular.
  It is not assigned invented standalone financial statements or counted as another company.

Historical failed attempts remain in the audit history. Current coverage is determined from
verified snapshots and the final coverage manifest, not from the existence of old failure logs.
Source-denial responses are not bypassed; workers pause new requests on 403/429.

## Parquet and database retirement

Primary archive: `.runtime/financial-research-pilot/parquet/<symbol>/<snapshot>/`.
Each snapshot contains `facts.parquet` and `snapshot.parquet`. The latter retains the complete
source table payload, document links and provenance. Raw HTML and original PDFs remain in
`objects/`, addressed by their SHA-256 hashes. Triveni OCR is in `document-pages/*.parquet`.

The old collector database was removed **after lossless export and verification**:

- 508 snapshots
- 500 document records
- 1,256 attempt records

These records are preserved in three Parquet files under
`legacy-records/3851467552561abd2bde18dd35dd951bafa286e800af7837405246a5e891c39d/`.
Application databases were not changed. Future collector writes default to Parquet. Loading
selected companies into a disposable database is an explicit optional operation.

## Verification artifacts

Inside the private runtime archive:

- `nse-coverage.parquet`: one row per NSE security, source, latest snapshot and missing sections.
- `nse-coverage-summary.json`: final coverage totals and unresolved/corrupt identities.
- `archive-integrity.json`: checksum counts for all raw objects and verified snapshot count.
- `database-retirement.json`: exact source checksum, exported row counts and removal result.
- `trivenipt-recovery.json`: original filing recovery outcome.
- `trivenipt-review/arithmetic-checks.json`: source transcription arithmetic checks.

No automatic RAG publication, financial recommendation, tenant database ingestion or recurring
collection schedule is enabled by this collection. Most discovered PDF links are not downloaded;
502 original PDFs are retained (500 earlier documents plus two Triveni filings).

## Latest coverage verification

Checked: 2026-10-14T03:48:20.660806+00:00

- All 3,186 NSE security rows verified: 3,185 company snapshots plus one issuer reference.
- No missing or corrupt NSE snapshots.
- All 3,195 immutable snapshot versions passed round-trip verification.
- Checksums passed for 3,321 HTML pages, 502 PDFs and four membership CSVs; zero archive errors.
- Pending identities: none in the current coverage manifest.
- 35 focused collector/storage tests and repository-wide Ruff passed.
- GitHub CI installs the optional financial dependency for both test configurations.

The complete archive remains in the ignored local runtime directory. Git tracks the collector,
storage utilities, tests, instructions and coverage reports; raw source data is not committed.
