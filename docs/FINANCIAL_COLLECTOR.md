# Authorized Indian-company financial collector

This is the first **operator-only collection stage**, supporting a 10-company pilot, Nifty 100, Nifty 500 and NSE main-board/SME securities. It collects
Screener public financial tables and discovers original PDF filing links. The owner confirmed
permission to collect Screener website data in this task. Keep the written permission and its
scope outside Git; recording a permission note does not independently establish redistribution rights.
No Screener login, private endpoint, AI chat extraction, or access-control bypass is used.

## Run locally

From the AI-Agent-Platform repository, install the optional archive dependency with
`.venv/bin/python -m pip install -e ".[financial]"`. New CLI runs default to Parquet;
`--storage sqlite` explicitly opts into the legacy database format.

Run:

```sh
PYTHONPATH=src .venv/bin/python -m agent_platform.financial_collector \
  --permission-note 'Owner-authorized Screener website collection; private pilot'
```

Defaults: INFY, TCS, RELIANCE, HDFCBANK, ICICIBANK, SBIN, ITC, LT, HCLTECH, WIPRO.
Override with `--companies INFY TCS`. `--pdfs-per-company 0` checks tables without downloading
documents; the default is 1 and maximum 3. The PDF selection follows document-link order on
the source page, **not a claim that it is the newest quarterly result**. The pilot fetched
annual-report links. Remaining document links are recorded but not downloaded.

### All Nifty 100 constituents

```sh
PYTHONPATH=src .venv/bin/python -m agent_platform.financial_collector \
  --universe nifty100 \
  --permission-note 'Owner-authorized Screener website collection'
```

The official NSE archive CSV supplies membership, company names, ISINs and industry. Collection
requires exactly 100 unique validated symbols and ISINs; the original CSV and its hash are saved.
The download time establishes when the list was observed, not an independently verified index
effective date. No hard-coded guess at current membership is used. If the official list is
unavailable, provide an already downloaded official CSV with `--constituents-file PATH`; retain
its source URL in a sibling file ending `.source.txt`. Do not silently substitute an old list.
Add `--companies SYMBOL ...` to retry a reviewed subset of that same universe. Progress is saved
after each company in the run report. Partial PDF coverage is explicitly reported and exits 2.

Unlisted company document hosts stay blocked by the allowlist until reviewed; exchange PDF hosts
are supported for all constituents. A consolidated-page 404, or a valid company page with an
empty consolidated quarterly table, permits a standalone-page attempt. Missing accounting-basis
labels remain unknown rather than guessed. Login/challenge pages and other HTTP errors do not
trigger that fallback.
The default 100-company run may download several gigabytes and takes several minutes. It uses
the existing local catalogue and retains earlier pilot snapshots.

VAML had no PDF links in Screener during the initial Nifty 100 run. A reviewed supplement
uses its official investor-relations page's consolidated results for June 2026. The precise CDN
host is allowlisted only for that symbol; discovery page, label and review date are retained.
This is a dated document supplement, not automatic discovery of future releases.

Run again to collect future table changes. This CLI does not install a recurring schedule.
Use one process per output directory. A protected operator scheduler can invoke it later.
No model/API inference charges are incurred by this collector; normal hosting/network costs apply.

### Nifty 500

```sh
PYTHONPATH=src .venv/bin/python -m agent_platform.financial_collector \
  --universe nifty500 --workers 3 --reuse-documents \
  --permission-note 'Owner-authorized Screener website collection'
```

The corresponding official NSE archive CSV must contain 500 unique valid company identities.
Explicitly labelled `Dummy ...` rows with `DUMMY` symbols and `DUM` identifiers are excluded and
recorded in `excluded_placeholders`, while the original CSV remains intact. Unexpected malformed
rows fail validation. On 13 October 2026, the downloaded file included 500 companies plus DUMMYHEG.

Up to three bounded workers are supported (default one), with independent storage handles,
per-company delays and atomic object writes. Only one CLI process should use a root at a time.
`--reuse-documents` verifies cached PDF hashes and records original fetch timestamps; it does
not imply the original URL has been checked again for revised PDFs. Omit it to re-fetch files.
New file writes preserve a 2 GB free-disk reserve. A source or disk failure remains visible in
the run report; it never counts as complete coverage.

Reviewed issuer-document hosts are scoped per symbol. Bagmane's June 2026 consolidated
results supplement is selected ahead of its credit-rating links; VAML, VISL, VOGL and VEDPOWER
have dated issuer supplements. CANHLIFE uses its April 2026 NSE results filing after its issuer
PDF returned 403. All discovered links remain recorded. These are not automatic latest-filing feeds.

## Local storage

Default root: `.runtime/financial-research-pilot/` (ignored by Git).

- `catalogue.sqlite3` (legacy SQLite mode only): financial snapshots (structured JSON), source URLs, timestamps,
  content hashes, PDF metadata and all collection attempts. Financial rows include section,
  raw metric label, period, available period-end date, raw value, decimal value, unit and basis.
