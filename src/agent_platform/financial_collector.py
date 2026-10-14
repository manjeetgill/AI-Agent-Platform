"""Operator-only, bounded collector for explicitly authorized public company pages.

No model calls, browser automation, login, or automatic publication to consuming apps.
The local SQLite catalogue and immutable files are a staging area, not financial advice.
"""

import argparse
import csv
import hashlib
import io
import ipaddress
import json
import re
import shutil
import socket
import sqlite3
import tempfile
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import quote, unquote, urldefrag, urljoin, urlsplit

import httpx

COMPANIES = {
    "INFY": ("Infosys", "www.infosys.com"),
    "TCS": ("Tata Consultancy Services", "www.tcs.com"),
    "RELIANCE": ("Reliance Industries", "www.ril.com"),
    "HDFCBANK": ("HDFC Bank", "www.hdfcbank.com", "www.hdfc.bank.in"),
    "ICICIBANK": ("ICICI Bank", "www.icicibank.com", "www.icici.bank.in"),
    "SBIN": ("State Bank of India", "sbi.co.in", "www.sbi.co.in", "bank.sbi"),
    "ITC": ("ITC", "www.itcportal.com", "itcportal.com"),
    "LT": ("Larsen & Toubro", "www.larsentoubro.com", "investors.larsentoubro.com"),
    "HCLTECH": ("HCL Technologies", "www.hcltech.com"),
    "WIPRO": ("Wipro", "www.wipro.com"),
}
NIFTY100_URL = "https://archives.nseindia.com/content/indices/ind_nifty100list.csv"
UNIVERSES = {"nifty100": (100, NIFTY100_URL), "nifty500": (500, "https://archives.nseindia.com/content/indices/ind_nifty500list.csv")}
NSE_LISTS = {
    "main_board": "https://archives.nseindia.com/content/equities/EQUITY_L.csv",
    "sme": "https://archives.nseindia.com/emerge/corporates/content/SME_EQUITY_L.csv",
}
SOURCE_ALIASES = {
    "RKFAL": {"code": "id/1286233", "isin": "INE2O2Y01016", "name": "R.K. Fashion Accessories",
              "evidence": "https://nsearchives.nseindia.com/emerge/corporates/content/Registration_27052026221452_DRHP.pdf"},
    "BELDING": {"code": "513307", "isin": "INE363L01045", "name": "Belding India",
                "evidence": "https://www.belding.ltd/wp-content/uploads/2026/10/Intimation-of-NSE-Listing_Belding-India-Limited_Signed.pdf"},
    "AGOL": {"code": "542579", "isin": "INE05FR01029", "name": "Ashapuri Gold Ornament",
             "evidence": "https://www.bseindia.com/xml-data/corpfiling/AttachHis/130f0e0d-9743-48cf-b6d3-13314866dac5.pdf"},
}
SECURITY_LINKS = {
    "NATCO-RE": {"isin": "INE987B20018", "issuer_symbol": "NATCOPHARM",
                 "issuer_isin": "INE987B01026", "security_type": "rights_entitlement",
                 "evidence": "https://archives.nseindia.com/content/circulars/CML76798.pdf"},
}

