import inspect

import pandas as pd

from shunkan import demo

OK = {"fetched": 2, "unpublished": 0, "failed": 0, "days_on_disk": 2}


class P:
    def history(self, symbol, period, interval):
        if symbol == "BROKEN":
            raise RuntimeError("no route")
        return pd.DataFrame({"close": [1.0, 2.0]}, index=pd.to_datetime(["2026-01-01", "2026-01-02"]))


class A:
    def __init__(self):
        self.rows = {}

    def upsert(self, symbol, df, source):
        self.rows[symbol] = len(df)
        return len(df)


def quiet(*a):
    return None


def test_demo_fills_history_and_participants(monkeypatch):
    monkeypatch.setattr(demo, "DEMO_SYMBOLS", ("^NSEI", "BROKEN"))
    archive = A()
    res = demo.run(days=5, provider=P(), archive=archive,
                   backfill=lambda days, progress=None: {**OK, "days": days}, out=quiet)
    assert res["history"] == {"^NSEI": 2}
    assert res["participant"] == {**OK, "days": 5}
    assert res["failed"] == {"BROKEN": "RuntimeError: no route"}


def test_demo_writes_where_the_daily_analysis_reads(tmp_path):
    from shunkan.markets import INDEX_ALIASES
    from shunkan.store import HistoryArchive

    assert demo.DEMO_SYMBOLS == tuple(INDEX_ALIASES[k] for k in ("NIFTY", "BANKNIFTY", "INDIAVIX"))
    res = demo.run(days=1, provider=P(), archive=HistoryArchive(root=tmp_path),
                   backfill=lambda days, progress=None: OK, out=quiet)
    assert res["failed"] == {}
    assert sorted(p.stem for p in (tmp_path / "history").glob("*.parquet")) == [
        "_INDIAVIX", "_NSEBANK", "_NSEI"]


def test_demo_counts_failed_participant_days():
    res = demo.run(days=5, provider=P(), archive=A(),
                   backfill=lambda days, progress=None: {**OK, "failed": 2}, out=quiet)
    assert res["failed"] == {"participant": "2 day(s) failed to fetch"}


def test_demo_needs_two_participant_days():
    res = demo.run(days=5, provider=P(), archive=A(),
                   backfill=lambda days, progress=None: {**OK, "fetched": 1, "unpublished": 2,
                                                         "days_on_disk": 1},
                   out=quiet)
    assert res["failed"] == {"participant": "only 1 participant days on disk"}


def test_demo_ends_on_next_only_when_everything_landed(monkeypatch):
    lines = []
    demo.run(days=5, provider=P(), archive=A(), backfill=lambda days, progress=None: OK,
             out=lines.append)
    assert lines[-1].startswith("Next:")

    monkeypatch.setattr(demo, "DEMO_SYMBOLS", ("^NSEI", "BROKEN"))
    lines.clear()
    demo.run(days=5, provider=P(), archive=A(),
             backfill=lambda days, progress=None: {**OK, "days_on_disk": 0}, out=lines.append)
    assert not any(ln.startswith("Next") for ln in lines)
    assert lines[-1] == "demo incomplete: BROKEN, participant failed"


def test_demo_refuses_offline_mode_and_writes_nothing(monkeypatch):
    monkeypatch.setenv("SHUNKAN_OFFLINE", "1")
    calls = []
    archive = A()
    res = demo.run(days=5, archive=archive, backfill=lambda days, progress=None: calls.append(days),
                   out=quiet)
    assert set(res["failed"]) == {"offline"}
    assert res["history"] == {} and archive.rows == {} and calls == []


def test_default_days_is_90_in_run_and_cli(monkeypatch):
    from shunkan import cli

    assert inspect.signature(demo.run).parameters["days"].default == 90
    seen = []
    monkeypatch.setattr(demo, "run", lambda days: seen.append(days) or {"failed": {}})
    assert cli.main(["demo"]) == 0
    assert seen == [90]


def test_cli_demo_exit_code(monkeypatch):
    from shunkan import cli

    seen = []

    def fake(days):
        seen.append(days)
        return {"history": {}, "participant": None, "failed": {} if days == 3 else {"x": "y"}}

    monkeypatch.setattr(demo, "run", fake)
    assert cli.main(["demo", "--days", "3"]) == 0
    assert cli.main(["demo", "--days", "4"]) == 1
    assert seen == [3, 4]


def test_cli_exits_1_when_participant_days_short(monkeypatch):
    import shunkan.data.participant as participant
    import shunkan.data.provider as provider
    import shunkan.store as store
    from shunkan import cli

    monkeypatch.setenv("SHUNKAN_OFFLINE", "0")
    monkeypatch.setattr(provider, "get_provider", lambda: P())
    monkeypatch.setattr(store, "HistoryArchive", A)
    monkeypatch.setattr(participant, "backfill",
                        lambda days, progress=None: {**OK, "fetched": 0, "unpublished": 3,
                                                     "days_on_disk": 0})
    assert cli.main(["demo", "--days", "5"]) == 1