- `objects/<sha256>.html`: original company page at collection time.
- `objects/<sha256>.pdf`: original downloaded filing, deduplicated by bytes.
- `run-<timestamp>.json`: immutable per-run outcome report.
- `latest-run.json`: most recent run outcome, including partial failures.
- `<symbol>-knowledge.jsonl` (legacy mode only): latest collected tables in the knowledge API upload shape.

Unchanged financial content reuses a snapshot; changes create a new snapshot and retain history.
Collection timestamps are **not** filing publication dates. Document labels and source links are
retained; the collector does not invent publication dates. It re-fetches bounded selected PDFs
when requested so same-URL replacements can be detected. No raw documents are committed to Git.

## Scope and correctness

Five supported sections: quarterly results, profit/loss, balance sheet, cash flow, and ratios.
Unavailable/masked values remain null with raw text preserved; they never become zero.
Rupee-per-share EPS, percentages and days are separate from INR-crore amounts. Unknown units
remain null. Metric labels are preserved, including different banking statement conventions.
Malformed row widths are excluded from numeric facts but remain in the raw table for inspection.
Source-reported numbers carry `source_reported_unverified`: PDF reconciliation, accounting
normalization, corporate-action adjustments, and financial calculations are subsequent work.

No request to a page is treated as success without a company heading and numeric financial
statement data. Newly listed companies can have annual statements before quarterly results;
these are retained with `missing_data_sections` identifying the empty sections. A ratios-only
or empty page is not accepted. Reviewed symbol aliases retain the original NSE symbol and
ISIN, the source company code and supporting evidence, and check the source company name.
Requests use bounded
workers with delays, bounded size/time and limited 5xx retries. HTTP 403/429 stop that request and are
recorded rather than evaded. HTTPS host allowlists and public-DNS checks apply at each redirect;
this is a trusted operator command, not an untrusted arbitrary-URL fetching API.
PDFs must start with the PDF signature; extraction/OCR and full PDF validation are separate.

Exit 0 means requested collection attempts succeeded; exit 2 means at least one page or selected
PDF failed. Earlier successful snapshots are retained. Inspect the JSON report before using results.

## Consuming apps and RAG

This staging archive is **not automatically connected to MyAlgoDesk, DealAgent or other tenants**.
The JSONL records conform to `POST /v1/knowledge/documents`; an authorized backend can submit
them using its own service identity (`knowledge:write`) and `X-External-User-ID`. Default visibility
is private. App-wide publication additionally requires `knowledge:publish` and explicit visibility
`app`. Source labels include the original section URL. Content-versioned external IDs preserve
old versions; production retrieval must select the appropriate document IDs/period to avoid mixing
historical snapshots. Do not upload every snapshot into an unfiltered "latest results" search.

PDF text/page extraction, app upload automation, a dashboard, scheduled discovery of new PDFs,
and verification against original filings are not included in this first collector stage.
See [KNOWLEDGE_RAG.md](KNOWLEDGE_RAG.md) for the existing app-isolated RAG API.

## Tests

```sh
.venv/bin/python -m pytest tests/python/test_financial_collector.py -q
```

Tests cover dates/units, unavailable data, malformed rows, source denial, redirect restrictions,
private DNS, PDF/size bounds, repeat collection and revised financial snapshots.

## All NSE main-board and SME companies

`--universe nse` loads NSE's official `EQUITY_L.csv` and `SME_EQUITY_L.csv` from
its archive. It validates both CSV schemas, series, symbols and ISINs, keeps segment/listing
metadata, and rejects conflicting identities. Exact duplicate identities across segments are
merged. This covers securities on those available-for-trading lists, not a claim to include
suspended/delisted companies, BSE-only companies, ETFs, debt or every historical NSE listing.
The observed 13 October 2026 lists contain 2,606 main-board plus 580 SME securities: 3,185 companies and one rights entitlement (NATCO-RE). The entitlement references NATCOPHARM financials with explicit issuer identity and is not counted as a separate company.

For a larger run, use an operator-selected storage directory with sufficient space:

```sh
PYTHONPATH=src .venv/bin/python -m agent_platform.financial_collector \
  --universe nse --root '/Volumes/YourDrive/financial-data' \
  --workers 3 --reuse-documents \
  --permission-note 'Owner-authorized public website collection'
```

Replace the example storage path with a real mounted volume. Existing data is not automatically
moved. A new root starts a separate catalogue. Use `--pdfs-per-company 0` for tables and document
links only. A PDF archive for thousands of companies can require tens of GB; the 500-company
sample occupied 7.36 GB of PDF bytes. Not all companies have readable pages or original PDFs;
missing data, new listings, denials and unreviewed issuer hosts remain explicit gaps.

To use saved official lists, pass `--constituents-file MAIN_CSV` and
`--sme-constituents-file SME_CSV` with their source sidecars. `--manifest-only` validates and saves
the company manifest without collecting pages; it does not mean company data has been fetched.
Use `--companies SYMBOL ...` for a bounded batch or reviewed retry. Each run records progress
and preserves earlier snapshots. The collector does not bypass denied requests.

