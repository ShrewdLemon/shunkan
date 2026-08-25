"""The validation gate, which is the only thing standing between a confident
model and a fabricated node in the knowledge graph.

Every case here is taken from a real 2026-08-25 run against the Balrampur
Chini Mills FY2026 annual report, not invented for the test.
"""

from __future__ import annotations

import pytest

from shunkan.data.llm import (LLMSettings, Extraction, _locate, _norm,
                              validate_against_source)

# The actual sentence in the filed report.
REAL = ("Revenue from sale of sugar and its by-products is recognised at a "
        "point in time, upon dispatch or delivery, as applicable, when the "
        "performance obligation is satisfied.")

# What the model actually returned. It INSERTED the parenthetical to
# manufacture evidence for three nodes it wanted to create. The nodes may even
# be factually true; the citation is not, and that is what the gate is for.
FABRICATED = ("Revenue from sale of sugar and its by-products (such as "
              "molasses, bagasse and pressmud) is recognised at a point in "
              "time, upon dispatch or delivery, as applicable")

DOC = "Some preamble.\n" + REAL + "\nMore text about the distillery segment."


def test_norm_collapses_pdf_line_breaks():
    """A PDF breaks sentences at the line box; the model reflows them."""
    assert _norm("Ethanol sold to Oil\nRefineries") == "ethanol sold to oil refineries"


def test_norm_strips_page_furniture():
    """A sentence crossing a page break has the running head spliced into it.
    Six TRUE outputs were dropped before this was handled."""
    spliced = ("manufacturing and sale of ethanol, ethyl neutral alcohol\n"
               "Integrated Annual Report 2025-26 | 89\nand agricultural fertilizers")
    assert "integrated annual report" not in _norm(spliced)


def test_exact_quote_is_accepted():
    assert _locate(REAL, _norm(DOC)) == "exact"


def test_fabricated_quote_is_rejected():
    """The insertion diverges early, so no long prefix matches either."""
    assert _locate(FABRICATED, _norm(DOC)) is None


def test_long_prefix_survives_a_mangled_tail():
    """Real quote, tail corrupted by page furniture the regex did not catch.
    Accepting on a long verbatim prefix keeps the true node."""
    quote = REAL[:120] + " 88 | Balrampur Chini Mills Limited xyzzy"
    assert _locate(quote, _norm(DOC)) == "prefix"


def test_short_quote_cannot_be_verified():
    assert _locate("sugar", _norm(DOC)) is None


def test_gate_keeps_real_and_drops_fabricated():
    payload = {
        "outputs": [
            {"name": "Sugar", "quote": REAL},
            {"name": "Molasses", "quote": FABRICATED},
        ],
        "customers": [],
        "inputs": [],
        "facilities": [],
    }
    kept, dropped = validate_against_source(payload, DOC)
    assert [i["name"] for i in kept["outputs"]] == ["Sugar"]
    assert len(dropped) == 1
    assert dropped[0]["name"] == "Molasses"
    assert "not found" in dropped[0]["reason"]
    # the rejected claim is retained verbatim so the UI can show what was tried
    assert "molasses, bagasse and pressmud" in dropped[0]["quote"]


def test_gate_drops_a_node_with_no_quote_at_all():
    payload = {"inputs": [{"name": "Sugarcane"}], "outputs": [],
               "customers": [], "facilities": []}
    kept, dropped = validate_against_source(payload, DOC)
    assert kept["inputs"] == []
    assert dropped[0]["reason"] == "no quote supplied"


def test_gate_dedupes_variant_spellings():
    payload = {"customers": [{"name": "Sugar", "quote": REAL},
                             {"name": "  sugar ", "quote": REAL}],
               "inputs": [], "outputs": [], "facilities": []}
    kept, _ = validate_against_source(payload, DOC)
    assert len(kept["customers"]) == 1


def test_settings_reject_a_max_tokens_that_reasoning_would_eat():
    """16000 on a 254k-token report returned an empty completion: the whole
    budget went on reasoning tokens. The floor exists so that cannot recur."""
    assert LLMSettings(max_tokens=4000).validate()
    assert LLMSettings(max_tokens=32000).validate() == []


def test_settings_reject_unknown_effort():
    assert LLMSettings(effort="turbo").validate()


def test_extraction_counts():
    ex = Extraction("BALRAMCHIN", customers=[{"name": "IFFCO", "quote": "x"}])
    assert ex.counts() == {"inputs": 0, "outputs": 0, "customers": 1, "facilities": 0}
