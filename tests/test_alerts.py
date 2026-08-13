import pytest

from shunkan.alerts import Alert, AlertBook, parse_alert
from shunkan.data.provider import SyntheticProvider


def test_parse_price_alert():
    a = parse_alert("NIFTY > 23500")
    assert (a.symbol, a.metric, a.op, a.value) == ("NIFTY", "price", ">", 23500.0)


def test_parse_rsi_alert():
    a = parse_alert("RELIANCE rsi < 30")
    assert (a.symbol, a.metric, a.op, a.value) == ("RELIANCE", "rsi", "<", 30.0)


def test_parse_vol_surge_alert():
    a = parse_alert("SBIN vol_surge >= 2")
    assert (a.symbol, a.metric, a.op) == ("SBIN", "vol_surge", ">=")


def test_parse_garbage_raises():
    with pytest.raises(ValueError):
        parse_alert("tell me when nifty moons")


def test_alert_check_operators():
    assert Alert("X", "price", ">", 100).check(101)
    assert not Alert("X", "price", ">", 100).check(100)
    assert Alert("X", "price", "<=", 100).check(100)


def test_book_persistence_roundtrip(tmp_path):
    book = AlertBook(path=tmp_path / "alerts.json")
    book.add(parse_alert("NIFTY > 1"))
    book.add(parse_alert("RELIANCE rsi < 99"))
    reloaded = AlertBook(path=tmp_path / "alerts.json")
    assert len(reloaded.alerts) == 2
    assert reloaded.alerts[0].symbol == "NIFTY"


def test_book_remove(tmp_path):
    book = AlertBook(path=tmp_path / "alerts.json")
    book.add(parse_alert("NIFTY > 1"))
    gone = book.remove(0)
    assert gone.symbol == "NIFTY"
    assert book.alerts == []
    with pytest.raises(ValueError):
        book.remove(5)


def test_check_all_fires_and_disarms(tmp_path):
    book = AlertBook(path=tmp_path / "alerts.json")
    # Synthetic prices are deterministic and > 0, so this always fires…
    book.add(parse_alert("AAA > 0.01"))
    # …and this never fires.
    book.add(parse_alert("AAA > 99999999"))
    fired = book.check_all(SyntheticProvider())
    assert len(fired) == 1
    alert, value = fired[0]
    assert not alert.armed
    assert value > 0.01
    # Second pass: nothing armed that can fire.
    assert book.check_all(SyntheticProvider()) == []
    # Persistence captured the fired state.
    reloaded = AlertBook(path=tmp_path / "alerts.json")
    assert not reloaded.alerts[0].armed
    assert reloaded.alerts[1].armed


def test_check_all_rsi_metric(tmp_path):
    book = AlertBook(path=tmp_path / "alerts.json")
    book.add(parse_alert("BBB rsi <= 100"))  # RSI is always <= 100
    fired = book.check_all(SyntheticProvider())
    assert len(fired) == 1
    assert 0 <= fired[0][1] <= 100
