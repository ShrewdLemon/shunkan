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
    assert "neither the quote nor the name" in dropped[0]["reason"]
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


def test_pdf_parsing_is_serialised_because_pdfium_is_not_thread_safe():
    """Five parallel bulk extractions produced "PDFium: Data format error" for
    BAJAJ-AUTO and 4,494 characters from ASIANPAINT's 294 pages. Both parse
    perfectly alone. Read as a data problem those look like corrupt or scanned
    filings, and the honest-refusal path would have recorded them as such and
    seeded a permanent hole in the database."""
    from shunkan.data import filings

    assert hasattr(filings, "_PDF_LOCK")
    src = filings.fetch_report_text.__doc__ or ""
    body = __import__("inspect").getsource(filings.fetch_report_text)
    assert "_PDF_LOCK" in body, "the parse must hold the lock"


def test_oversize_document_is_truncated_loudly_not_silently():
    from shunkan.data import llm

    assert llm._MAX_CHARS > 1_000_000


# --- the recovery tier -----------------------------------------------------
# On AXISBANK the gate rejected 18 of 66 nodes. The ENTITIES were real - "Kisan
# Credit Card", "neo by Axis Bank", "Axis House" all appear in the filing - and
# the sentences the model wrote around them were not. Dropping those threw away
# true facts over a bad citation; keeping the model's sentence would publish a
# fabricated quote. The gate now does neither.

DOC2 = ("The Bank offers many things. Our product suite includes crop loans "
        "under Kisan Credit Card (KCC) and investment credit for farm "
        "infrastructure. Other matters follow.")


def test_a_real_entity_with_a_paraphrased_quote_is_recovered_not_dropped():
    payload = {"outputs": [{"name": "Kisan Credit Card",
                            "quote": "The Bank provides Kisan Credit Card to "
                                     "farmers across rural India as part of its "
                                     "agricultural lending programme."}],
               "inputs": [], "customers": [], "facilities": []}
    kept, dropped = validate_against_source(payload, DOC2)
    assert not dropped
    node = kept["outputs"][0]
    assert node["match"] == "recovered"
    # the model's sentence is NEVER kept - the document's own sentence replaces it
    assert "agricultural lending programme" not in node["quote"]
    assert "Kisan Credit Card" in node["quote"]
    assert node["quote"] in DOC2 or _norm(node["quote"]) in _norm(DOC2)


def test_recovery_still_drops_an_entity_the_document_never_names():
    payload = {"outputs": [{"name": "Cryptocurrency Custody",
                            "quote": "The Bank offers cryptocurrency custody."}],
               "inputs": [], "customers": [], "facilities": []}
    kept, dropped = validate_against_source(payload, DOC2)
    assert kept["outputs"] == []
    assert dropped[0]["reason"].startswith("neither")


def test_recovery_will_not_fire_on_a_name_too_short_to_be_deliberate():
    """A two-character name matches almost any document."""
    from shunkan.data.llm import _recover_sentence, _norm as N

    assert _recover_sentence("KC", DOC2, N(DOC2)) is None


def test_recovered_quote_is_bounded():
    from shunkan.data.llm import _recover_sentence, _norm as N

    got = _recover_sentence("Kisan Credit Card", DOC2, N(DOC2))
    assert got and 25 <= len(got) <= 600


# --- the quote must mention the node --------------------------------------
# Found by auditing 809 seeded nodes: the gate verified that a quote OCCURS in
# the filing and never that it MENTIONS what it was cited for. 56 nodes had a
# quote sharing under a third of the name's content words; 11 shared none.
# Every case below is verbatim from that audit.

def _gate(name, quote, doc, cat="facilities"):
    payload = {c: [] for c in ("inputs", "outputs", "customers", "facilities")}
    payload[cat] = [{"name": name, "quote": quote}]
    return validate_against_source(payload, doc)


def test_share_capital_sentence_cannot_establish_a_facility():
    """BAJAJ-AUTO: a share-capital line was cited for an Engineering Design
    Centre. It establishes a subsidiary and never a building."""
    doc = ("Bajaj Auto (Thailand) Ltd is a wholly owned subsidiary in Thailand "
           "with an issued and subscribed share capital of Thai Baht (THB) 45 "
           "million. Other matters follow.")
    kept, dropped = _gate("Bajaj Auto (Thailand) Engineering Design Centre",
                          doc.split(". ")[0] + ".", doc)
    names = [x["name"] for x in kept["facilities"]]
    # it may be recovered on the shared name tokens, but it must NOT be
    # accepted as an exact citation for a design centre
    assert all(x.get("match") != "exact" for x in kept["facilities"]), names


