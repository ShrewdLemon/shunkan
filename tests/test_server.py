"""API tests for the web server (offline synthetic mode via conftest)."""

import pytest
from fastapi.testclient import TestClient

from shunkan.server import create_app


@pytest.fixture(scope="module")
def client():
    with TestClient(create_app()) as c:
        yield c


def test_status(client):
    r = client.get("/api/status").json()
    assert r["offline"] is True
    assert r["session"]["phase"]
    assert "version" in r


def test_index_served(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "SHUNKAN" in r.text


def test_pulse(client):
    r = client.get("/api/pulse").json()
    assert "india" in r and "global" in r
    assert any("price" in t for t in r["india"])


def test_history_shape(client):
    r = client.get("/api/history/RELIANCE?period=3mo").json()
    assert len(r["candles"]) > 10
    c0 = r["candles"][0]
    assert {"time", "open", "high", "low", "close"} <= set(c0)
    assert len(r["volume"]) == len(r["candles"])


def test_sparks(client):
    r = client.get("/api/sparks?symbols=AAA,BBB").json()
    assert set(r) == {"AAA", "BBB"}
    assert all(len(v) > 5 and all(isinstance(x, float) for x in v) for v in r.values())


def test_chain(client):
    r = client.get("/api/chain/NIFTY").json()
    assert r["spot"] > 0
    assert len(r["rows"]) > 5
    assert sum(1 for row in r["rows"] if row["atm"]) == 1
    a = r["analytics"]
    assert a["pcr_oi"] > 0 and a["max_pain"] > 0
    assert a["bias"] in ("bullish", "bearish", "neutral")


def test_payoff_template_and_custom(client):
    r = client.get("/api/payoff/NIFTY?strategy=iron_condor").json()
    assert len(r["legs"]) == 4
    assert len(r["curve"]) > 50
    assert 0 < r["pop"] < 1
    strike = int(r["curve"][len(r["curve"]) // 2]["x"] // 50 * 50)
    chain = client.get("/api/chain/NIFTY").json()
    s0 = int(chain["rows"][0]["strike"])
    custom = client.get(f"/api/payoff/NIFTY?legs=%2B{s0}CE").json()
    assert custom["name"] == "custom"
    assert custom["unlimited_profit"] is True


def test_payoff_bad_strategy_400(client):
    assert client.get("/api/payoff/NIFTY?strategy=money_printer").status_code == 400


def test_iv(client):
    r = client.get("/api/iv/NIFTY").json()
    assert 0 < r["atm_iv"] < 1
    assert "21" in r["cone"]
    assert len(r["smile"]) > 5


def test_volume(client):
    r = client.get("/api/volume/RELIANCE").json()
    assert r["poc"] > 0
    assert len(r["profile"]) > 10
    assert r["value_area"][0] <= r["value_area"][1]


def test_backtest_modes(client):
    base = {"symbol": "NIFTY", "strategy": "sma_cross", "period": "5y"}
    bt = client.post("/api/backtest", json=base).json()
    assert bt["mode"] == "backtest"
    assert len(bt["equity"]) > 100
    assert "sharpe" in bt["metrics"]

    wf = client.post("/api/backtest", json={**base, "period": "10y", "mode": "walkforward"}).json()
    assert wf["mode"] == "walkforward"
    assert len(wf["windows"]) >= 3
    assert "verdict" in wf

    mc = client.post("/api/backtest", json={**base, "mode": "montecarlo"}).json()
    assert mc["mode"] == "montecarlo"
    assert 0 <= mc["prob_loss"] <= 1
    assert len(mc["bands"]["p50"]) > 100


def test_chart_catalog(client):
    r = client.get("/api/chart/catalog").json()
    assert "SMA" in r["indicators"] and "RSI" in r["indicators"]
    assert r["indicators"]["SMA"]["pane"] == "price"
    assert r["indicators"]["RSI"]["pane"] == "lower"
    assert r["indicators"]["RSI"]["guides"] == [30, 70]


def test_chart_indicators(client):
    r = client.get("/api/chart/indicators/RELIANCE?period=1y&interval=1d"
                   "&specs=sma:20,bb:20,rsi:14,macd").json()
    by_id = {i["id"]: i for i in r["indicators"]}
    assert set(by_id) == {"SMA:20", "BB:20", "RSI:14", "MACD:0"}
    # overlay vs oscillator pane assignment
    assert by_id["SMA:20"]["pane"] == "price"
    assert by_id["RSI:14"]["pane"] == "lower"
    # Bollinger has three lines, MACD has a histogram, every line has clean points
    assert len(by_id["BB:20"]["lines"]) == 3
    assert by_id["MACD:0"]["hist"]
    pts = by_id["SMA:20"]["lines"][0]["data"]
    assert len(pts) > 50 and all("time" in p and "value" in p for p in pts)
    # provenance present on each indicator
    assert all(i["prov"]["formula"] and i["prov"]["source"] for i in r["indicators"])


def test_chart_indicators_ignores_unknown(client):
    r = client.get("/api/chart/indicators/RELIANCE?specs=bogus:5,sma:10").json()
    assert [i["id"] for i in r["indicators"]] == ["SMA:10"]


def test_chart_config_roundtrip(client):
    cfg = {"type": "candles", "interval": "1d", "indicators": ["sma:20", "rsi:14"]}
    assert client.post("/api/chart/config/RELIANCE", json=cfg).json()["ok"]
    back = client.get("/api/chart/config/RELIANCE").json()
    assert back["indicators"] == ["sma:20", "rsi:14"]
    assert client.get("/api/chart/config/NOTSET").json() == {}


def test_builder_indicators_catalog(client):
    r = client.get("/api/builder/indicators").json()
    assert "RSI" in r["indicators"] and "ADX" in r["indicators"]
    assert {"<", ">", "cross_above", "cross_below"} <= set(r["operators"])
    assert "atr" in r["sl_tp_modes"]
    assert "1d" in r["intervals"]


def test_builder_backtest_runs(client):
    body = {
        "symbol": "NIFTY", "interval": "1d", "period": "5y",
        "spec": {
            "direction": "long",
            "long_entry": [{"left": {"indicator": "RSI", "period": 14}, "op": "<", "value": 35}],
            "long_exit": [{"left": {"indicator": "RSI", "period": 14}, "op": ">", "value": 65}],
        },
        "sl_mode": "atr", "sl_value": 2.0, "tp_mode": "atr", "tp_value": 3.0,
    }
    r = client.post("/api/backtest/build", json=body).json()
    assert r["mode"] == "builder"
    assert len(r["equity"]) > 100
    assert "sharpe" in r["metrics"]
    assert isinstance(r["trades"], list)
    assert r["offline"] is True and "Synthetic" in r["data_note"]
    # exit reasons must be drawn from the honest set
    for t in r["trades"]:
        assert t["exit_reason"] in ("signal", "stop", "target", "end-of-data")
    assert "execution" in r["prov"] and r["prov"]["execution"]["caveat"]


def test_builder_rejects_bad_spec(client):
    bad = {"symbol": "NIFTY", "spec": {"direction": "long", "long_entry": []}}
    assert client.post("/api/backtest/build", json=bad).status_code == 400
    bad_ind = {"symbol": "NIFTY", "spec": {
        "direction": "long",
        "long_entry": [{"left": {"indicator": "WAT"}, "op": "<", "value": 1}]}}
    assert client.post("/api/backtest/build", json=bad_ind).status_code == 400
    assert client.post("/api/backtest/build",
                       json={"symbol": "NIFTY", "interval": "7y", "spec": {
                           "direction": "long",
                           "long_entry": [{"left": {"indicator": "RSI"}, "op": "<", "value": 30}]}
                       }).status_code == 400


def test_screen(client):
    r = client.get("/api/screen?universe=mega&rules=rsi>0").json()
    assert r["universe_size"] == 10
    assert len(r["rows"]) >= 1
    assert client.get("/api/screen?universe=narnia").status_code == 400


def test_watchlist_roundtrip(client):
    r = client.post("/api/watchlist", json={"symbols": ["AAA", "BBB"]}).json()
    assert set(r["symbols"]) == {"AAA", "BBB"}
    assert set(client.get("/api/watchlist").json()["symbols"]) == {"AAA", "BBB"}


def test_portfolio_trade_flow(client):
    before = client.get("/api/portfolio").json()
    r = client.post("/api/portfolio/trade",
                    json={"side": "buy", "symbol": "CCC", "quantity": 5, "price": 100.0})
    assert r.status_code == 200
    mid = client.get("/api/portfolio").json()
    assert mid["cash"] == pytest.approx(before["cash"] - 500.0)
    r2 = client.post("/api/portfolio/trade",
                     json={"side": "sell", "symbol": "CCC", "quantity": 5, "price": 110.0}).json()
    assert r2["realized"] == pytest.approx(50.0)


def test_portfolio_bad_trade_400(client):
    """Nonsense input is still rejected — but selling what you don't hold is
    no longer nonsense, it is opening a short."""
    for body in ({"side": "sideways", "symbol": "GHOST", "quantity": 1, "price": 1.0},
                 {"side": "sell", "symbol": "GHOST", "quantity": 0, "price": 1.0},
                 {"side": "sell", "symbol": "GHOST", "quantity": -5, "price": 1.0}):
        assert client.post("/api/portfolio/trade", json=body).status_code == 400


def test_portfolio_can_sell_to_open(client):
    r = client.post("/api/portfolio/trade",
                    json={"side": "sell", "symbol": "SHORTY", "quantity": 5, "price": 100.0})
    assert r.status_code == 200
    pos = next(p for p in client.get("/api/portfolio").json()["positions"]
               if p["symbol"] == "NSE:SHORTY")
    assert pos["quantity"] == pytest.approx(-5)
    assert pos["is_short"] is True
    assert pos["market_value"] < 0  # a short is a liability, not an asset


def test_alert_lifecycle(client):
    n0 = len(client.get("/api/alerts").json()["alerts"])
    r = client.post("/api/alerts", json={"rule": "ZZZ > 999999"}).json()
    assert "ZZZ" in r["text"]
    alerts = client.get("/api/alerts").json()["alerts"]
    assert len(alerts) == n0 + 1
    idx = alerts[-1]["index"]
    assert client.delete(f"/api/alerts/{idx}").status_code == 200
    assert len(client.get("/api/alerts").json()["alerts"]) == n0
    assert client.post("/api/alerts", json={"rule": "gibberish"}).status_code == 400


def test_chain_provenance_present(client):
    r = client.get("/api/chain/NIFTY").json()
    pv = r["prov"]
    for key in ("pcr_oi", "max_pain", "expected_move_pct", "atm_iv", "bias", "delta_oi"):
        assert key in pv, f"missing prov for {key}"
        assert pv[key]["formula"]
        assert pv[key]["source"]
        assert isinstance(pv[key]["inputs"], list)
    # A modelled chain must never produce a ΔOI number. The store refuses to
    # hold one, and the endpoint now refuses to difference one against a real
    # stored snapshot — which would have worn the "prev session close" label.
    assert r["is_model"] is True
    assert r["delta_oi_basis"] == "model chain — ΔOI not computed"
    assert all(row["call"]["oi_change"] is None for row in r["rows"])
    assert all(row["put"]["oi_change"] is None for row in r["rows"])


def test_iv_rank_local_honest_when_empty(client):
    r = client.get("/api/iv/NIFTY").json()
    rank = r["iv_rank_local"]
    assert rank["available"] is False  # offline test store has no real captures
    assert "rank" not in rank
    assert rank["days_required"] >= 20
    assert "iv_rank_local" in r["prov"]


def test_payoff_pop_provenance(client):
    r = client.get("/api/payoff/NIFTY?strategy=iron_condor").json()
    assert "pop" in r["prov"]
    assert "lognormal" in r["prov"]["pop"]["formula"]
    assert r["prov"]["pop"]["caveat"]  # POP must declare itself a model


def test_news_provenance(client):
    resp = client.get("/api/news?limit=5")
    if resp.status_code != 200:
        pytest.skip("news feed unavailable in this environment")
    body = resp.json()
    if body["items"]:
        item = body["items"][0]
        assert "sentiment" in item["prov"] and "confidence" in item["prov"]
        assert "lexicon" in item["prov"]["sentiment"]["formula"]
    assert "prov" in body["bias"]


def test_layout_roundtrip(client):
    fresh = client.get("/api/layout").json()
    assert "widgets" in fresh  # None on first run -> frontend default
    layout = {"widgets": [{"type": "chart", "x": 0, "y": 0, "w": 5, "h": 5,
                           "config": {"symbol": "NIFTY", "channel": "A"}}],
              "channels": {"A": "NIFTY", "B": "BANKNIFTY", "C": "RELIANCE"}}
    assert client.post("/api/layout", json=layout).json()["ok"]
    back = client.get("/api/layout").json()
    assert back["widgets"][0]["config"]["symbol"] == "NIFTY"
    assert back["channels"]["B"] == "BANKNIFTY"


def test_store_stats_endpoint(client):
    r = client.get("/api/store/stats").json()
    assert "size_bytes" in r and "chains" in r and "bars" in r


def test_store_bars_endpoint_empty_is_honest(client):
    r = client.get("/api/store/bars/NIFTY").json()
    assert r["bars"] == []
    assert "no locally captured" in r["note"]


def test_websocket_ticks(client):
    with client.websocket_connect("/ws/ticks") as ws:
        hello = ws.receive_json()
        assert hello["type"] == "hello"
        assert hello["live"] is False  # offline -> synthetic feed
        msg = ws.receive_json()
        assert msg["type"] == "ticks"
        assert len(msg["data"]) >= 1
        tick = msg["data"][0]
        assert tick["ltp"] > 0 and "symbol" in tick


def test_model_chain_reports_no_unusual_activity(client):
    """synthetic.py deliberately plants two hot strikes so the detector has
    something to find. Asserting the detector DOES find them, and that the
    endpoint still reports none, is what proves the gate is doing the work."""
    from shunkan.derivatives.chain import analyze_chain
    from shunkan.derivatives.synthetic import synthetic_chain

    assert analyze_chain(synthetic_chain("NIFTY")).unusual  # detectable...

    r = client.get("/api/chain/NIFTY").json()
    assert r["is_model"] is True
    assert r["analytics"]["unusual"] == []  # ...but never reported as real


def test_chain_failure_returns_structured_trail(client, monkeypatch):
    from shunkan.data.chains import ChainUnavailable

    trail = ["Kite: no broker connected", "NSE: blocked"]
    monkeypatch.setattr(
        "shunkan.data.chains._resolve",
        lambda *a, **k: ChainUnavailable("NIFTY", trail),
    )
    r = client.get("/api/chain/NIFTY")
    assert r.status_code == 502
    detail = r.json()["detail"]
    assert detail["symbol"] == "NIFTY"
    assert detail["source_trail"] == trail
    assert "No live option chain" in detail["error"]


def test_chain_strike_window_bounds_the_table_not_the_analytics(client):
    """NSE lists 100+ strikes and the browser draws every one. The window
    trims the table; max_pain and PCR must still see the whole chain."""
    full = client.get("/api/chain/NIFTY?strikes=0").json()
    narrow = client.get("/api/chain/NIFTY?strikes=3").json()

    assert len(narrow["rows"]) <= 7 < len(full["rows"])
    assert narrow["rows_total"] == full["rows_total"] == len(full["rows"])
    assert narrow["strike_window"] == 3
    # analytics ran on the full chain either way
    assert narrow["analytics"]["max_pain"] == full["analytics"]["max_pain"]
    assert narrow["analytics"]["pcr_oi"] == full["analytics"]["pcr_oi"]
    assert sum(1 for r in narrow["rows"] if r["atm"]) == 1  # ATM stays in view


def test_chain_exposes_an_expiry_ladder(client):
    r = client.get("/api/chain/NIFTY").json()
    assert r["expiries"], "the UI cannot offer a selector without a ladder"
    assert r["expiry"] in r["expiries"]


def test_chain_rejects_an_unparseable_expiry(client):
    r = client.get("/api/chain/NIFTY?expiry=next-tuesday")
    assert r.status_code == 400
    assert "YYYY-MM-DD" in r.json()["detail"]


def test_trade_books_an_option_leg_off_the_chain(client):
    """The idea -> position path: a chain row carries everything a leg needs."""
    chain = client.get("/api/chain/NIFTY").json()
    row = next(r for r in chain["rows"] if r["atm"])
    body = {"side": "sell", "symbol": "NIFTY", "kind": "CE",
            "expiry": chain["expiry"], "strike": row["strike"],
            "lot_size": 65, "lots": 2, "price": row["call"]["ltp"]}
    r = client.post("/api/portfolio/trade", json=body).json()
    assert r["quantity"] == 130                       # 2 lots x 65
    assert r["instrument"].startswith("NFO:NIFTY|")
    assert "CE" in r["label"]

    pos = next(p for p in client.get("/api/portfolio").json()["positions"]
               if p["symbol"] == r["instrument"])
    assert pos["is_short"] is True
    assert pos["quantity"] == -130
    assert pos["kind"] == "CE" and pos["lot_size"] == 65


def test_trade_in_lots_refuses_without_a_lot_size(client):
    chain = client.get("/api/chain/NIFTY").json()
    row = next(r for r in chain["rows"] if r["atm"])
    r = client.post("/api/portfolio/trade", json={
        "side": "sell", "symbol": "NIFTY", "kind": "PE", "expiry": chain["expiry"],
        "strike": row["strike"], "lots": 2, "price": row["put"]["ltp"]})
    assert r.status_code == 400
    assert "lot size" in r.json()["detail"].lower()


def test_trade_rejects_an_impossible_contract(client):
    r = client.post("/api/portfolio/trade", json={
        "side": "buy", "symbol": "RELIANCE", "kind": "EQ",
        "exchange": "MCX", "quantity": 1, "price": 100.0})
    assert r.status_code == 400
    assert "MCX" in r.json()["detail"]


def test_portfolio_reports_net_book_greeks(client):
    """A desk reads net delta/gamma/theta/vega, not a list of legs."""
    chain = client.get("/api/chain/NIFTY").json()
    row = next(r for r in chain["rows"] if r["atm"])
    for right, px in (("CE", row["call"]["ltp"]), ("PE", row["put"]["ltp"])):
        r = client.post("/api/portfolio/trade", json={
            "side": "sell", "symbol": "NIFTY", "kind": right,
            "expiry": chain["expiry"], "strike": row["strike"],
            "quantity": 65, "price": px})
        assert r.status_code == 200, r.text

    risk = client.get("/api/portfolio").json()["risk"]
    assert risk["complete"] is True
    assert risk["net"]["gamma"] < 0        # short straddle is short convexity
    assert risk["net"]["theta"] > 0        # and collects decay
    assert "NIFTY" in risk["by_underlying"]
    assert "gamma" in risk["summary"]

    # leave the book flat for other tests
    for right, px in (("CE", row["call"]["ltp"]), ("PE", row["put"]["ltp"])):
        client.post("/api/portfolio/trade", json={
            "side": "buy", "symbol": "NIFTY", "kind": right,
            "expiry": chain["expiry"], "strike": row["strike"],
            "quantity": 65, "price": px})
