import json
import socket

import httpx
import pytest

from agent_platform import financial_collector as fc

HTML = '''<h1>Infosys Ltd</h1><section id="quarters">
<p>Consolidated Figures in Rs. Crores</p><table class="data-table">
<tr><th></th><th data-date-key="2025-03-31">Mar 2025</th><th>TTM</th></tr>
<tr><td>Sales <span>+</span></td><td>1,234.50</td><td>xx</td></tr>
<tr><td>OPM %</td><td>20%</td><td>19%</td></tr>
<tr><td>EPS in Rs</td><td>5.50</td><td>—</td></tr>
<tr><td>Malformed</td><td>999</td></tr></table></section>
<section id="documents"><a href="https://www.infosys.com/report.pdf#page=3">Annual 2025</a>
<a href="https://www.infosys.com/report.pdf">Duplicate</a>
<a href="javascript:alert(1)">Bad</a></section>'''


def test_period_units_missing_values_and_document_deduplication():
    result = fc.parse_page(HTML, "INFY", "https://www.screener.in/company/INFY/consolidated/")
    facts = result["facts"]
    assert len(facts) == 6
    assert facts[0] == {"section": "quarters", "metric": "Sales", "period": "Mar 2025",
                        "period_end": "2025-03-31", "raw": "1,234.50", "value": "1234.50",
                        "unit": "INR crore", "basis": "consolidated",
                        "validation": "source_reported_unverified"}
    assert facts[1]["value"] is None
    assert facts[1]["period_end"] is None
    assert facts[2]["unit"] == "percent"
    assert facts[4]["unit"] == "INR/share"
    assert len(result["documents"]) == 1
    assert result["documents"][0]["url"] == "https://www.infosys.com/report.pdf"


@pytest.mark.parametrize("raw,expected", [("(12.5)", "-12.5"), ("−4", "-4"),
                                         ("NaN", None), ("xx.xx", None), ("", None)])
def test_numbers(raw, expected):
    assert fc.number(raw) == expected


def test_login_or_layout_failure_is_not_success():
    with pytest.raises(ValueError, match="No readable"):
        fc.parse_page("<h1>Login required</h1>", "INFY", "https://www.screener.in/")


def test_snapshot_idempotency_and_restatement(tmp_path, monkeypatch):
    db = fc.initialize(tmp_path)
    body = [HTML]
    monkeypatch.setattr(fc, "fetch", lambda client, url, hosts, limit: (body[0].encode(), url))
    first = fc.collect(None, db, tmp_path, "INFY", 0)
    second = fc.collect(None, db, tmp_path, "INFY", 0)
    assert first["snapshot"] == second["snapshot"]
    assert second["status"] == "unchanged"
    body[0] = HTML.replace("1,234.50", "1,235.50")
    third = fc.collect(None, db, tmp_path, "INFY", 0)
    assert third["snapshot"] != first["snapshot"]
    assert db.execute("SELECT count(*) FROM snapshots").fetchone()[0] == 2
    payload = json.loads((tmp_path / "INFY-knowledge.jsonl").read_text())
    assert payload["visibility"] == "private"
    assert "#quarters" in payload["source_label"]
    db.close()


def test_network_denies_private_dns_and_off_domain(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **kw: [(2, 1, 6, "", ("127.0.0.1", 443))])
    with pytest.raises(ValueError, match="DNS"):
        fc.validate_url("https://www.screener.in/", {"www.screener.in"})
    for url in ["http://www.screener.in/", "https://evil.test/a", "https://user@www.screener.in/"]:
        with pytest.raises(ValueError, match="allowlist"):
            fc.validate_url(url, {"www.screener.in"})


def test_access_denial_not_retried_and_redirect_checked(monkeypatch):
    checked = []

    def validate(url, hosts):
        checked.append(url)
        if urlsplit_host(url) not in hosts:
            raise ValueError("outside allowlist")

    monkeypatch.setattr(fc, "validate_url", validate)
    with (
        httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(403))) as client,
        pytest.raises(httpx.HTTPStatusError),
    ):
        fc.fetch(client, "https://www.screener.in/", {"www.screener.in"}, 100)
    assert len(checked) == 1
    with httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(302, headers={"location": "https://evil.test/"})
    )) as client, pytest.raises(ValueError, match="allowlist"):
        fc.fetch(client, "https://www.screener.in/", {"www.screener.in"}, 100)


def urlsplit_host(url):
    return fc.urlsplit(url).hostname


@pytest.mark.parametrize("data,limit,pdf", [(b"12345", 3, False), (b"<html>blocked</html>", 100, True)])
def test_size_and_pdf_validation(monkeypatch, data, limit, pdf):
    monkeypatch.setattr(fc, "validate_url", lambda *args: None)
    with (
        httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, content=data))) as client,
        pytest.raises(ValueError),
    ):
        fc.fetch(client, "https://www.screener.in/", {"www.screener.in"}, limit, pdf=pdf)