# Reviewed original-source supplement when Screener has no document links.
# This is a dated filing, never a promise of "latest" coverage.
DOCUMENT_SUPPLEMENTS = {
    "VISL": [{
        "url": "https://www.vedantaironandsteel.com/wp-content/uploads/2026/07/Consolidated-financials-Q1-FY-2026-27.pdf",
        "label": "Consolidated financial results, quarter ended 2026-06-30",
        "discovered_on": "https://www.vedantaironandsteel.com/investor-relations/results-and-reports/",
        "source_kind": "reviewed_company_investor_relations",
        "reviewed_on": "2026-10-13",
        "status": "discovered"
}],
    "VOGL": [{
        "url": "https://www.vedantaoilandgas.com/wp-content/uploads/2026/07/Conso-VOGL-Q1.pdf",
        "label": "Consolidated financial results, quarter ended 2026-06-30",
        "discovered_on": "https://www.vedantaoilandgas.com/investor-relations/",
        "source_kind": "reviewed_company_investor_relations",
        "reviewed_on": "2026-10-13",
        "status": "discovered"
}],
    "VEDPOWER": [{
        "url": "https://www.vedantapower.com/wp-content/uploads/2026/07/Consolidated-Financials.pdf",
        "label": "Consolidated financial results, quarter ended 2026-06-30",
        "discovered_on": "https://www.vedantapower.com/investor-relations/results-reports/",
        "source_kind": "reviewed_company_investor_relations",
        "reviewed_on": "2026-10-13",
        "status": "discovered"
}],
    "CANHLIFE": [{
        "url": "https://nsearchives.nseindia.com/corporate/CANARAHSBCLIFE_28042026182841_OutcomeReg30NSE.pdf",
        "label": "Board outcome and financial results, 2026-04-28",
        "discovered_on": "https://nsearchives.nseindia.com/corporate/CANARAHSBCLIFE_28042026182841_OutcomeReg30NSE.pdf",
        "source_kind": "reviewed_exchange_filing",
        "reviewed_on": "2026-10-13",
        "status": "discovered"
}],

    "BAGMANE": [{
        "url": "https://www.bagmanereit.com/media/filer_public/46/98/4698fb63-7945-476e-89b9-8c66fad2d6d5/bagmane_prime_office_reit_financial_results.pdf",
        "label": "Consolidated financial results, quarter ended 2026-06-30",
        "discovered_on": "https://www.bagmanereit.com/investors/resources/",
        "source_kind": "reviewed_company_investor_relations",
        "reviewed_on": "2026-10-13", "status": "discovered",
    }],
    "VAML": [{
        "url": "https://d1rbiogke1jwo5.cloudfront.net/wp-content/uploads/2026/07/Consolidated-Financials.pdf",
        "label": "Consolidated financial results, quarter ended 2026-06-30",
        "discovered_on": "https://vedantaaluminium.com/investors/results-and-reports/",
        "source_kind": "reviewed_company_investor_relations",
        "reviewed_on": "2026-10-13",
        "status": "discovered",
    }],
}

DOCUMENT_HOSTS = {
    "ANTHEM": {"anthembio.com"},
    "CANHLIFE": {"www.canarahsbclife.com"},
    "EMBASSY": {"eopwebsvr.blob.core.windows.net"},
    "JYOTICNC": {"jyoti.co.in"},
    "LENSKART": {"static.lenskart.com"},
}
SECTIONS = {"quarters", "profit-loss", "balance-sheet", "cash-flow", "ratios"}
EXCHANGES = {"www.bseindia.com", "archives.bseindia.com", "archives.nseindia.com",
             "nsearchives.nseindia.com", "www.nseindia.com"}
SOURCE_LOCK = threading.Lock()
SOURCE_INTERVAL = 0.0  # Set by the CLI; direct test/library callers choose their own scheduler.
SOURCE_NEXT = 0.0


def source_gate(url):
    global SOURCE_NEXT
    if urlsplit(url).hostname == "www.screener.in" and SOURCE_INTERVAL:
        with SOURCE_LOCK:
            delay = SOURCE_NEXT - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            SOURCE_NEXT = time.monotonic() + SOURCE_INTERVAL


def bounded_results(executor, worker, symbols, workers):
    """Do not enqueue thousands of requests after an upstream denial."""
    remaining = iter(symbols)
    pending = {executor.submit(worker, s) for s in [next(remaining, None) for _ in range(workers)] if s}
    halted = False
    while pending:
        completed, pending = wait(pending, return_when=FIRST_COMPLETED)
        batch = [f.result() for f in completed]
        halted |= any(r.get("status") == "failed" and r.get("reason", "").startswith(("HTTP 429", "HTTP 403", "Disk reserve", "ConnectError", "ConnectTimeout", "ReadTimeout")) for r in batch)
        yield from batch
        if not halted:
            for _ in completed:
                symbol = next(remaining, None)
                if symbol:
                    pending.add(executor.submit(worker, symbol))


