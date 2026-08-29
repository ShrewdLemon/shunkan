"""The filer's relationship text is a HEADED PHRASE, and the head governs.

These cases are taken verbatim from filed BSE related-party XBRL. The flat
substring scan they replace classified every one of the person phrases below
as a subsidiary, because "subsidiary" appears in them as the object of "of".
"""
from __future__ import annotations

import pytest

from shunkan.data.bse import normalise_relationship


@pytest.mark.parametrize("raw,expect", [
    # --- genuinely structural ---
    ("Subsidiary", "subsidiary_of"),
    ("Subsidiary of the Company", "subsidiary_of"),
    ("Subsidiary of the company", "subsidiary_of"),
    ("Wholly owned subsidiary", "wholly_owned_subsidiary_of"),
    ("Wholly-owned Subsidiary", "wholly_owned_subsidiary_of"),
    ("Fellow subsidiary", "fellow_subsidiary_of"),
    ("Associate", "associate_of"),
    ("Joint Venture", "joint_venture_with"),
    ("Holding Company", "holding_company_of"),
    ("Ultimate Parent Company", "subsidiary_of_ultimate_parent"),
    ("Promoter", "promoter_group_of"),

    # --- people, whose phrases NAME a structural entity as a qualifier ---
    ("Director", "key_management_of"),
    ("Director of Subsidiary", "key_management_of"),
    ("Director of Subsidiary Company", "key_management_of"),
    ("KMP of Subsidiary", "key_management_of"),
    ("KMP of Subsidiary Company", "key_management_of"),
    ("Key Managerial Personnel of Subsidiary", "key_management_of"),
    ("Key Managerial Personnel (KMP)", "key_management_of"),
    ("Key Management Personnel/ Directors/their relatives", "relative_of_kmp"),
    ("Relative of KMP/Director of subsidiary", "relative_of_kmp"),
    ("Relative of KMP/Director of Fellow Subsidiary", "relative_of_kmp"),
    ("Relative of Director", "relative_of_kmp"),

    # --- an entity reached THROUGH a person is neither the person nor a
    #     subsidiary of the filer ---
    ("Interested entity of KMP/Directors or their relative",
     "kmp_interested_entity_of"),
    ("Interested entity of KMP/Director of subsidiary",
     "kmp_interested_entity_of"),
    ("Enterprise over which KMP has significant influence",
     "kmp_interested_entity_of"),

    # --- a promoter's relative is promoter group as a matter of law ---
    ("Relative of Promoter", "promoter_group_of"),
    ("Promoter Group", "promoter_group_of"),

    # --- nothing usable ---
    ("", "related_party_of"),
    ("   ", "related_party_of"),
    ("Any other related party", "related_party_of"),
])
def test_relationship_head_governs(raw: str, expect: str) -> None:
    assert normalise_relationship(raw) == expect


def test_no_person_phrase_becomes_structural() -> None:
    """The specific regression: a person must never land on a company relation.

    528 HDFC Bank rows did exactly that, so the company page would have
    claimed 528 subsidiaries - individuals among them - each with a source
    attached to make it look checked.
    """
    company_rels = {
        "subsidiary_of", "wholly_owned_subsidiary_of", "fellow_subsidiary_of",
        "associate_of", "joint_venture_with", "holding_company_of",
        "subsidiary_of_ultimate_parent",
    }
    person_phrases = [
        "Director of Subsidiary", "KMP of Subsidiary Company",
        "Relative of KMP/Director of subsidiary", "Director of Subsidiary Company",
        "Key Managerial Personnel of Subsidiary",
        "Relative of KMP/Director of Fellow Subsidiary",
        "Director of Associate Company", "KMP of Joint Venture",
    ]
    for phrase in person_phrases:
        assert normalise_relationship(phrase) not in company_rels, phrase