def constituent_csv(count=100):
    return ("Company Name,Industry,Symbol,Series,ISIN Code\n" + "\n".join(
        f"Company {i},Services,CO{i},EQ,INE{i:09d}" for i in range(count)
    )).encode()


def test_official_universe_requires_100_unique_identities():
    assert len(fc.parse_constituents(constituent_csv())) == 100
    for data in [constituent_csv(99), constituent_csv().replace(b"CO99", b"CO98"),
                 constituent_csv().replace(b"CO99", b"../../etc"), b"<html>blocked</html>"]:
        with pytest.raises(ValueError):
            fc.parse_constituents(data)


def test_nifty500_accepts_only_explicit_dummy_placeholder():
    content = constituent_csv(500) + b"\nDummy HEG Ltd.,Capital Goods,DUMMYHEG,EQ,DUM545A01024"
    assert len(fc.parse_constituents(content, 500)) == 500
    with pytest.raises(ValueError, match="Invalid"):
        fc.parse_constituents(content.replace(b"Dummy HEG Ltd.", b"Unverified Company"), 500)
    with pytest.raises(ValueError, match="Expected 500"):
        fc.parse_constituents(constituent_csv(499), 500)


def test_pdf_reuse_requires_matching_content_hash(tmp_path, monkeypatch):
    calls = []

    def fetch(client, url, hosts, limit, pdf=False):
        calls.append(url)
        return (b"%PDF-1.6 test" if pdf else HTML.encode()), url

    monkeypatch.setattr(fc, "fetch", fetch)
    monkeypatch.setattr(fc.time, "sleep", lambda _: None)
    with fc.initialize(tmp_path) as db:
        first = fc.collect(None, db, tmp_path, "INFY", 1)
        second = fc.collect(None, db, tmp_path, "INFY", 1, reuse_documents=True)
        assert len(calls) == 3 and second["documents"][0]["reused"] is True
        path = tmp_path / "objects" / (first["documents"][0]["hash"] + ".pdf")
        path.write_bytes(b"corrupt")
        fc.collect(None, db, tmp_path, "INFY", 1, reuse_documents=True)
        assert len(calls) == 5
        assert path.read_bytes() == b"%PDF-1.6 test"


def test_consolidated_not_found_uses_standalone_and_encodes_symbol(tmp_path, monkeypatch):
    urls = []

    def fetch(client, url, hosts, limit):
        urls.append(url)
        if "consolidated" in url:
            response = httpx.Response(404, request=httpx.Request("GET", url))
            response.raise_for_status()
        return HTML.replace("Consolidated", "Standalone").encode(), url

    monkeypatch.setattr(fc, "fetch", fetch)
    with fc.initialize(tmp_path) as db:
        result = fc.collect(None, db, tmp_path, "M&M", 0)
        payload = json.loads(db.execute("SELECT payload FROM snapshots").fetchone()[0])
    assert result["status"] == "collected"
    assert len(urls) == 2 and "M%26M" in urls[0]
    assert payload["tables"][0]["basis"] == "standalone"


def test_missing_document_is_incomplete_not_success(tmp_path, monkeypatch):
    monkeypatch.setattr(fc, "fetch", lambda client, url, hosts, limit: (HTML.split('<section id="documents">')[0].encode(), url))
    with fc.initialize(tmp_path) as db:
        result = fc.collect(None, db, tmp_path, "NEWCO", 1)
    assert result["document_coverage"] == "incomplete"
    assert result["documents"] == []


def test_empty_consolidated_table_can_use_standalone(tmp_path, monkeypatch):
    empty = '<h1>Company</h1><section id="quarters"><table class="data-table"><tr><th></th></tr></table></section>'
    calls = []

    def fetch(client, url, hosts, limit):
        calls.append(url)
        return (empty if "consolidated" in url else HTML).encode(), url

    monkeypatch.setattr(fc, "fetch", fetch)
    with fc.initialize(tmp_path) as db:
        assert fc.collect(None, db, tmp_path, "IRFC", 0)["status"] == "collected"
    assert len(calls) == 2


def test_challenge_page_does_not_trigger_standalone_retry(tmp_path, monkeypatch):
    calls = []

    def fetch(client, url, hosts, limit):
        calls.append(url)
        return b"<h1>Please log in</h1>", url

    monkeypatch.setattr(fc, "fetch", fetch)
    with fc.initialize(tmp_path) as db, pytest.raises(ValueError, match="No readable"):
        fc.collect(None, db, tmp_path, "IRFC", 0)
    assert len(calls) == 1


