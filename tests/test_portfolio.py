import pytest

from shunkan.portfolio import Portfolio


@pytest.fixture
def pf(tmp_path):
    return Portfolio(cash=10_000.0, path=tmp_path / "pf.json")


def test_buy_reduces_cash_and_opens_position(pf):
    pf.buy("AAPL", 10, 100.0)
    assert pf.cash == pytest.approx(9_000.0)
    assert pf.positions["NSE:AAPL"].quantity == 10
    assert pf.positions["NSE:AAPL"].avg_cost == pytest.approx(100.0)


def test_buy_insufficient_cash_raises(pf):
    with pytest.raises(ValueError, match="Insufficient cash"):
        pf.buy("AAPL", 1000, 100.0)


def test_average_cost_across_lots(pf):
    pf.buy("AAPL", 10, 100.0)
    pf.buy("AAPL", 10, 200.0)
    assert pf.positions["NSE:AAPL"].avg_cost == pytest.approx(150.0)


def test_sell_fifo_realizes_pnl(pf):
    pf.buy("AAPL", 10, 100.0)
    pf.buy("AAPL", 10, 200.0)
    realized = pf.sell("AAPL", 15, 250.0)
    # FIFO: 10 @ 100 -> +1500, 5 @ 200 -> +250
    assert realized == pytest.approx(1_750.0)
    assert pf.positions["NSE:AAPL"].quantity == pytest.approx(5)
    assert pf.realized_pnl == pytest.approx(1_750.0)


def test_selling_more_than_held_closes_then_goes_short(pf):
    """The old book raised here. Refusing to go short is precisely what made
    a credit spread or short straddle unrepresentable."""
    pf.buy("AAPL", 5, 100.0)
    realized = pf.sell("AAPL", 10, 120.0)
    assert realized == pytest.approx((120.0 - 100.0) * 5)  # the long's profit
    assert pf.positions["NSE:AAPL"].quantity == pytest.approx(-5)
    assert pf.positions["NSE:AAPL"].is_short


def test_sell_entire_position_removes_it(pf):
    pf.buy("AAPL", 5, 100.0)
    pf.sell("AAPL", 5, 110.0)
    assert "NSE:AAPL" not in pf.positions


def test_valuation(pf):
    pf.buy("AAPL", 10, 100.0)
    prices = {"NSE:AAPL": 120.0}
    assert pf.market_value(prices) == pytest.approx(1_200.0)
    assert pf.unrealized_pnl(prices) == pytest.approx(200.0)
    assert pf.total_equity(prices) == pytest.approx(9_000.0 + 1_200.0)


def test_save_load_roundtrip(pf, tmp_path):
    pf.buy("AAPL", 10, 100.0)
    pf.sell("AAPL", 5, 150.0)
    pf.save()
    loaded = Portfolio.load(tmp_path / "pf.json")
    assert loaded.cash == pytest.approx(pf.cash)
    assert loaded.realized_pnl == pytest.approx(250.0)
    assert loaded.positions["NSE:AAPL"].quantity == pytest.approx(5)
    assert len(loaded.history) == 2


def test_load_corrupted_file_starts_fresh(tmp_path):
    path = tmp_path / "pf.json"
    path.write_text("{not json")
    pf = Portfolio.load(path)
    assert pf.cash == pytest.approx(100_000.0)
    assert pf.positions == {}


# -- margin: reported, never estimated ----------------------------------------


def test_margin_is_unknown_until_the_exchange_prices_it(pf):
    from shunkan.portfolio import Instrument
    from datetime import date

    assert pf.margin_used() == 0.0  # flat book genuinely costs nothing
    ce = Instrument.option("NIFTY", date(2026, 8, 18), 24500.0, "CE", lot_size=65)
    pf.trade(ce, "SELL", 65, 139.70)
    assert pf.margin_used() is None  # a short with no priced margin is UNKNOWN
    assert pf.margin is None         # and no estimate was invented


def test_margin_goes_unknown_again_when_the_book_changes(pf):
    from shunkan.portfolio import Instrument
    from datetime import date

    ce = Instrument.option("NIFTY", date(2026, 8, 18), 24500.0, "CE", lot_size=65)
    pe = Instrument.option("NIFTY", date(2026, 8, 18), 24500.0, "PE", lot_size=65)
    pf.trade(ce, "SELL", 65, 139.70)
    pf.margin = {"final": {"total": 183_621.62}, "unpriceable": [],
                 "book": pf._book_fingerprint()}
    assert pf.margin_used() == pytest.approx(183_621.62)

    pf.trade(pe, "SELL", 65, 117.65)   # new leg; the old price no longer applies
    assert pf.margin_used() is None


def test_margin_goes_unknown_when_only_the_SIZE_changes(pf):
    """Halving a leg leaves the position keys identical but changes margin
    entirely — a key-only staleness check kept reporting the old number."""
    from shunkan.portfolio import Instrument
    from datetime import date

    ce = Instrument.option("NIFTY", date(2026, 8, 18), 24500.0, "CE", lot_size=65)
    pf.trade(ce, "SELL", 130, 139.70)
    pf.margin = {"final": {"total": 401_167.27}, "unpriceable": [],
                 "book": pf._book_fingerprint()}
    assert pf.margin_used() == pytest.approx(401_167.27)

    pf.trade(ce, "BUY", 65, 139.70)    # same key, half the size
    assert sorted(pf.positions) == [ce.key]   # composition unchanged...
    assert pf.margin_used() is None           # ...but the price is stale


def test_margin_is_unknown_when_any_leg_could_not_be_priced(pf):
    from shunkan.portfolio import Instrument
    from datetime import date

    ce = Instrument.option("NIFTY", date(2026, 8, 18), 24500.0, "CE", lot_size=65)
    pf.trade(ce, "SELL", 65, 139.70)
    pf.margin = {"final": {"total": 100.0}, "unpriceable": ["NIFTY 18AUG26 99999 CE"],
                 "book": pf._book_fingerprint()}
    # a total that silently omits a leg reads as complete and understates risk
    assert pf.margin_used() is None


def test_trade_lots_refuses_without_a_known_lot_size(pf):
    from shunkan.portfolio import Instrument
    from datetime import date

    unknown = Instrument.option("NIFTY", date(2026, 8, 18), 24500.0, "CE")
    with pytest.raises(ValueError, match="No lot size"):
        pf.trade_lots(unknown, "SELL", 2, 139.70)
