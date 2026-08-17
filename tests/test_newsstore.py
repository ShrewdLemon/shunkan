"""News archive: mapping traps, dedup, and the honesty of the backfill channel."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from shunkan.data.constituents import (
    Constituent,
    alias_table,
    map_title,
    parse_constituents_csv,
)
from shunkan.data.newsstore import _key, news_for, persist, store_file
from shunkan.intel.feeds import NewsItem


CONSTITUENTS = [
    Constituent("M&M", "Mahindra & Mahindra Ltd.", ("NIFTY50",)),
    Constituent("TECHM", "Tech Mahindra Ltd.", ("NIFTY50",)),
    Constituent("KOTAKBANK", "Kotak Mahindra Bank Ltd.", ("BANKNIFTY",)),
    Constituent("RELIANCE", "Reliance Industries Ltd.", ("NIFTY50",)),
    Constituent("SBIN", "State Bank of India", ("NIFTY50", "BANKNIFTY")),
    Constituent("LT", "Larsen & Toubro Ltd.", ("NIFTY50",)),
]
ALIASES = alias_table(CONSTITUENTS)


def item(title, when="2026-03-05T08:00:00+00:00"):
    return NewsItem(title=title, link="https://news.google.com/x?y=" + title[:8],
                    source="Test", published=datetime.fromisoformat(when))


# -- the mapping traps, each one a real Indian-market collision ---------------


def test_kotak_mahindra_bank_is_not_mahindra_and_mahindra():
    hits = map_title("Kotak Mahindra Bank Q1 profit rises 12%", ALIASES)
    assert hits == ["KOTAKBANK"]


def test_tech_mahindra_is_not_m_and_m():
    assert map_title("Tech Mahindra wins large BFSI deal", ALIASES) == ["TECHM"]
    assert map_title("M&M tractor sales up 14% in July", ALIASES) == ["M&M"]


def test_sbi_maps_through_the_press_abbreviation():
    assert map_title("SBI raises MCLR by 10 bps", ALIASES) == ["SBIN"]


def test_lt_the_symbol_never_matches_but_l_and_t_does():
    """'LT' as a bare token is an abbreviation for nothing a journalist
    writes; the press writes L&T."""
    assert map_title("L&T bags Middle East EPC order", ALIASES) == ["LT"]
    assert map_title("ALTERNATE energy stocks rally", ALIASES) == []


def test_a_headline_can_name_two_companies():
    hits = map_title("Reliance Industries and SBI sign payments JV", ALIASES)
    assert set(hits) == {"RELIANCE", "SBIN"}


def test_query_noise_is_not_tagged():
    """The Women's Day listicle that arrived in a real Reliance query: the
    query said Reliance, the title does not, so it carries no symbol."""
    assert map_title("Happy International Women's Day! #IWD2026", ALIASES) == []


# -- the archive itself -------------------------------------------------------


def test_persist_is_idempotent_across_refetches(tmp_path):
    items = [item("Reliance Industries shares gain 2% on US waiver")]
    assert persist(items, "backfill", ALIASES, root=tmp_path, query_symbol="RELIANCE") == 1
    # same article, different Google redirect token on the second fetch
    again = [item("Reliance Industries shares gain 2% on US waiver")]
    again[0].link = "https://news.google.com/DIFFERENT_TOKEN"
    assert persist(again, "live", ALIASES, root=tmp_path) == 0


def test_dedup_key_is_title_and_date_not_url():
    a = item("Some headline")
    b = item("Some headline")
    b.link = "https://news.google.com/other"
    assert _key(a.title, a.published) == _key(b.title, b.published)


def test_title_mapping_wins_over_query_attribution(tmp_path):
    """A Reliance query returning an SBI headline stores it tagged SBIN, with
    the query recorded separately so the channel stays auditable."""
    persist([item("SBI cuts deposit rates")], "backfill", ALIASES,
            root=tmp_path, query_symbol="RELIANCE")
    import pandas as pd

    df = pd.read_parquet(store_file(tmp_path))
    assert df.iloc[0]["symbols"] == "SBIN"
    assert df.iloc[0]["query_symbol"] == "RELIANCE"
    assert df.iloc[0]["origin"] == "backfill"


def test_news_for_filters_by_exact_symbol_token(tmp_path):
    persist([item("Reliance Industries expands retail"),
             item("SBI Life premium growth steady")], "live", ALIASES, root=tmp_path)
    rel = news_for("RELIANCE", root=tmp_path)
    assert len(rel) == 1
    assert "Reliance" in rel.iloc[0]["title"]


# -- constituents parser ------------------------------------------------------


def test_constituents_parser_and_format_refusal():
    csv = ("Company Name,Industry,Symbol,Series,ISIN Code\n"
           "Reliance Industries Ltd.,Oil & Gas,RELIANCE,EQ,INE002A01018\n")
    got = parse_constituents_csv(csv, "NIFTY50")
    assert got[0].symbol == "RELIANCE"
    from shunkan.data.provider import DataError
    with pytest.raises(DataError):
        parse_constituents_csv("a,b\n1,2\n", "NIFTY50")