def missing_symbols(store, symbols):
    """Skip verified archived identities; corruption must not be treated as absence."""
    pending, skipped = [], []
    for symbol in symbols:
        issuer = SECURITY_LINKS.get(symbol, {}).get("issuer_symbol", symbol)
        try:
            store.latest(issuer)
        except ValueError as exc:
            if not str(exc).startswith("No archived snapshot for "):
                raise
            pending.append(symbol)
        else:
            skipped.append(symbol)
    return pending, skipped


def parse_constituents(content, expected=100):
    rows = list(csv.DictReader(io.StringIO(content.decode("utf-8-sig"))))
    companies = {}
    isins = set()
    for row in rows:
        symbol = row.get("Symbol", "").strip()
        isin = row.get("ISIN Code", "").strip()
        name = row.get("Company Name", "").strip()
        if name.startswith("Dummy ") and symbol.startswith("DUMMY") and isin.startswith("DUM"):
            continue  # Explicitly labelled index placeholder; retained in raw CSV and run manifest.
        if (not re.fullmatch(r"[A-Z0-9][A-Z0-9&.-]{0,29}", symbol)
                or not re.fullmatch(r"IN[A-Z0-9]{10}", isin) or not name
                or symbol in companies or isin in isins):
            raise ValueError("Invalid/duplicate constituent identity")
        companies[symbol] = {"name": name, "isin": isin, "industry": row.get("Industry", "")}
        isins.add(isin)
    if len(companies) != expected:
        raise ValueError(f"Expected {expected} Nifty constituents, received {len(companies)}")
    return companies


def now():
    return datetime.now(UTC).isoformat()


def parse_nse_list(content, segment):
    """Normalize NSE's two different CSV schemas without guessing index membership."""
    reader = csv.DictReader(io.StringIO(content.decode("utf-8-sig")))
    companies = {}
    isins = set()
    permitted = {"EQ", "BE", "BZ"} if segment == "main_board" else {"SM", "ST", "SZ"}
    for raw in reader:
        row = {k.strip().replace("_", " "): v.strip() for k, v in raw.items() if k and v is not None}
        symbol, isin = row.get("SYMBOL", ""), row.get("ISIN NUMBER", "")
        name, series = row.get("NAME OF COMPANY", ""), row.get("SERIES", "")
        if (not re.fullmatch(r"[A-Z0-9][A-Z0-9&.-]{0,29}", symbol)
                or not re.fullmatch(r"IN[A-Z0-9]{10}", isin) or not name
                or series not in permitted or symbol in companies or isin in isins):
            raise ValueError("Invalid/duplicate NSE security identity or unsupported series")
        companies[symbol] = {"name": name, "isin": isin, "series": series,
                             "segments": [segment], "listing_date": row.get("DATE OF LISTING")}
        if symbol in SECURITY_LINKS:
            if isin != SECURITY_LINKS[symbol]["isin"]:
                raise ValueError("Reviewed security identity changed")
            companies[symbol].update(SECURITY_LINKS[symbol])
        isins.add(isin)
    if not 1 <= len(companies) <= 10000:
        raise ValueError("Empty or unexpectedly large NSE security list")
    return companies


def merge_nse_lists(main_board, sme):
    merged = {s: dict(m) for s, m in main_board.items()}
    identities = {m["isin"]: s for s, m in merged.items()}
    for symbol, member in sme.items():
        if symbol in merged:
            if merged[symbol]["isin"] != member["isin"]:
                raise ValueError("Conflicting NSE symbol identity")
            merged[symbol]["segments"] = ["main_board", "sme"]
        elif member["isin"] in identities:
            raise ValueError("NSE ISIN has conflicting symbols; review migration")
        else:
            merged[symbol] = dict(member)
            identities[member["isin"]] = symbol
    return merged


def clean(value):
    return " ".join(value.split()).replace("\x00", "")


