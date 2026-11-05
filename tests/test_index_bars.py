"""Official NSE daily index bars from ind_close_all on nsearchives."""

from datetime import date

import httpx
import pytest

from shunkan.data import index_bars
from shunkan.data.provider import DataError, FetchFailed

D = date(2026, 11, 5)
FILE = "ind_close_all_05112026.csv"
HEAD = ("Index Name,Index Date,Open Index Value,High Index Value,Low Index Value,Closing Index Value,"
        "Points Change,Change(%),Volume,Turnover (Rs. Cr.),P/E,P/B,Div Yield\n")
NIFTY = "Nifty 50,05-11-2026,24378.05,24579.9,24296.3,24536.6,576.0,2.4,534628921,54548.69,20.25,3.01,1.42\n"
BANK = "Nifty Bank,05-11-2026,57102.8,57847.65,56718.2,57773.65,1860.05,3.33,497471889,18305.05,12.77,1.74,1.08\n"
VIX = "India VIX,05-11-2026,15.1875,15.9575,11.62,15.58,0.39,2.58,-,-,-,-,-\n"


def client(body: str, seen: list):
    def handler(request):
        seen.append(request.url.path.rsplit("/", 1)[-1])
        return httpx.Response(200, content=body.encode())

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_paths_and_url(tmp_path):
    assert index_bars.url_for(D) == f"https://nsearchives.nseindia.com/content/indices/{FILE}"
    assert index_bars.path_for(D, tmp_path) == tmp_path / "indices" / "raw" / "2026" / FILE


def test_fetch_is_write_once_and_bars_read_back(tmp_path):
    seen = []
    c = client(HEAD + NIFTY + BANK + VIX, seen)
    p = index_bars.fetch(D, tmp_path, c)
    assert index_bars.fetch(D, tmp_path, c) == p and seen == [FILE]
    assert index_bars.bar(D, "NIFTY", tmp_path)["close"] == 24536.6
    assert index_bars.bar(D, "BANKNIFTY", tmp_path)["close"] == 57773.65
    v = index_bars.bar(D, "INDIAVIX", tmp_path)
    assert (v["open"], v["high"], v["low"], v["close"]) == (15.1875, 15.9575, 11.62, 15.58)


def test_a_file_without_both_indices_is_refused_and_not_saved(tmp_path):
    with pytest.raises(FetchFailed, match="has no Nifty Bank"):
        index_bars.fetch(D, tmp_path, client(HEAD + NIFTY + VIX, []))
    assert not index_bars.path_for(D, tmp_path).exists()


def test_a_bar_that_is_not_a_bar_or_another_day_raises(tmp_path):
    p = index_bars.path_for(D, tmp_path)
    p.parent.mkdir(parents=True)
    p.write_text(HEAD + NIFTY.replace("24296.3", "24600.0") + BANK)
    with pytest.raises(DataError, match="not a bar"):
        index_bars.bar(D, "NIFTY", tmp_path)
    with pytest.raises(DataError, match="has no India VIX"):
        index_bars.bar(D, "INDIAVIX", tmp_path)
    with pytest.raises(DataError, match="carries 2026-11-05, not 2026-11-06"):
        index_bars.parse(p, date(2026, 11, 6))
    with pytest.raises(DataError, match="no official index bar"):
        index_bars.bar(D, "FINNIFTY", tmp_path)
