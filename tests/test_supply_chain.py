"""The SPLC extractor: evidence-first, never inferred."""

from shunkan.data.supply_chain import build_supply_map

# A miniature annual report in the register real ones use.
AR = """
Corporate Overview | Integrated Annual Report 2025-26
The Company operates 10 sugar manufacturing units with a combined crushing
capacity of 80,000 TCD of cane per day. It also operates 5 distillery units
with a combined production capacity of 1,050 KL per day.
Procurement of sugarcane from farmers in Uttar Pradesh remains the Company's
principal raw material and the largest single input cost.
Ethanol produced from molasses is supplied to oil marketing companies under
the national blending programme.
Industrial alcohol is distributed to institutional buyers for various uses.
As on 31st March, 2026, the Company has one Associate Company, namely,
Auxilo Finserve Private Limited (AFPL).
The Company does not have any subsidiaries or joint ventures.
"""


def test_inputs_are_found_with_their_sentence():
    m = build_supply_map("TEST", AR, "doc", 12, "Test Company")
    terms = {n.term for n in m.inputs}
    assert "sugarcane" in terms or "cane" in terms
    for n in m.inputs:
        assert n.evidence and len(n.evidence) > 30
        assert n.term.lower() in n.evidence.lower()   # the quote proves the node


def test_customers_capture_the_counterparty_sentence():
    m = build_supply_map("TEST", AR, "doc", 12, "Test Company")
    ev = " ".join(n.evidence for n in m.customers).lower()
    assert "oil marketing companies" in ev or "institutional buyers" in ev


def test_facilities_carry_capacities():
    m = build_supply_map("TEST", AR, "doc", 12, "Test Company")
    joined = " ".join(n.term for n in m.facilities)
    assert "TCD" in joined or "KL" in joined


def test_family_names_the_associate_and_keeps_the_negative():
    m = build_supply_map("TEST", AR, "doc", 12, "Test Company")
    assert any("Auxilo" in n.term for n in m.family)
    assert any("does not have any subsidiaries" in n for n in m.notes)


def test_nothing_is_invented_when_the_report_is_silent():
    m = build_supply_map("TEST", "This report contains no operational detail.",
                         "doc", 1, "Test Company")
    assert m.inputs == [] and m.customers == []
    assert any("no input/customer language matched" in n for n in m.notes)


def test_page_furniture_never_becomes_evidence():
    noisy = AR + "\n(H in crores) 18 | Test Company Limited sugarcane cane raw material"
    m = build_supply_map("TEST", noisy, "doc", 12, "Test Company")
    for n in m.inputs + m.outputs + m.customers:
        assert "| Test Company" not in n.evidence