## Parquet-first storage and on-demand database cache

New collection writes `parquet/<symbol>/<snapshot-hash>/facts.parquet` with explicit
column types and Zstandard compression. Raw values, exact decimal strings, nulls, units,
periods and accounting basis survive round trips. `snapshot.parquet` stores provenance and
the full original parsed tables/document links. Publication uses an atomic directory rename
only after both files pass validation. Incomplete staging directories are ignored.

Company files can be read directly without any database. The CLI does **not** create or
write SQLite in default Parquet mode. Audit events and PDF-cache metadata are small JSON
files in `audit/` and `document-index/`. Original HTML and PDF objects stay separate;
Parquet compression does not shrink PDF files. Legacy JSONL exports are retained, not updated by subsequent Parquet runs. The local collector database was explicitly retired after lossless Parquet verification; application databases were not changed.

Convert an existing collector catalogue without deleting or changing it:

```sh
PYTHONPATH=src .venv/bin/python -m agent_platform.financial_parquet migrate \
  --legacy-db .runtime/financial-research-pilot/catalogue.sqlite3 \
  --root .runtime/financial-research-pilot
```

Migration preserves all snapshot versions and revalidates their content hashes and facts.
It does not relocate raw PDF/HTML files; use the same archive root or copy the objects
separately before relocating the archive. No app database is populated by this command.

Load only requested companies into a local, disposable database cache:

```sh
PYTHONPATH=src .venv/bin/python -m agent_platform.financial_parquet load \
  --root .runtime/financial-research-pilot \
  --database .runtime/financial-cache.sqlite3 --symbols INFY TCS
```

This validates the latest archived snapshot of every requested symbol before writing, then
upserts only those companies into `financial_cache` in one transaction. Repeated requests
do not duplicate rows. A request accepts at most 100 symbols. Previously cached companies
remain until explicitly removed; there is no automatic eviction/TTL yet. A new snapshot
is picked up on the next explicit load. Latest means latest **archived snapshot**, not a
guarantee of current market data. Never mix these source-reported figures with verified
financial calculations without reconciliation.

`ParquetStore.latest(symbol)` and `hydrate(root, destination, symbols)` are the Python
entry points. These are operator/backend utilities, not public tenant endpoints. Automatic
request-triggered loading in consuming apps or their production PostgreSQL database is not
wired by this change. Existing authentication/tenant-scoped knowledge publication remains
a separate explicit action.

### Source pacing and interrupted collection

All CLI workers share `--request-interval` (default 5 seconds) between Screener requests,
including standalone fallbacks. On a company-page 403/429, connection failure/timeout, or disk-reserve error, the bounded
queue stops scheduling new companies, finishes the already active requests, and saves
`pending_symbols` with `status: paused_source_or_storage`. Exit code 2 remains a partial run.
Do not repeatedly rerun a denied source. After availability recovers, use the saved manifest
and request only pending/failed symbols; successful immutable Parquet snapshots remain usable.
A PDF failure at an individual issuer is reported independently and does not block unrelated
company-page collection. `--request-interval` applies to a single CLI process: run only one
collector against this source at a time.

### Retire the legacy collector database

Only for an idle collector database whose contents should be removed after export:

```sh
PYTHONPATH=src .venv/bin/python -m agent_platform.financial_parquet retire \
  --legacy-db .runtime/financial-research-pilot/catalogue.sqlite3 \
  --root .runtime/financial-research-pilot
```

Stop legacy writers and checkpoint/close SQLite first. The command refuses WAL/journal
sidecars, unknown tables or schemas, changed files, and round-trip mismatches. It migrates
all snapshots, exports every snapshot/document/attempt record into
`legacy-records/<database-sha256>/*.parquet`, verifies exact equality, then removes the source
database. This is an explicit maintenance operation, not a concurrent writer migration.
Use only the collector catalogue, never an application's transactional database.

### Reviewed recovery sources

BELDING, AGOL and RKFAL use reviewed Screener codes, retaining the NSE symbol, ISIN
and supporting listing evidence. TRIVENIPT was recovered from original issuer filings
when neither its NSE nor BSE Screener page existed. Its six statement tables were visually
checked, with PDF page references and arithmetic checks. Raw text from all 45 scanned pages
is archived separately in `document-pages/*.parquet` as **unverified OCR**. It is not
promoted to numerical facts. The FY26 pre-demerger statements and Q1 restated comparatives
retain separate period labels. This original-filing recovery is a dated operator import;
it does not establish an automatic filing parser for future scans.

### Resume only missing company snapshots

Add `--only-missing --storage parquet` to the collector command. It verifies existing
snapshot contents, skips those companies, and retries missing identities. Corrupt Parquet
raises an error instead of silently being accepted or overwritten. Rights entitlements
use their verified issuer snapshot for this check. This option does not refresh stale
existing snapshots: omit it when intentionally collecting newer financial periods.
A source outage stops the batch and leaves unattempted companies in `pending_symbols`.