class Page(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.section = ""
        self.sections = {}
        self.links = []
        self.cell = None
        self.row = None
        self.table = None
        self.anchor = None
        self.heading = None
        self.title = ""

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "section":
            self.section = attrs.get("id", "")
            self.sections.setdefault(self.section, {"text": [], "tables": []})
        if tag == "h1":
            self.heading = []
        if tag == "table" and self.section in SECTIONS and "data-table" in attrs.get("class", "").split():
            self.table = []
        if tag == "tr" and self.table is not None:
            self.row = []
        if tag in {"td", "th"} and self.row is not None:
            self.cell = {"text": [], "date": attrs.get("data-date-key")}
        if tag == "a" and self.section == "documents":
            self.anchor = {"url": attrs.get("href", ""), "text": []}

    def handle_data(self, data):
        if self.section in self.sections:
            self.sections[self.section]["text"].append(data)
        if self.cell is not None:
            self.cell["text"].append(data)
        if self.anchor is not None:
            self.anchor["text"].append(data)
        if self.heading is not None:
            self.heading.append(data)

    def handle_endtag(self, tag):
        if tag == "h1" and self.heading is not None:
            self.title = clean(" ".join(self.heading))
            self.heading = None
        if tag in {"td", "th"} and self.cell is not None:
            self.cell["text"] = clean(" ".join(self.cell["text"]))
            self.row.append(self.cell)
            self.cell = None
        if tag == "tr" and self.row is not None:
            self.table.append(self.row)
            self.row = None
        if tag == "table" and self.table is not None:
            self.sections[self.section]["tables"].append(self.table)
            self.table = None
        if tag == "a" and self.anchor is not None:
            self.anchor["text"] = clean(" ".join(self.anchor["text"]))
            self.links.append(self.anchor)
            self.anchor = None
        if tag == "section":
            self.section = ""


def number(raw):
    value = raw.replace(",", "").replace("%", "").replace("−", "-").strip()
    if value.startswith("(") and value.endswith(")"):
        value = "-" + value[1:-1]
    try:
        result = Decimal(value)
        return str(result) if result.is_finite() else None
    except InvalidOperation:
        return None


def parse_page(html, symbol, source_url):
    page = Page()
    page.feed(html)
    tables, facts = [], []
    for section in sorted(SECTIONS):
        block = page.sections.get(section, {})
        context = clean(" ".join(block.get("text", [])))
        basis = "consolidated" if re.search(r"\bConsolidated\b", context) else "unknown"
        if basis == "unknown" and re.search(r"\bStandalone\b", context):
            basis = "standalone"
        for rows in block.get("tables", []):
            if not rows:
                continue
            headers = rows[0]
            table = {"section": section, "basis": basis, "headers": headers, "rows": rows[1:],
                     "display_unit": "INR crore" if "Figures in Rs. Crores" in context else None}
            tables.append(table)
            for row in rows[1:]:
                if len(row) != len(headers) or not row[0]["text"]:
                    continue  # Do not silently shift malformed columns into the wrong period.
                metric = row[0]["text"].rstrip(" +")
                for header, cell in zip(headers[1:], row[1:]):
                    raw = cell["text"]
                    unit = None
                    if "%" in raw or "%" in metric:
                        unit = "percent"
                    elif metric.startswith("EPS"):
                        unit = "INR/share"
                    elif "Days" in metric:
                        unit = "days"
                    elif section != "ratios":
                        unit = table["display_unit"]
                    facts.append({"section": section, "metric": metric, "period": header["text"],
                                  "period_end": header["date"], "raw": raw, "value": number(raw),
                                  "unit": unit, "basis": basis, "validation": "source_reported_unverified"})
    populated = {f["section"] for f in facts if f["value"] is not None}
    if not page.title or not populated.intersection(SECTIONS - {"ratios"}):
        raise ValueError("No readable financial table; login, blocked page or layout change")
    links = []
    seen = set()
    for link in page.links:
        url = urldefrag(urljoin(source_url, link["url"]))[0]
        if urlsplit(url).path.lower().endswith(".pdf") and url not in seen:
            links.append({"url": url, "label": link["text"], "status": "discovered"})
            seen.add(url)
    return {"symbol": symbol, "company": page.title, "source_url": source_url,
            "tables": tables, "facts": facts, "documents": links,
            "missing_data_sections": sorted(SECTIONS - populated)}


def validate_url(url, hosts):
    parts = urlsplit(url)
    if (parts.scheme != "https" or parts.hostname not in hosts or parts.username or parts.password
            or parts.port not in (None, 443)):
        raise ValueError("URL is outside the explicit HTTPS source allowlist")
    addresses = socket.getaddrinfo(parts.hostname, 443, type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
        raise ValueError("Source DNS is not public")


def fetch(client, url, hosts, limit, pdf=False):
    """Explicit redirects, size/deadline bounds; no retries on access denial."""
    deadline = time.monotonic() + 120
    for _ in range(4):
        validate_url(url, hosts)
        for attempt in range(3):
            source_gate(url)
            with client.stream("GET", url) as response:
                if response.status_code in {301, 302, 303, 307, 308}:
                    url = urljoin(url, response.headers["location"])
                    break
                if response.status_code in {500, 502, 503, 504} and attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                response.raise_for_status()
                content = bytearray()
                for chunk in response.iter_bytes():
                    content.extend(chunk)
                    if len(content) > limit or time.monotonic() > deadline:
                        raise ValueError("Source exceeds download size/time bound")
                if pdf and not content.startswith(b"%PDF-"):
                    raise ValueError("Document response is not a PDF")
                return bytes(content), url
        else:
            raise ValueError("Retry budget exhausted")
    raise ValueError("Redirect budget exhausted")


def open_store(root, storage):
    if storage == "parquet":
        from .financial_parquet import ParquetStore
        return ParquetStore(root)
    return initialize(root)


def initialize(root):
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    (root / "objects").mkdir(exist_ok=True)
    db = sqlite3.connect(root / "catalogue.sqlite3")
    db.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS snapshots(
          id TEXT PRIMARY KEY, symbol TEXT NOT NULL, collected_at TEXT NOT NULL,
          source_url TEXT NOT NULL, html_hash TEXT NOT NULL, payload TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS snapshot_company ON snapshots(symbol,collected_at);
        CREATE TABLE IF NOT EXISTS documents(
          hash TEXT PRIMARY KEY, source_url TEXT NOT NULL, fetched_at TEXT NOT NULL, bytes INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS attempts(
          id INTEGER PRIMARY KEY, symbol TEXT NOT NULL, url TEXT NOT NULL,
          checked_at TEXT NOT NULL, status TEXT NOT NULL, detail TEXT NOT NULL);
    """)
    return db


def save_object(root, content, extension):
    digest = hashlib.sha256(content).hexdigest()
    path = root / "objects" / (digest + extension)
    if not path.exists() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
        if shutil.disk_usage(root).free < len(content) + 2_000_000_000:
            raise OSError("Disk reserve reached: keeping 2 GB free")
        with tempfile.NamedTemporaryFile(dir=root / "objects", suffix=".tmp", delete=False) as handle:
            handle.write(content)
            temporary = Path(handle.name)
        temporary.replace(path)
    return digest


def record(db, symbol, url, status, detail=""):
    if hasattr(db, "save_snapshot"):
        db.record(symbol, url, status, detail, now())
        return
    db.execute("INSERT INTO attempts(symbol,url,checked_at,status,detail) VALUES(?,?,?,?,?)",
               (symbol, url, now(), status, detail))
    db.commit()


def collect(client, db, root, symbol, pdf_limit=1, reuse_documents=False):
    alias = SOURCE_ALIASES.get(symbol)
    source_code = alias["code"] if alias else symbol
    encoded_code = quote(source_code, safe='/' if alias else '')
    source = f"https://www.screener.in/company/{encoded_code}/consolidated/"
    try:
        content, resolved = fetch(client, source, {"www.screener.in"}, 4_000_000)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code != 404:
            raise
        source = f"https://www.screener.in/company/{encoded_code}/"
        content, resolved = fetch(client, source, {"www.screener.in"}, 4_000_000)
    # Some standalone-only issuers return a valid 200 company page with an empty
    # consolidated table. Only this specific structure permits the alternate page;
    # a login/challenge page or malformed populated table must still fail closed.
    probe = Page()
    probe.feed(content.decode("utf-8"))
    quarterly = probe.sections.get("quarters", {}).get("tables", [])
    if (resolved.rstrip("/").endswith("/consolidated") and probe.title and quarterly
            and quarterly[0] and len(quarterly[0][0]) == 1):
        source = f"https://www.screener.in/company/{encoded_code}/"
        content, resolved = fetch(client, source, {"www.screener.in"}, 4_000_000)
    # A redirect to a different company is never accepted as this symbol's data.
    if unquote(urlsplit(resolved).path).rstrip("/") not in {f"/company/{source_code}/consolidated", f"/company/{source_code}"}:
        raise ValueError("Company URL identity changed")
    payload = parse_page(content.decode("utf-8"), symbol, resolved)
    if alias:
        if not payload["company"].casefold().startswith(alias["name"].casefold()):
            raise ValueError("Reviewed source alias company name changed")
        payload["source_identity"] = alias
    supplements = DOCUMENT_SUPPLEMENTS.get(symbol, [])
    payload["documents"] = supplements + [d for d in payload["documents"] if d["url"] not in {s["url"] for s in supplements}]
    html_hash = save_object(root, content, ".html")
    # Cosmetic page changes should not create a new financial snapshot.
    snapshot = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    stamp = now()
    if hasattr(db, "save_snapshot"):
        inserted = db.save_snapshot(snapshot, symbol, stamp, resolved, html_hash, payload)
    else:
        inserted = db.execute("INSERT OR IGNORE INTO snapshots VALUES(?,?,?,?,?,?)",
                              (snapshot, symbol, stamp, resolved, html_hash, json.dumps(payload))).rowcount
    record(db, symbol, resolved, "collected" if inserted else "unchanged", snapshot)
    hosts = EXCHANGES | set(COMPANIES.get(symbol, (symbol,))[1:])
    hosts |= DOCUMENT_HOSTS.get(symbol, set())
    hosts |= {urlsplit(d["url"]).hostname for d in DOCUMENT_SUPPLEMENTS.get(symbol, [])}
    documents = []
    # Only a bounded subset is fetched; all discovered links stay in the snapshot.
    for link in payload["documents"][:pdf_limit]:
        url = link["url"]
        try:
            cached = None
            if reuse_documents:
                cached = (db.cached_document(url) if hasattr(db, "save_snapshot") else
                          db.execute("SELECT hash,bytes,fetched_at FROM documents WHERE source_url=? ORDER BY fetched_at DESC LIMIT 1", (url,)).fetchone())
            if cached and (root / "objects" / (cached[0] + ".pdf")).is_file():
                saved = root / "objects" / (cached[0] + ".pdf")
                if hashlib.sha256(saved.read_bytes()).hexdigest() == cached[0]:
                    documents.append({"url": url, "hash": cached[0], "status": "collected", "bytes": cached[1],
                                      "reused": True, "fetched_at": cached[2]})
                    record(db, symbol, url, "document_reused", cached[0])
                    continue
            pdf, final = fetch(client, url, hosts, 80_000_000, pdf=True)
            digest = save_object(root, pdf, ".pdf")
            if hasattr(db, "save_snapshot"):
                db.cache_document(url, digest, len(pdf), now())
            else:
                db.execute("INSERT OR IGNORE INTO documents VALUES(?,?,?,?)", (digest, final, now(), len(pdf)))
            record(db, symbol, url, "document_collected", digest)
            documents.append({"url": url, "hash": digest, "status": "collected", "bytes": len(pdf),
                              "discovered_on": link.get("discovered_on", resolved), "label": link["label"]})
        except (httpx.HTTPError, ValueError, OSError) as exc:
            detail = failure(exc)
            record(db, symbol, url, "document_failed", detail)
            documents.append({"url": url, "status": "failed", "reason": detail})
        time.sleep(1)
    # Export per-table records compatible with the existing authenticated knowledge upload API.
    # This file is staging only: no implicit cross-app publication or credential copying.
    export = []
    for table in payload["tables"]:
        text = json.dumps({"company": payload["company"], "symbol": symbol,
                           "source": resolved, "collected_at": stamp, "financial_table": table}, ensure_ascii=False)
        export.append({"external_id": f"financial:{symbol}:{snapshot[:16]}:{table['section']}",
                       "title": f"{symbol} {table['section']} ({table['basis']})",
                       "text": text, "source_label": resolved + "#" + table["section"], "visibility": "private"})
    if not hasattr(db, "save_snapshot"):
        (root / f"{symbol}-knowledge.jsonl").write_text("\n".join(json.dumps(r) for r in export) + "\n")
    return {"symbol": symbol, "status": "collected" if inserted else "unchanged", "snapshot": snapshot,
            "tables": len(payload["tables"]), "facts": len(payload["facts"]),
            "numeric_facts": sum(f["value"] is not None for f in payload["facts"]),
            "document_links": len(payload["documents"]), "documents": documents,
            "missing_sections": sorted(SECTIONS - {t["section"] for t in payload["tables"]}),
            "missing_data_sections": payload["missing_data_sections"],
            "document_coverage": "not_requested" if not pdf_limit else
                ("complete" if len(documents) == pdf_limit and all(d["status"] == "collected" for d in documents)
                 else "incomplete")}


def failure(exc):
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code}; no bypass attempted"
    if isinstance(exc, httpx.HTTPError):
        return type(exc).__name__
    return str(exc)[:200]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(".runtime/financial-research-pilot"))
    parser.add_argument("--storage", choices=["parquet", "sqlite"], default="parquet",
                        help="Parquet is the default; SQLite is a legacy opt-in")
    parser.add_argument("--companies", nargs="+", help="Subset of the selected universe")
    parser.add_argument("--universe", choices=["pilot", "nifty100", "nifty500", "nse"], default="pilot")
    parser.add_argument("--sme-constituents-file", type=Path, help="Official SME CSV when using --universe nse")
    parser.add_argument("--only-missing", action="store_true", help="Resume Parquet collection without re-fetching verified archived companies")
    parser.add_argument("--manifest-only", action="store_true", help="Validate and save membership without collecting company pages")
    parser.add_argument("--constituents-file", type=Path,
                        help="Previously downloaded official Nifty CSV; provenance must be retained alongside it")
    parser.add_argument("--permission-note", required=True, help="Record the operator's source authorization")
    parser.add_argument("--pdfs-per-company", type=int, choices=range(4), default=1)
    parser.add_argument("--workers", type=int, choices=range(1, 4), default=1)
    parser.add_argument("--request-interval", type=float, default=5.0,
                        help="Minimum seconds between Screener requests across all workers")
    parser.add_argument("--reuse-documents", action="store_true", help="Reuse hash-verified saved PDFs; does not recheck their freshness")
    args = parser.parse_args()
    if not 1 <= args.request_interval <= 60:
        parser.error("--request-interval must be between 1 and 60 seconds")
    if args.only_missing and args.storage != "parquet":
        parser.error("--only-missing requires Parquet storage")
    global SOURCE_INTERVAL
    SOURCE_INTERVAL = args.request_interval
    db = open_store(args.root, args.storage)
    results = []
    report = {"started_at": now(), "authorization": args.permission_note,
              "scope": "operator-only collection", "storage": args.storage, "universe": args.universe, "results": results}
    report_path = args.root / ("run-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f") + ".json")
    with httpx.Client(timeout=30, trust_env=False, follow_redirects=False,
                      headers={"User-Agent": "MyAlgoDeskFinancialResearch/0.1 (authorized pilot)"}) as client:
        universe = dict(COMPANIES)
        if args.universe == "nse":
            lists, sources = {}, {}
            for segment, local in [("main_board", args.constituents_file), ("sme", args.sme_constituents_file)]:
                url = NSE_LISTS[segment]
                if local:
                    data, origin = local.read_bytes(), str(local.resolve())
                else:
                    data, origin = fetch(client, url, {"archives.nseindia.com"}, 2_000_000)
                lists[segment] = parse_nse_list(data, segment)
                sources[segment] = {"source": origin, "official_url": url,
                                    "sha256": save_object(args.root, data, ".csv"),
                                    "count": len(lists[segment]), "loaded_at": now()}
                if local and local.with_suffix(".source.txt").is_file():
                    sources[segment]["download_source"] = local.with_suffix(".source.txt").read_text().strip()
            universe = merge_nse_lists(lists["main_board"], lists["sme"])
            report["constituents"] = {"sources": sources, "members": universe, "loaded_at": now()}
        if args.universe in UNIVERSES:
            expected, index_url = UNIVERSES[args.universe]
            if args.constituents_file:
                data = args.constituents_file.read_bytes()
                origin = str(args.constituents_file.resolve())
                provenance = args.constituents_file.with_suffix(".source.txt")
                if provenance.is_file():
                    report["constituent_download_source"] = provenance.read_text().strip()
            else:
                data, origin = fetch(client, index_url, {"archives.nseindia.com"}, 200_000)
            universe = parse_constituents(data, expected)
            report["excluded_placeholders"] = [
                row for row in csv.DictReader(io.StringIO(data.decode("utf-8-sig")))
                if row["Symbol"].strip() not in universe
            ]
            digest = save_object(args.root, data, ".csv")
            report["constituents"] = {"source": origin, "official_url": index_url,
                                      "sha256": digest, "members": universe, "loaded_at": now()}
        symbols = list(dict.fromkeys(args.companies or universe))
        if not set(symbols) <= universe.keys():
            parser.error("Companies must belong to the selected universe")
        for symbol in set(symbols) & SOURCE_ALIASES.keys():
            if isinstance(universe[symbol], dict) and universe[symbol]["isin"] != SOURCE_ALIASES[symbol]["isin"]:
                parser.error("Reviewed source alias no longer matches the NSE identity")
        if args.manifest_only:
            report["status"] = "membership_only"
            report_path.write_text(json.dumps(report, indent=2))
            print(json.dumps({"status": "membership_only", "companies": len(universe), "manifest": str(report_path)}))
            db.close()
            return
        if args.only_missing:
            symbols, report["skipped_archived_symbols"] = missing_symbols(db, symbols)
        def run_company(symbol):
            if symbol in SECURITY_LINKS:
                return {"symbol": symbol, "status": "issuer_reference", **SECURITY_LINKS[symbol]}
            local_db = open_store(args.root, args.storage)
            try:
                with httpx.Client(timeout=30, trust_env=False, follow_redirects=False,
                                  headers={"User-Agent": "MyAlgoDeskFinancialResearch/0.1 (authorized pilot)"}) as worker:
                    return collect(worker, local_db, args.root, symbol, args.pdfs_per_company, args.reuse_documents)
            except (httpx.HTTPError, ValueError, OSError) as exc:
                result = {"symbol": symbol, "status": "failed", "reason": failure(exc)}
                record(local_db, symbol, "https://www.screener.in/", "failed", result["reason"])
                return result
            finally:
                local_db.close()
                time.sleep(2)

        with ThreadPoolExecutor(max_workers=args.workers) as executor:
            for result in bounded_results(executor, run_company, symbols, args.workers):
                results.append(result)
                print(json.dumps(result), flush=True)
                report["checked_at"] = now()
                temporary = report_path.with_suffix(".tmp")
                temporary.write_text(json.dumps(report, indent=2))
                temporary.replace(report_path)
        attempted = {r["symbol"] for r in results}
        report["pending_symbols"] = [s for s in symbols if s not in attempted]
        report["status"] = "paused_source_or_storage" if report["pending_symbols"] else "finished"
    report_path.write_text(json.dumps(report, indent=2))
    (args.root / "latest-run.json").write_text(json.dumps(report, indent=2))
    db.close()
    if any(r["status"] == "failed" or r.get("document_coverage") == "incomplete" or any(d["status"] == "failed" for d in r.get("documents", []))
           for r in results):
        raise SystemExit(2)


if __name__ == "__main__":
    main()