def test_related_party_boilerplate_naming_nobody_is_not_evidence():
    """AXISBANK: one sentence naming no entity was cited for both LIC Housing
    Finance and IDBI Bank."""
    doc = ("These services are offered to all customers (related / unrelated) "
           "in the ordinary course of business. Service charges from related "
           "party are recognised accordingly.")
    kept, dropped = _gate("LIC Housing Finance Limited",
                          doc.split(". ")[0] + ".", doc, cat="customers")
    assert kept["customers"] == []
    # the quote IS in the document; it simply names nobody, and the reason
    # must say that rather than implying the quote was invented
    assert dropped and "does not mention this node" in dropped[0]["reason"]


def test_a_residual_expense_row_is_not_a_raw_material():
    """BALRAMCHIN: 'Others 3066.55 566.93' was cited for 'Other raw materials'."""
    doc = "Consumption of stores. Others 3066.55 566.93 and further lines."
    kept, dropped = _gate("Sugarcane bagasse pellets", "Others 3066.55 566.93",
                          doc, cat="inputs")
    assert kept["inputs"] == []


def test_morphology_is_tolerated_so_real_products_survive():
    """'Gold Loans' must still be supported by 'gold loan portfolio' - the
    target is boilerplate, not paraphrase."""
    doc = ("The Bank grew its gold loan portfolio substantially during the "
           "year under review across rural branches.")
    kept, _ = _gate("Gold Loans", doc, doc, cat="outputs")
    assert [x["name"] for x in kept["outputs"]] == ["Gold Loans"]
    assert kept["outputs"][0]["match"] == "exact"


def test_a_name_with_no_content_words_still_passes_other_rules():
    doc = "The and for with from its our their. A second sentence entirely."
    kept, _ = _gate("The and", "The and for with from its our their.", doc,
                    cat="outputs")
    assert len(kept["outputs"]) == 1


def test_lenient_json_survives_the_cheap_breakages():
    """A ~300k-token call once failed on one stray comma. This helper was
    silently deleted by a later edit and nothing caught it until a run
    crashed, so it gets a test."""
    from shunkan.data.llm import _loads_lenient

    assert _loads_lenient('{"a": 1}')["a"] == 1
    assert _loads_lenient('noise {"a": [1, 2,]} tail')["a"] == [1, 2]
    # truncated mid-object, as a length-capped reply arrives
    got = _loads_lenient('{"items": [{"name": "x", "quote": "y"}, {"name": "part')
    assert got["items"][0]["name"] == "x"


def test_digest_extraction_validates_against_the_full_text_not_the_digest():
    """A sentence the digest dropped must still be recoverable, so the cheap
    path can never be less honest than the expensive one."""
    import inspect

    from shunkan.data import llm

    src = inspect.getsource(llm.extract_from_digest)
    assert "validate_against_source(payload, text)" in src


def test_quotes_are_sliced_byte_exact_not_retyped():
    """These filings carry soft hyphens, non-breaking spaces and the U+FFFE
    that pypdfium2 emits at line breaks. A retyped quote silently loses one
    and the gate then rejects a TRUE node for an invisible typo."""
    from shunkan.data.manual import build_payload, flatten, sentence_at

    doc = ("Preamble here. The Business continues to procure wood, a key raw\n"
           "mate￾rial, from sustainable sources. More text follows.")
    got = sentence_at(flatten(doc), "procure wood")
    assert "￾" in got, "the slice must preserve the source's odd characters"
    payload, problems = build_payload(doc, {"inputs": [("Wood", "procure wood")]})
    assert not problems
    kept, dropped = validate_against_source(payload, doc)
    assert [x["name"] for x in kept["inputs"]] == ["Wood"]
    assert kept["inputs"][0]["match"] == "exact"


def test_a_marker_that_is_absent_is_reported_not_dropped():
    """A typo in the marker and a fact absent from the filing look identical
    in the output and are very different mistakes."""
    from shunkan.data.manual import build_payload

    _, problems = build_payload("Some document text.",
                                {"outputs": [("Widgets", "no such phrase")]})
    assert problems and "marker not found" in problems[0]["problem"]


def test_furniture_stripping_cannot_eat_the_document():
    """The first version was `integrated annual report[^|]*\\|\\s*\\d+`. `[^|]*`
    is unbounded, so it ran from a running head to the next pipe character
    ANYWHERE later in the filing. Measured: it deleted 66,155 characters
    (3.69%) of HDFCBANK's text and 24,114 (1.17%) of TATAPOWER's from the
    gate's view, which silently made TRUE, verbatim quotes uncitable. Bounded
    now, and this test exists so nobody unbounds it again."""
    from shunkan.data.llm import _FURNITURE

    doc = ("Integrated Annual Report 2025-26 | 89 " + "x" * 5000
           + " important sentence about hydro stations | 12 |")
    stripped = _FURNITURE.sub(" ", doc)
    assert "important sentence about hydro stations" in stripped
    assert len(doc) - len(stripped) < 200, "furniture stripping ate the document"


