# Financial collection pilot — live verification

Collected: 2026-10-13T13:54:55.684527+00:00

All ten authorized Screener company pages collected successfully. All ten selected original PDFs downloaded from BSE and passed pdfinfo structural inspection. PDF pages have not been reconciled against extracted financial figures.

| Company | Numeric table cells | PDF links discovered | Downloaded PDF pages |
|---|---:|---:|---:|
| INFY | 537 | 19 | 383 |
| TCS | 547 | 46 | 361 |
| RELIANCE | 561 | 16 | 187 |
| HDFCBANK | 513 | 21 | 678 |
| ICICIBANK | 513 | 53 | 510 |
| SBIN | 517 | 20 | 756 |
| ITC | 561 | 16 | 416 |
| LT | 561 | 49 | 756 |
| HCLTECH | 547 | 45 | 485 |
| WIPRO | 541 | 39 | 561 |

Totals: 50 tables; 5,398 numeric cells; 316 unavailable/non-numeric cells retained; 324 PDF links discovered; 10 PDFs downloaded (166,883,941 bytes).

Selected links are labelled Annual Report 2026 by Screener. This is a bounded initial collection, not complete historical or latest-quarter coverage.

Private data: `.runtime/financial-research-pilot/catalogue.sqlite3`, `objects/`, and company knowledge JSONL exports. No data was published to an app or sent to an LLM. No paid model calls. Scheduled collection and PDF text extraction remain separate stages.

Validation: 12 focused tests passed; Ruff passed. A second live fetch of all ten company pages returned unchanged; the catalogue retained exactly 10 financial snapshots and 10 PDF objects.