def test_scrip_code_miss_names_the_reason() -> None:
    """BSE Ltd and CDSL are NSE-listed only, so a BSE-sourced feed being empty
    for them is correct behaviour, not a failure. The message has to say so -
    a bare "no scrip code" reads as a broken lookup and invites someone to
    "fix" it by loosening the name match until it hits the wrong company."""
    import pytest as _pytest

    from shunkan.data.bse import DataError, scrip_code

    with _pytest.raises(DataError) as ei:
        scrip_code("NOTAREALTICKERXYZ")
    msg = str(ei.value)
    assert "scrip master" in msg
    assert "NSE-listed-only" in msg


@pytest.mark.parametrize("raw,expect", [
    # THE HEAD NAMES THE PARTY; the qualifier names who they are related
    # THROUGH. 2,728 HDFC Bank rows filed "Related Party of Subsidiary" and
    # became subsidiaries of the bank - among them "Ameet P. Hariani HUF" and
    # "Ms. Heta Hariani Ray", a director's family holdings.
    ("Related Party of Subsidiary", "related_party_of_subsidiary"),
    ("Related Parties of Subsidiary", "related_party_of_subsidiary"),
    ("Related Party of the Company", "related_party_of"),
    ("Other Related Party", "related_party_of"),

    # A subsidiary OF THE PARENT is a sibling, not a child. Reading it as a
    # subsidiary of the filer promotes 1,084 sibling companies into children.
    ("Subsidiary of parent company", "fellow_subsidiary_of"),
    ("Subsidiaries of parent company", "fellow_subsidiary_of"),
    ("Subsidiary of Holding Company", "fellow_subsidiary_of"),
    ("Subsidiary of the Company", "subsidiary_of"),
    ("Subsidiaries of TCS", "subsidiary_of"),

    # filed with a stray space before the hyphen, 944 times
    ("Wholly -Owned Subsidiary", "wholly_owned_subsidiary_of"),
    ("Wholly - Owned Subsidiary", "wholly_owned_subsidiary_of"),
    ("WHOLLY OWNED SUBSIDIARY", "wholly_owned_subsidiary_of"),

    # plural and casing variants seen in the corpus
    ("Subsidiaries", "subsidiary_of"),
    ("SUBSIDIARY", "subsidiary_of"),
    ("Subsidiary Company", "subsidiary_of"),
    ("Step Subsidiary", "subsidiary_of"),
])
def test_qualifier_does_not_become_the_relation(raw: str, expect: str) -> None:
    assert normalise_relationship(raw) == expect


def test_related_party_of_subsidiary_is_not_a_subsidiary() -> None:
    """The specific regression, stated as the claim it would have made."""
    assert normalise_relationship("Related Party of Subsidiary") != "subsidiary_of"


@pytest.mark.parametrize("raw,expect", [
    # "Employee" splits both ways and the qualifier decides which.
    ("Employee of Max Healthcare Institute Limited and director in related party",
     "key_management_of"),
    ("Employee of Jubilant Foodworks & Director of subsidiary companies",
     "key_management_of"),
    # ...but these name a FUND, not a person, and were previously read as a
    # subsidiary or a director depending on what else was in the string.
    ("Employee Trust", "employee_benefit_plan_of"),
    ("Employee Welfare Trust", "employee_benefit_plan_of"),
    ("Employee benefit plans", "employee_benefit_plan_of"),
    ("Employees' Gratuity Fund", "employee_benefit_plan_of"),
    ("Employees Provident Fund Trust", "employee_benefit_plan_of"),
    ("Employee Superannuation Fund", "employee_benefit_plan_of"),
])
def test_employee_is_a_person_or_a_fund_depending_on_the_qualifier(
        raw: str, expect: str) -> None:
    assert normalise_relationship(raw) == expect


def test_company_names_that_look_like_honorifics_are_untouched() -> None:
    """An audit that flags people by honorific catches "Dr. Reddy's
    Laboratories SA" and "Shri Kannan Departmental Store Limited", which are
    companies. The classifier reads the RELATIONSHIP text, never the
    counterparty name, so those stay correct - this test pins that boundary so
    nobody "improves" it by sniffing names."""
    import inspect

    from shunkan.data import bse

    src = inspect.getsource(bse.normalise_relationship)
    assert "counterparty" not in src, \
        "normalise_relationship must classify the relationship text only"
    assert normalise_relationship("Subsidiary") == "subsidiary_of"
