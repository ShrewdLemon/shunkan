"""NSE serves truncated filings with a matching Content-Length.

httpx sees a clean 200 and raises nothing, so the corruption only surfaces
later as "PDFium: Data format error" - which reads as a broken parser or a
scanned filing and is neither. Measured 2026-08-26 across the 59 core names:
3 are persistently short (AUBANK 99.4%, COALINDIA 56.8%, NTPC 17.5%), and any
symbol can hit the transient form because NSE's edge replicas disagree with
each other.
"""

from __future__ import annotations

import io
import zipfile

import pytest

from shunkan.data import filings
from shunkan.data.provider import DataError

COMPLETE = b"%PDF-1.7\n/L 120\n" + b"x" * 80 + b"\ntrailer\n%%EOF\n"
TRUNCATED = b"%PDF-1.7\n/L 22660544\n" + b"x" * 60          # no %%EOF


def _serve(monkeypatch, bodies):
    """Hand out one body per call, so a retry can land on a different replica."""
    seq = list(bodies)

    class R:
        def __init__(self, c):
            self.content = c

    monkeypatch.setattr(filings, "_H", {"User-Agent": "t"})
    import httpx

    monkeypatch.setattr(httpx, "get", lambda *a, **k: R(seq.pop(0) if seq else b""))
    monkeypatch.setattr("time.sleep", lambda *a: None)


def test_a_complete_pdf_is_returned(monkeypatch):
    _serve(monkeypatch, [COMPLETE])
    assert filings._download_report("u") == COMPLETE


def test_truncation_is_detected_by_the_eof_trailer_not_the_length_header(monkeypatch):
    """KOTAKBANK's /L is 20 bytes under its Content-Length and it parses fine
    at 522 pages, so /L is not a safe test. %%EOF flagged all three real cases
    with no false positives."""
    _serve(monkeypatch, [TRUNCATED, TRUNCATED, TRUNCATED])
    with pytest.raises(DataError) as e:
        filings._download_report("u", attempts=3)
    assert "incomplete" in str(e.value)
    # the message must name NSE's truncation, not implicate the parser
    assert "truncated at" in str(e.value)
    assert "22,660,544" in str(e.value)


def test_a_retry_lands_on_a_good_replica(monkeypatch):
    """AUBANK FY2025 pulled eight times gave a complete body four times and a
    truncated one four times, Content-Length agreeing every time."""
    _serve(monkeypatch, [TRUNCATED, COMPLETE])
    assert filings._download_report("u", attempts=3) == COMPLETE


def test_an_html_error_page_is_not_mistaken_for_a_filing(monkeypatch):
    _serve(monkeypatch, [b"<!DOCTYPE html><html>404</html>"] * 3)
    with pytest.raises(DataError) as e:
        filings._download_report("u", attempts=3)
    assert "not a PDF" in str(e.value)


def test_older_filings_arrive_as_zips_and_are_unwrapped(monkeypatch):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("AR_2023.pdf", COMPLETE)
    _serve(monkeypatch, [buf.getvalue()])
    assert filings._download_report("u") == COMPLETE


def test_fallback_skips_the_duplicated_newest_row(monkeypatch):
    """NSE returns a DUPLICATED newest row for exactly the symbols whose newest
    upload is broken - AUBANK, COALINDIA, NTPC - so a naive ars[1] retries the
    identical bad URL."""
    reports = [{"url": "bad", "to_year": "2026"},
               {"url": "bad", "to_year": "2026"},   # the duplicate
               {"url": "good", "to_year": "2025"}]
    monkeypatch.setattr(filings, "annual_reports", lambda s: reports)
    tried = []

    def fake(url, max_pages=600):
        tried.append(url)
        if url == "bad":
            raise DataError("NSE served an incomplete file")
        return ("x" * 60_000, 120)

    monkeypatch.setattr(filings, "fetch_report_text", fake)
    ar, text, pages = filings.latest_readable_report("AUBANK")
    assert tried == ["bad", "good"], "the duplicate row must not be retried"
    # the caller must be able to label the extraction with the year READ
    assert ar["to_year"] == "2025"


def test_no_readable_report_says_which_years_were_tried(monkeypatch):
    monkeypatch.setattr(filings, "annual_reports",
                        lambda s: [{"url": "a", "to_year": "2026"},
                                   {"url": "b", "to_year": "2025"}])
    monkeypatch.setattr(filings, "fetch_report_text",
                        lambda *a, **k: (_ for _ in ()).throw(DataError("short")))
    with pytest.raises(DataError) as e:
        filings.latest_readable_report("X")
    assert "FY2026" in str(e.value) and "FY2025" in str(e.value)


def test_a_covering_letter_is_not_an_annual_report(monkeypatch):
    """NSE's FY2026 row for UBL is a 2-page, 3.9 KB covering letter that links
    to the report instead of containing it - and FY2025 is the same letter.
    Both PARSE cleanly, so a readability check alone accepted them and the
    company would have been seeded from a compliance note."""
    reports = [{"url": "letter26", "to_year": "2026"},
               {"url": "letter25", "to_year": "2025"},
               {"url": "real24", "to_year": "2024"}]
    monkeypatch.setattr(filings, "annual_reports", lambda s: reports)

    def fake(url, max_pages=600):
        if url.startswith("letter"):
            return ("Please find enclosed the link to the Annual Report.", 2)
        return ("x" * 900_000, 165)

    monkeypatch.setattr(filings, "fetch_report_text", fake)
    ar, text, pages = filings.latest_readable_report("UBL")
    assert ar["to_year"] == "2024" and pages == 165


def test_the_stub_reason_says_what_it_was(monkeypatch):
    monkeypatch.setattr(filings, "annual_reports",
                        lambda s: [{"url": "a", "to_year": "2026"}])
    monkeypatch.setattr(filings, "fetch_report_text", lambda *a, **k: ("short", 2))
    with pytest.raises(DataError) as e:
        filings.latest_readable_report("X")
    assert "covering letter" in str(e.value)
