"""The knowledge graph: identity, sourcing, traversal, and the cache."""

import pytest

from shunkan.store.graph import GraphStore, normalise


@pytest.fixture
def g(tmp_path):
    return GraphStore(tmp_path / "t.db")


def test_normalise_strips_form_but_keeps_identity():
    # legal form and appended qualifiers are noise
    assert normalise("Reliance Industries Limited") == normalise("RELIANCE INDUSTRIES LTD")
    assert normalise("SBI Mutual Fund - Through its various schemes") == "SBI MUTUAL FUND"
    assert normalise("LIFE INSURANCE CORPORATION OF INDIA P") == \
        normalise("Life Insurance Corporation of India")
    # ...but words that DISCRIMINATE must survive, or entities merge
    assert normalise("SBI Mutual Fund") != normalise("State Bank of India")


def test_alias_resolves_every_spelling_to_one_node(g):
    nid = g.put_node("holder", "LIC", "Life Insurance Corporation of India")
    for spelling in ("LIFE INSURANCE CORPORATION OF INDIA P",
                     "Life Insurance Corporation of India",
                     "life insurance corporation of india"):
        g.put_alias(spelling, nid)
    g.commit()
    assert {g.resolve(s) for s in
            ("LIFE INSURANCE CORPORATION OF INDIA P",
             "Life  Insurance Corporation Of India")} == {nid}


def test_an_edge_without_a_source_is_refused(g):
    a = g.put_node("company", "AAA", "A Ltd")
    b = g.put_node("holder", "H", "Holder")
    with pytest.raises(ValueError, match="source"):
        g.put_edges([{"src": b, "dst": a, "rel": "holds", "weight": 1.0}])


def test_neighbours_carry_direction_weight_and_source(g):
    a = g.put_node("company", "AAA", "A Ltd")
    b = g.put_node("holder", "H", "Holder")
    g.put_edges([{"src": b, "dst": a, "rel": "holds", "weight": 5.5,
                  "unit": "pct", "as_of": "30-JUN-2026", "source": "XBRL"}])
    g.commit()
    inbound = g.neighbours(a)
    assert len(inbound) == 1
    n = inbound[0]
    assert (n.direction, n.weight, n.source, n.rel) == ("in", 5.5, "XBRL", "holds")
    assert g.neighbours(b)[0].direction == "out"


def test_co_held_finds_the_second_hop(g):
    x = g.put_node("company", "X", "X Ltd")
    y = g.put_node("company", "Y", "Y Ltd")
    s1 = g.put_node("scheme", "S1", "Scheme One")
    s2 = g.put_node("scheme", "S2", "Scheme Two")
    g.put_edges([{"src": s, "dst": c, "rel": "scheme_holds", "weight": 1.0,
                  "source": "portfolio"} for s in (s1, s2) for c in (x, y)])
    g.commit()
    co = g.co_held(x)
    assert co and co[0]["id"] == y and co[0]["shared"] == 2


def test_cache_expires_on_its_own_ttl(g):
    g.cache_put("k", {"v": 1}, "test", ttl_s=0.0)
    assert g.cache_get("k") is None          # already stale
    g.cache_put("k2", {"v": 2}, "test", ttl_s=3600)
    assert g.cache_get("k2") == {"v": 2}
    assert g.cache_get("k2", max_age_s=0) is None   # caller can be stricter


def test_stats_report_the_shape(g):
    g.put_node("company", "AAA", "A Ltd")
    g.put_node("holder", "H", "Holder")
    g.commit()
    st = g.stats()
    assert st["nodes"] == 2 and st["by_kind"]["company"] == 1