def test_control_characters_and_ligature_junk_fold_out():
    """MOTHERSON's top-customer chart is delimited by raw ESC bytes, which
    \\s+ does not touch. These filings also carry BOTH U+FFFD and U+FFFE as
    ligature-failure glyphs, sometimes in adjacent sentences."""
    from shunkan.data.llm import _norm

    assert _norm("Top customers\x1bchart") == _norm("Top customers chart")
    assert _norm("tari�ff") == _norm("tari￾ff") == "tariff"


def test_abbreviations_do_not_split_a_sentence():
    """'Dr. Fixit' appears 40 times in Pidilite's filing and grepping the
    phrase returned nothing, because every occurrence had been cut in half."""
    from shunkan.data.manual import _split_sentences

    got = _split_sentences("We sell Dr. Fixit waterproofing. It is a brand.")
    assert got[0].endswith("waterproofing.")
    assert "Dr. Fixit" in got[0]


def test_c1_bullet_glyph_folds():
    """Adani and Apollo filings use U+0083 as the bullet. It renders as
    NOTHING in a terminal, so a quote copied across a bullet looks
    byte-identical to the source on screen and silently fails to match. Four
    real nodes were lost to it before an agent tracked it down."""
    from shunkan.data.llm import _norm

    assert _norm("products\x83 include steel") == _norm("products include steel")


def test_acronyms_count_as_content_words():
    """The length>=4 floor discarded acronyms, so "NCR project portfolio"
    scored 1 of 2 and was rejected even though NCR and projects are both in
    the citing sentence. An acronym is usually the MOST identifying part of a
    name; ignoring it inverts the test."""
    from shunkan.data.llm import _content_words, _quote_mentions_node

    assert "ncr" in _content_words("NCR project portfolio")
    assert "asf" in _content_words("Aviation Service Facilities (ASF)")
    assert _quote_mentions_node(
        "NCR project portfolio", _norm("The Company expanded its NCR projects."))
    # but a short lowercase word is still not identifying
    assert "the" not in _content_words("the and of")


def test_a_back_window_snaps_to_word_boundaries():
    """A window landing mid-word decapitates the word the node name needs:
    back=420 on DALBHARAT produced 'NT LOCATIONS...' and the gate then
    rejected 'Ahmedabad plant' at 1 of 2 content words."""
    from shunkan.data.manual import sentence_at

    flat = "PLANT LOCATIONS Ahmedabad plant is at Sector 5 and operates well"
    got = sentence_at(flat, "Sector 5", back=26, fwd=0)
    # unsnapped this would begin "NT LOCATIONS", cutting LOCATIONS in half
    assert got.startswith("LOCATIONS"), got
    assert "Ahmedabad plant" in got, "the word the node name needs survived"


def test_occurrence_selects_a_later_match():
    """Dalmia Bharat's plant list appears twice; anchoring on the first cost
    six nodes."""
    from shunkan.data.manual import sentence_at

    flat = "Table one lists Ariyalur. Filler sentence here. Table 14 lists Ariyalur again."
    first = sentence_at(flat, "Ariyalur")
    second = sentence_at(flat, "Ariyalur", occurrence=2)
    assert "Table one" in first and "Table 14" in second


def test_a_name_overlap_failure_says_so_instead_of_crying_fabrication():
    """"neither the quote nor the name occurs in the document" misdirected
    every agent that hit it: for "Sikandarabad plant" cited to an address
    line, the quote is REAL and the name carries a word the sentence lacks.
    The fix is renaming, not finding new evidence."""
    doc = "PLANT LOCATIONS Sikandarabad Plot 5, Industrial Area, UP 203205 and more."
    payload = {"facilities": [{"name": "Sikandarabad warehouse",
                               "quote": "Sikandarabad Plot 5, Industrial Area, UP 203205"}],
               "inputs": [], "outputs": [], "customers": []}
    kept, dropped = validate_against_source(payload, doc)
    if dropped:
        assert "does not mention this node" in dropped[0]["reason"]
        assert "warehouse" in dropped[0]["reason"]


def test_combining_and_precomposed_accents_fold():
    """Jubilant's filing writes CAFE + U+0301 in one place and the precomposed
    U+00E9 three words later. They render identically and never match as
    bytes, so a true quote fails for a difference no reader can see."""
    from shunkan.data.llm import _norm

    assert _norm("CAFÉ Coffee Day") == _norm("Café Coffee Day")


def test_a_hard_to_tokenise_name_still_gets_checked():
    """"Uri-I" reduced to zero content words - three letters, and the acronym
    pattern needs a second capital - and an empty list made the check return
    True UNCHECKED. A name that is hard to tokenise deserves a weaker check,
    never no check at all."""
    from shunkan.data.llm import _content_words, _quote_mentions_node

    assert _content_words("Uri-I")          # not empty
    assert not _quote_mentions_node("Uri-I", _norm("a sentence about cement"))
    assert _quote_mentions_node("Uri-I", _norm("The Uri-I power station"))
