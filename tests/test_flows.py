"""FII derivatives (fii_stats .xls) and FII/DII cash flows, offline."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import httpx
import pytest

from shunkan.data import flows
from shunkan.data.provider import FetchFailed, Unpublished

FIX = Path(__file__).parent / "fixtures"
XLS = (FIX / "fii_stats_03-Nov-2026.xls").read_bytes()
FIIDII = json.loads((FIX / "fiidii.json").read_text())
NOV3, NOV4 = date(2026, 11, 3), date(2026, 11, 4)
DENIED = b"<HTML><HEAD><TITLE>Access Denied</TITLE></HEAD><BODY>You don't have permission"


def _client(status=200, body=b"", seen=None):
    def handler(request):
        if seen is not None:
            seen.append(request)
        return httpx.Response(status, content=body)

    return httpx.Client(transport=httpx.MockTransport(handler))


class FakeWWW:
    def __init__(self, payload):
        self.payload, self.paths = payload, []

    def get_json(self, path, params=None):
        self.paths.append(path)
        return self.payload


def test_parse_fii_stats_reads_the_real_file():
    out = flows.parse_fii_stats(XLS, NOV3)
    assert out["date"] == "2026-11-03"
    assert set(out) == {"date", "index_futures", "nifty_futures", "banknifty_futures",
                        "index_options", "stock_futures", "stock_options"}
    assert out["nifty_futures"] == {"buy_contracts": 34551, "buy_cr": 5326.48,
                                    "sell_contracts": 5964, "sell_cr": 919.73,
                                    "net_cr": 4406.75, "oi_contracts": 187486,
                                    "oi_cr": 28989.87}
    assert out["index_futures"]["buy_contracts"] == 42218
    assert out["banknifty_futures"]["oi_cr"] == 9498.51
    assert out["stock_options"]["sell_cr"] == 33887.24


def test_title_naming_another_day_fails():
    with pytest.raises(FetchFailed, match="03-Nov-2026"):
        flows.parse_fii_stats(XLS, NOV4)


def test_missing_label_fails_naming_it():
    with pytest.raises(FetchFailed, match="STOCK OPTIONS"):
        flows.parse_fii_stats(XLS.replace(b"STOCK OPTIONS", b"STOCK OPTIONZ"), NOV3)


def _patched_rows(monkeypatch, edit):
    real = flows._rows
    monkeypatch.setattr(flows, "_rows", lambda data: edit(real(data)))


def test_swapped_buy_sell_header_fails(monkeypatch):
    def swap(rows):
        rows[1][1:5] = ["SELL", "SELL", "BUY", "BUY"]
        return rows

    _patched_rows(monkeypatch, swap)
    with pytest.raises(FetchFailed, match="header changed"):
        flows.parse_fii_stats(XLS, NOV3)


def test_renamed_header_in_the_real_bytes_fails():
    with pytest.raises(FetchFailed, match="header changed"):
        flows.parse_fii_stats(XLS.replace(b"SELL", b"SOLD"), NOV3)


def test_duplicate_label_fails(monkeypatch):
    def dup(rows):
        return rows + [["NIFTY FUTURES", "1", "1.00", "1", "1.00", "1", "1.00"]]

    _patched_rows(monkeypatch, dup)
    with pytest.raises(FetchFailed, match="NIFTY FUTURES"):
        flows.parse_fii_stats(XLS, NOV3)


def test_non_xls_body_fails():
    with pytest.raises(FetchFailed):
        flows.parse_fii_stats(DENIED, NOV3)


def test_fii_derivatives_caches_and_reads_back_offline(tmp_path):
    seen = []
    out = flows.fii_derivatives(NOV3, root=tmp_path, client=_client(200, XLS, seen))
    assert seen[0].url.path == "/content/fo/fii_stats_03-Nov-2026.xls"
    cached = tmp_path / "flows" / "fii_stats" / "2026" / "fii_stats_03-Nov-2026.xls"
    assert cached.read_bytes() == XLS
    again = flows.fii_derivatives(NOV3, root=tmp_path, client=_client(500, b"", seen))
    assert again == out and len(seen) == 1


@pytest.mark.parametrize("status,body,error", [(404, b"Not Found", Unpublished),
                                                (200, DENIED, FetchFailed)])
def test_fii_derivatives_failure_writes_nothing(tmp_path, status, body, error):
    with pytest.raises(error):
        flows.fii_derivatives(NOV3, root=tmp_path, client=_client(status, body))
    assert not (tmp_path / "flows").exists()


def test_wrong_day_file_is_not_cached(tmp_path):
    with pytest.raises(FetchFailed):
        flows.fii_derivatives(NOV4, root=tmp_path, client=_client(200, XLS))
    assert not (tmp_path / "flows").exists()


def test_parse_fiidii_fixture():
    out = flows.parse_fiidii(FIIDII)
    assert out["date"] == "2026-11-03"
    assert out["fii"] == {"buy_cr": 20970.51, "sell_cr": 19720.11, "net_cr": 1250.40}
    assert out["dii"]["net_cr"] == -3678.85
    assert out["basis"] == "NSE provisional, NSE+BSE+MSEI"


def test_parse_fiidii_reads_comma_grouped_strings():
    js = [dict(r, buyValue="1,16,723.91") if r["category"] == "DII" else r for r in FIIDII]
    assert flows.parse_fiidii(js)["dii"]["buy_cr"] == 116723.91


def test_parse_fiidii_dates_must_agree():
    js = [dict(r, date="31-Oct-2026") if r["category"] == "DII" else r for r in FIIDII]
    with pytest.raises(FetchFailed, match="date"):
        flows.parse_fiidii(js)


def test_parse_fiidii_duplicate_category_fails():
    with pytest.raises(FetchFailed, match="FII/FPI"):
        flows.parse_fiidii(FIIDII + [dict(FIIDII[1], netValue="9.99")])


@pytest.mark.parametrize("js", [{"error": "x"}, [], [FIIDII[0]]])
def test_parse_fiidii_wrong_shape_fails(js):
    with pytest.raises(FetchFailed):
        flows.parse_fiidii(js)


def test_fii_dii_cash_saves_by_the_payload_date(tmp_path):
    www = FakeWWW(FIIDII)
    out = flows.fii_dii_cash(root=tmp_path, www=www)
    assert www.paths == ["/api/fiidiiTradeReact"]
    path = tmp_path / "flows" / "fiidii" / "2026" / "2026-11-03.json"
    assert json.loads(path.read_text()) == FIIDII
    assert out == flows.parse_fiidii(FIIDII)
    assert flows.fii_dii_cash_on(NOV3, root=tmp_path) == out
    assert flows.fii_dii_cash_on(NOV4, root=tmp_path) is None


def test_fii_dii_cash_bad_payload_writes_nothing(tmp_path):
    with pytest.raises(FetchFailed):
        flows.fii_dii_cash(root=tmp_path, www=FakeWWW({"error": "x"}))
    assert not (tmp_path / "flows").exists()


def test_fii_dii_cash_returns_what_is_on_disk(tmp_path):
    earlier = [dict(r, netValue="1.00") if r["category"] == "FII/FPI" else r for r in FIIDII]
    path = tmp_path / "flows" / "fiidii" / "2026" / "2026-11-03.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(earlier))
    out = flows.fii_dii_cash(root=tmp_path, www=FakeWWW(FIIDII))
    assert out["fii"]["net_cr"] == 1.0
    assert out == flows.fii_dii_cash_on(NOV3, root=tmp_path)
