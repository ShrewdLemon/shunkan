"""India macro: the policy corridor and the series behind it.

The failure mode of a rate scraper is not an exception - it is a
plausible-looking stale number. RBI can restyle its homepage whenever it
likes, and a macro screen that quietly reports the 2019 repo rate is worse
than one that says it could not read the page. These tests are mostly about
that, and about the caching bug that made a transient blip permanent.
"""
from __future__ import annotations

import pytest

from shunkan.data import macro
from shunkan.data.provider import DataError


@pytest.fixture(autouse=True)
def _clear_caches():
    macro._SERIES_CACHE.clear()
    if hasattr(macro.policy_rates, "cache"):
        macro.policy_rates.cache.clear()
    yield
    macro._SERIES_CACHE.clear()


# --- the parser, against page shapes RBI has actually served -----------------

_PAGE = """
<html><body><table>
<tr><td>Policy Repo Rate</td><td>5.25%</td></tr>
<tr><td>Standing Deposit Facility Rate</td><td>5.00%</td></tr>
<tr><td>Marginal Standing Facility Rate</td><td>5.50%</td></tr>
<tr><td>Bank Rate</td><td>5.50%</td></tr>
<tr><td>Fixed Reverse Repo Rate</td><td>3.35%</td></tr>
<tr><td>CRR</td><td>3.00%</td></tr>
<tr><td>SLR</td><td>18.00%</td></tr>
</table></body></html>
"""


def test_reads_every_rate_off_a_realistic_page(monkeypatch):
    monkeypatch.delenv("SHUNKAN_OFFLINE", raising=False)
    monkeypatch.setattr(macro, "_get", lambda *a, **k: _PAGE)
    out = macro.policy_rates()
    got = {r["key"]: r["pct"] for r in out["rates"]}
    assert got == {"repo": 5.25, "sdf": 5.0, "msf": 5.5, "bank_rate": 5.5,
                   "reverse_repo": 3.35, "crr": 3.0, "slr": 18.0}
    assert out["corridor_width_bps"] == 50      # MSF 5.50 - SDF 5.00
    assert out["found"] == out["expected"] == 7
    assert out["source"].startswith("https://www.rbi.org.in")


def test_a_label_split_by_inline_markup_still_reads():
    """RBI bolds and spans things inside its rate labels.

    The first version of _flatten replaced tags with a pipe, so
    "Policy <b>Repo</b> Rate" became "Policy |Repo| Rate" and matched nothing.
    Every separator reads the CURRENT page identically, so the choice had to
    be made on the cases where they differ - and these are the two.
    """
    flat = macro._flatten("<td>Policy <b>Repo</b> Rate</td><td>5.25%</td>")
    r = macro._find_rate(flat, "repo", "Policy Repo Rate", "")
    assert r.pct == 5.25, "a label split by inline markup was not found"


def test_a_percentage_in_its_own_cell_still_reads():
    flat = macro._flatten("<td>Bank Rate</td><td>5.50</td><td>%</td>")
    r = macro._find_rate(flat, "bank_rate", "Bank Rate", "")
    assert r.pct == 5.50, "the number and its % sign were in separate cells"


def test_distinct_cells_do_not_merge_into_one_token():
    """The separator must still keep neighbouring cells apart."""
    flat = macro._flatten("<td>CRR</td><td>3.00%</td>")
    assert "CRR 3.00%" in flat or "CRR 3.00 %" in flat, flat


def test_a_distant_number_is_not_claimed_as_a_rate(monkeypatch):
    """An unbounded search pairs a label with a number screens away - which is
    how a scraper starts reporting a rate that was true years ago."""
    monkeypatch.delenv("SHUNKAN_OFFLINE", raising=False)
    page = ("<td>Policy Repo Rate</td>" + "<td>filler text</td>" * 40
            + "<td>4.00%</td><td>Bank Rate</td><td>5.50%</td>")
    monkeypatch.setattr(macro, "_get", lambda *a, **k: page)
    rates = {r["key"]: r for r in macro.policy_rates()["rates"]}
    assert rates["repo"]["pct"] is None, "claimed a rate from far down the page"
    assert "layout" in (rates["repo"]["reason"] or "")


def test_a_restyled_page_refuses_rather_than_reporting_an_empty_corridor(monkeypatch):
    monkeypatch.delenv("SHUNKAN_OFFLINE", raising=False)
    monkeypatch.setattr(macro, "_get", lambda *a, **k: "<html>nothing here</html>")
    with pytest.raises(DataError) as ei:
        macro.policy_rates()
    assert "structure has changed" in str(ei.value)


def test_a_partial_read_reports_which_rates_are_missing(monkeypatch):
    """Some rates present is a real answer; it must say which are not."""
    monkeypatch.delenv("SHUNKAN_OFFLINE", raising=False)
    monkeypatch.setattr(macro, "_get",
                        lambda *a, **k: "<td>Policy Repo Rate</td><td>5.25%</td>")
    out = macro.policy_rates()
    assert out["found"] == 1 and out["expected"] == 7
    missing = [r for r in out["rates"] if r["pct"] is None]
    assert len(missing) == 6
    assert all(r["reason"] for r in missing), "a gap with no stated reason"
    assert out["corridor_width_bps"] is None, "width computed without a floor"