def test_nse_main_board_and_sme_schemas():
    main = fc.parse_nse_list(b'SYMBOL,NAME OF COMPANY, SERIES, ISIN NUMBER\nINFY,Infosys,EQ,INE009A01021\n', 'main_board')
    sme = fc.parse_nse_list(b'SYMBOL,NAME_OF_COMPANY,SERIES,ISIN_NUMBER\nSMETEST,Example,SM,INE123A01012\n', 'sme')
    merged = fc.merge_nse_lists(main, sme)
    assert len(merged) == 2 and merged['SMETEST']['segments'] == ['sme']
    migrated = {'INFY': dict(main['INFY'], segments=['sme'])}
    assert fc.merge_nse_lists(main, migrated)['INFY']['segments'] == ['main_board', 'sme']
    assert main['INFY']['segments'] == ['main_board']
    with pytest.raises(ValueError, match='Conflicting'):
        fc.merge_nse_lists(main, {'INFY': dict(main['INFY'], isin='INE123A01012')})
    with pytest.raises(ValueError, match='conflicting symbols'):
        fc.merge_nse_lists(main, {'NEWNAME': main['INFY']})


@pytest.mark.parametrize('data', [b'<html>Unavailable</html>', b'SYMBOL,NAME OF COMPANY, SERIES, ISIN NUMBER\nINFY,Infosys,ETF,INE009A01021\n', b'SYMBOL,NAME OF COMPANY, SERIES, ISIN NUMBER\n../bad,Test,EQ,INE009A01021\n'])
def test_nse_list_rejects_invalid_response(data):
    with pytest.raises(ValueError):
        fc.parse_nse_list(data, 'main_board')


@pytest.mark.parametrize('reason', ['HTTP 429; no bypass attempted', 'ConnectError', 'ConnectTimeout', 'ReadTimeout'])
def test_source_denial_stops_scheduling_remaining_companies(reason):
    from concurrent.futures import ThreadPoolExecutor
    calls = []

    def worker(symbol):
        calls.append(symbol)
        return {'symbol': symbol, 'status': 'failed', 'reason': reason}

    with ThreadPoolExecutor(max_workers=1) as executor:
        rows = list(fc.bounded_results(executor, worker, ['INFY', 'TCS', 'ITC'], 1))
    assert len(rows) == 1 and calls == ['INFY']


def test_annual_only_company_keeps_missing_quarter_flag():
    annual = HTML.replace('id="quarters"', 'id="profit-loss"')
    result = fc.parse_page(annual, "ARMEE", "https://www.screener.in/company/ARMEE/")
    assert result['facts'][0]['value'] == '1234.50'
    assert 'quarters' in result['missing_data_sections']
    assert 'profit-loss' not in result['missing_data_sections']
    with pytest.raises(ValueError, match='No readable'):
        fc.parse_page(HTML.replace('id="quarters"', 'id="ratios"'), 'TEST', 'https://www.screener.in/')


def test_reviewed_alias_preserves_symbol_and_checks_company(tmp_path, monkeypatch):
    monkeypatch.setattr(fc, 'fetch', lambda client, url, hosts, limit: (
        HTML.replace('Infosys Ltd', 'Ashapuri Gold Ornament Ltd').encode(), url))
    with fc.initialize(tmp_path) as db:
        row = fc.collect(None, db, tmp_path, 'AGOL', 0)
        assert row['symbol'] == 'AGOL'
    monkeypatch.setattr(fc, 'fetch', lambda client, url, hosts, limit: (HTML.encode(), url))
    with fc.initialize(tmp_path) as db, pytest.raises(ValueError, match='alias company name'):
        fc.collect(None, db, tmp_path, 'AGOL', 0)


def test_rights_entitlement_is_explicitly_linked_to_issuer():
    raw=b'SYMBOL,NAME OF COMPANY,SERIES,ISIN NUMBER\nNATCO-RE,Natco Pharma Limited-RE,BE,INE987B20018\n'
    row=fc.parse_nse_list(raw, 'main_board')['NATCO-RE']
    assert row['security_type']=='rights_entitlement'
    assert row['issuer_symbol']=='NATCOPHARM'
    assert row['isin'] != row['issuer_isin']
    with pytest.raises(ValueError, match='security identity'):
        fc.parse_nse_list(raw.replace(b'INE987B20018',b'INE987B01026'), 'main_board')


def test_resume_only_missing_keeps_failures_and_does_not_hide_corruption():
    class Store:
        def latest(self, symbol):
            if symbol == "TCS":
                raise ValueError("No archived snapshot for TCS")
            if symbol == "ITC":
                raise ValueError("Parquet snapshot integrity mismatch")
            return {"symbol": symbol}

    assert fc.missing_symbols(Store(), ['INFY', 'TCS', 'NATCO-RE']) == (['TCS'], ['INFY', 'NATCO-RE'])
    with pytest.raises(ValueError, match="integrity"):
        fc.missing_symbols(Store(), ['ITC'])