def test_no_hardcoded_fallback_rate_exists():
    """The one thing this module must never do."""
    src = (macro.__file__)
    text = open(src).read()
    body = text[text.index("def _find_rate"):text.index("def dashboard")]
    for suspicious in ("6.5", "6.50", "5.5 if", "or 6.", "default=6"):
        assert suspicious not in body, \
            f"a fallback rate {suspicious!r} crept into the parser"


def test_offline_refuses_by_name(monkeypatch):
    monkeypatch.setenv("SHUNKAN_OFFLINE", "1")
    with pytest.raises(DataError) as ei:
        macro.policy_rates()
    assert "SHUNKAN_OFFLINE" in str(ei.value)


# --- the caching bug --------------------------------------------------------

def test_a_failed_series_is_not_cached(monkeypatch):
    """THE BUG THIS SUITE EXISTS FOR. series() returns failures as a value
    rather than raising, so a plain @ttl_cache stored them like any other
    result - one transient timeout blanked the screen for six hours."""
    monkeypatch.delenv("SHUNKAN_OFFLINE", raising=False)
    calls = {"n": 0}

    def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] <= 2:                    # both attempts of the first call
            raise TimeoutError("simulated")
        return ('[{"page":1},[{"date":"2024","value":5.5},'
                '{"date":"2025","value":6.5}]]')

    monkeypatch.setattr(macro, "_get", flaky)
    first = macro.series("FP.CPI.TOTL.ZG")
    assert first.reason and not first.points

    second = macro.series("FP.CPI.TOTL.ZG")
    assert second.points, "the refusal was cached — a blip became a six-hour gap"
    assert second.points[-1]["year"] == 2025


def test_a_successful_series_is_cached(monkeypatch):
    monkeypatch.delenv("SHUNKAN_OFFLINE", raising=False)
    calls = {"n": 0}

    def counted(*a, **k):
        calls["n"] += 1
        return '[{"page":1},[{"date":"2025","value":2.4}]]'

    monkeypatch.setattr(macro, "_get", counted)
    macro.series("FP.CPI.TOTL.ZG")
    macro.series("FP.CPI.TOTL.ZG")
    assert calls["n"] == 1, "a good result was refetched"


def test_series_retries_once_before_giving_up(monkeypatch):
    monkeypatch.delenv("SHUNKAN_OFFLINE", raising=False)
    calls = {"n": 0}

    def once_flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise TimeoutError("blip")
        return '[{"page":1},[{"date":"2025","value":2.4}]]'

    monkeypatch.setattr(macro, "_get", once_flaky)
    s = macro.series("FP.CPI.TOTL.ZG")
    assert s.points, "a single blip was treated as a failure"
    assert calls["n"] == 2


def test_null_observations_are_dropped_not_zeroed(monkeypatch):
    """World Bank sends nulls for years it has no reading. A null is not a 0%
    inflation print."""
    monkeypatch.delenv("SHUNKAN_OFFLINE", raising=False)
    monkeypatch.setattr(macro, "_get", lambda *a, **k:
                        '[{"page":1},[{"date":"2023","value":null},'
                        '{"date":"2024","value":5.4}]]')
    s = macro.series("FP.CPI.TOTL.ZG")
    assert [p["year"] for p in s.points] == [2024]
    assert all(p["value"] != 0 for p in s.points)


def test_an_all_null_window_says_so(monkeypatch):
    monkeypatch.delenv("SHUNKAN_OFFLINE", raising=False)
    monkeypatch.setattr(macro, "_get", lambda *a, **k:
                        '[{"page":1},[{"date":"2023","value":null}]]')
    s = macro.series("FP.CPI.TOTL.ZG")
    assert not s.points and "null at the source" in s.reason


def test_points_are_in_year_order(monkeypatch):
    monkeypatch.delenv("SHUNKAN_OFFLINE", raising=False)
    monkeypatch.setattr(macro, "_get", lambda *a, **k:
                        '[{"page":1},[{"date":"2025","value":3},'
                        '{"date":"2010","value":1},{"date":"2018","value":2}]]')
    s = macro.series("FP.CPI.TOTL.ZG")
    years = [p["year"] for p in s.points]
    assert years == sorted(years), f"series is not chronological: {years}"


def test_unknown_indicator_is_refused():
    with pytest.raises(DataError):
        macro.series("NOT.A.REAL.INDICATOR")


def test_dashboard_survives_rbi_being_down(monkeypatch):
    """RBI and the World Bank are different institutions; one being
    unreachable says nothing about the other."""
    monkeypatch.delenv("SHUNKAN_OFFLINE", raising=False)

    def selective(url, *a, **k):
        if "rbi.org.in" in url:
            raise TimeoutError("rbi down")
        return '[{"page":1},[{"date":"2025","value":2.4}]]'

    monkeypatch.setattr(macro, "_get", selective)
    out = macro.dashboard()
    assert out["corridor"] is None
    assert out["corridor_reason"], "no reason given for the missing corridor"
    assert any(s["points"] for s in out["series"]), \
        "RBI being down took the World Bank series with it"
