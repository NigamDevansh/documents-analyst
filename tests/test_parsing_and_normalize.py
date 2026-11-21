"""Sections, chunks and normalisers — Phase 2B.

Two different standards apply here, and the split is deliberate:

  * **Section boundaries are heuristic.** Getting one slightly wrong costs
    retrieval quality.
  * **Offsets are exact.** Getting one wrong breaks Gate 1.

So the offset properties are asserted absolutely, and the heading heuristics
are asserted against real contract fixtures rather than invented strings.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from documents_analyst_core.parsing.chunks import chunk_sections, context_header
from documents_analyst_core.parsing.sections import Section, find_preamble, split_sections
from documents_analyst_core.portable.document_text import extract
from documents_analyst_core.provenance.normalize import (
    normalize_date,
    normalize_duration_days,
    normalize_duration_hours,
    normalize_duration_months,
    normalize_entity_name,
    normalize_money,
    normalize_percent,
)

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
MSA = extract(FIXTURES / "conflict_pile" / "01_msa.md").text


# ── sections: the exact part ──────────────────────────────────────────────


def test_sections_cover_the_text_with_no_gaps() -> None:
    """A gap would be text no chunk can cite — a term that exists in the
    document and cannot appear in the register."""
    sections = split_sections(MSA)

    assert sections[0].start == 0
    assert sections[-1].end == len(MSA)
    for earlier, later in zip(sections, sections[1:], strict=False):
        assert earlier.end == later.start


def test_sections_do_not_overlap() -> None:
    """Overlap would let one clause be extracted twice and land as two facts
    disagreeing with themselves."""
    sections = split_sections(MSA)
    assert all(s.start < s.end for s in sections)


def test_numbers_are_kept_as_printed() -> None:
    """ "4.2" is how the contract refers to itself, and how a reviewer will look
    it up. Renumbering would make our citations unusable against the paper."""
    numbers = [s.number for s in split_sections(MSA) if s.number]

    assert numbers[:3] == ["1.", "2.", "3."]
    assert len(numbers) == 12


def test_empty_text_produces_no_sections() -> None:
    assert split_sections("") == []


def test_text_with_no_headings_is_one_section() -> None:
    body = "A single paragraph of contract prose with no numbering at all. " * 4
    sections = split_sections(body)

    assert len(sections) == 1
    assert sections[0].number is None
    assert (sections[0].start, sections[0].end) == (0, len(body))


# ── sections: the heuristic part ──────────────────────────────────────────


def test_a_numbered_paragraph_is_not_treated_as_a_heading() -> None:
    """Contracts open clauses with numbered prose, which matches the heading
    pattern perfectly. Length is what separates the two, and getting it wrong
    shatters a document into sections holding no terms."""
    text = (
        "4.2 The Supplier shall maintain insurance of not less than five million "
        "dollars covering public liability, product liability and professional "
        "indemnity for the duration of this Agreement and for six years after.\n\n"
        "5. Termination\n\nEither party may terminate on ninety days notice."
    )
    numbers = [s.number for s in split_sections(text) if s.number]

    assert "5." in numbers
    assert "4.2" not in numbers


def test_a_short_run_of_headings_does_not_become_one_section_each() -> None:
    """A title page of one-line headings otherwise becomes several sections
    holding nothing, and every retrieval hit lands on a heading.

    The assertion is *fewer sections than headings*, not an exact count. Where
    the run collapses depends on where the real prose starts, and pinning that
    would be testing the arithmetic rather than the behaviour.
    """
    text = "1. Definitions\n2. Term\n3. Payment\n\n" + ("Real contract prose. " * 30)

    sections = split_sections(text, min_chars=120)

    assert len(sections) < 3, "three headings must not become three sections"
    assert sections[-1].end == len(text)


def test_a_numbered_section_is_never_absorbed_into_the_preamble() -> None:
    """The preamble has no number to keep, so the merge would silently delete
    "5." from the document's own reference scheme — and a citation that cannot
    name its clause loses most of its value."""
    text = "Between Acme and Beta.\n\n5. Termination\n\nNinety days notice."

    sections = split_sections(text, min_chars=120)

    assert [s.number for s in sections] == [None, "5."]


def test_level_comes_from_the_dotted_number() -> None:
    text = "1. Top\n\n" + "x " * 80 + "\n\n1.1 Nested\n\n" + "y " * 80
    levels = {s.number: s.level for s in split_sections(text) if s.number}

    assert levels["1."] == 1
    assert levels["1.1"] == 2


def test_the_preamble_is_bounded_not_the_whole_document() -> None:
    """Party extraction over a whole contract picks up every company named in
    an indemnity clause."""
    sections = split_sections(MSA)
    start, end = find_preamble(MSA, sections)

    assert (start, end) < (0, len(MSA)) or end < len(MSA)
    assert "Acme Corporation" in MSA[start:end]


def test_a_street_address_is_not_a_section_heading() -> None:
    """`200 Freight Lane` and `4 Payment Terms` are the same shape.

    Accepting the bare form split the party block of every block-formatted
    letter and invoice: the preamble ended at the street number, so only the
    party named above it was ever seen.
    """
    text = (
        "SIDE LETTER\n\nFrom: Beta Logistics LLC\n200 Freight Lane\n"
        "Albany, New York 12205\n\nTo: Acme Corporation\n100 Industrial Way\n"
        "Newark, Delaware 19702\n\n1. Expiry\n\n" + ("Contract prose. " * 20)
    )
    numbers = [s.number for s in split_sections(text) if s.number]

    assert numbers == ["1."], f"an address line became a section: {numbers}"


def test_the_preamble_holds_every_party_of_a_block_formatted_document() -> None:
    """The regression that made a pile report no conflicts at all.

    An invoice naming its vendor above its customer resolved to one party, the
    side letter naming them the other way round resolved to the other, and the
    two were filed as different agreements — so the price and expiry they
    disagreed on were never compared.
    """
    text = (
        "INVOICE\n\nVendor Information:\nBeta Logistics LLC\n200 Freight Lane\n"
        "Albany, New York 12205\n\nCustomer Information:\nAcme Corporation\n"
        "100 Industrial Way\nNewark, Delaware 19702\n\n"
        "This invoice is issued pursuant to the Master Services Agreement.\n"
    )
    start, end = find_preamble(text, split_sections(text))
    window = text[start:end]

    assert "Beta Logistics LLC" in window
    assert "Acme Corporation" in window


def test_an_unnumbered_document_still_gets_a_bounded_preamble() -> None:
    """A document that never numbers a section would otherwise hand its whole
    body to party extraction — and the remittance block at the bottom of an
    invoice names a bank that is not a party to anything."""
    body = "Acme Corporation and Beta Logistics LLC.\n" + ("Prose with no headings. " * 400)
    start, end = find_preamble(body, split_sections(body))

    assert end - start < len(body)
    assert "Acme Corporation" in body[start:end]


# ── chunks ────────────────────────────────────────────────────────────────


def test_every_chunk_text_is_exactly_its_own_slice() -> None:
    """The whole contract of the module. A chunk whose text was cleaned or
    joined can no longer locate its own quote."""
    sections = split_sections(MSA)
    for chunk in chunk_sections(MSA, sections):
        assert chunk.text == MSA[chunk.start : chunk.end]


def test_chunks_never_cross_a_section_boundary() -> None:
    """A chunk spanning two sections produces a citation pointing at two places
    at once."""
    sections = split_sections(MSA)
    bounds = [(s.start, s.end) for s in sections]

    for chunk in chunk_sections(MSA, sections):
        assert any(lo <= chunk.start and chunk.end <= hi for lo, hi in bounds)


def test_a_long_section_splits_with_overlap_and_still_terminates() -> None:
    """Overlap stops a term stated across a sentence break from falling into
    the gap between two chunks and being retrievable from neither."""
    body = "This clause states a term. " * 200
    section = [Section("9.", "Long", 0, len(body), 1)]

    chunks = chunk_sections(body, section, target_tokens=50, overlap_tokens=10)

    assert len(chunks) > 3
    assert all(c.text == body[c.start : c.end] for c in chunks)
    assert any(b.start < a.end for a, b in zip(chunks, chunks[1:], strict=False)), "chunks overlap"


def test_the_context_header_is_never_part_of_the_text() -> None:
    """Prepending it would shift every offset in the chunk by its own length,
    silently breaking Gate 1 for every fact inside."""
    sections = split_sections(MSA)
    chunks = chunk_sections(MSA, sections)

    headed = [c for c in chunks if c.context_header]
    assert headed
    for chunk in headed:
        assert not chunk.text.startswith(chunk.context_header)


def test_context_header_reads_as_a_label() -> None:
    assert context_header(Section("4.2", "Payment Terms", 0, 1, 2)) == "4.2 · Payment Terms"
    assert context_header(Section(None, None, 0, 1, 1)) == ""


# ── normalisers: durations ────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Net 30", 30),
        ("net30", 30),
        ("thirty (30) days", 30),
        ("30 days", 30),
        ("thirty days", 30),
        ("2 years", 730),
        ("ninety (90) days", 90),
        ("forty-five days", 45),
        ("no duration here", None),
    ],
)
def test_duration_days(text: str, expected: int | None) -> None:
    assert normalize_duration_days(text) == expected


def test_parenthesised_digits_win_over_the_word() -> None:
    """The two halves can disagree in a badly drafted contract. The digits are
    what a court reads and what a reviewer checking our work will see."""
    assert normalize_duration_days("thirty (45) days") == 45


@pytest.mark.parametrize(
    ("text", "expected"),
    [("thirty-six (36) months", 36), ("3 years", 36), ("12 months", 12), ("30 days", None)],
)
def test_duration_months(text: str, expected: int | None) -> None:
    assert normalize_duration_months(text) == expected


def test_duration_hours() -> None:
    assert normalize_duration_hours("four (4) hours") == 4
    assert normalize_duration_hours("1 business day") == 8
    assert normalize_duration_hours("30 days") is None


def test_conversion_goes_to_a_finer_unit_only() -> None:
    """The asymmetry is the point, and it is not arbitrary.

    Coarse → fine is exact enough to compare: a contract saying "payment within
    2 months" really is 60 days for the purpose of ranking payment terms. Fine
    → coarse is not: "45 days" is not a whole number of months, and rounding it
    to 1 or 2 invents a term the contract never stated.

    This is why the taxonomy splits durations by unit rather than storing one
    `duration` — collapsing them is where "45 days" quietly becomes
    "45 months".
    """
    assert normalize_duration_days("2 months") == 60
    assert normalize_duration_days("2 years") == 730

    assert normalize_duration_months("45 days") is None
    assert normalize_duration_hours("30 days") is None


# ── normalisers: money and percent ────────────────────────────────────────


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("$10,000", (Decimal("10000"), "USD")),
        ("USD 10000.00", (Decimal("10000.00"), "USD")),
        ("£2,500", (Decimal("2500"), "GBP")),
        ("€1.5 million", (Decimal("1500000.0"), "EUR")),
        ("$500k", (Decimal("500000"), "USD")),
        ("10,000", None),
    ],
)
def test_money(text: str, expected: tuple[Decimal, str] | None) -> None:
    assert normalize_money(text) == expected


def test_an_amount_with_no_currency_is_refused() -> None:
    """Assuming dollars would let a euro cap and a dollar cap compare as equal,
    and a contradiction that should have been raised would vanish."""
    assert normalize_money("The cap is 10,000") is None


def test_percent() -> None:
    assert normalize_percent("1.5%") == Decimal("1.5")
    assert normalize_percent("99.9 per cent") == Decimal("99.9")
    assert normalize_percent("no rate") is None


# ── normalisers: dates ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("1 January 2025", "2025-01-01"),
        ("31 December 2027", "2027-12-31"),
        ("January 1, 2025", "2025-01-01"),
        ("2025-01-01", "2025-01-01"),
        ("15 Mar 2026", "2026-03-15"),
    ],
)
def test_unambiguous_dates(text: str, expected: str) -> None:
    assert normalize_date(text) == expected


def test_an_ambiguous_numeric_date_returns_none() -> None:
    """03/04/2025 is 3 April in London and 4 March in New York, and nothing in
    the text says which. A wrong termination date is worse than a missing one."""
    assert normalize_date("03/04/2025") is None


def test_a_numeric_date_resolvable_by_range_is_accepted() -> None:
    """Only when one number cannot be a month. No guessing beyond that."""
    assert normalize_date("25/12/2025") == "2025-12-25"
    assert normalize_date("12/25/2025") == "2025-12-25"


def test_an_impossible_date_returns_none() -> None:
    assert normalize_date("31 February 2025") is None


# ── normalisers: entities ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("Acme Corp.", "ACME CORPORATION"),
        ("Beta Logistics LLC", "Beta Logistics, L.L.C."),
        ("Northwind Trading Ltd", "northwind trading limited"),
    ],
)
def test_entity_names_match_across_spellings(left: str, right: str) -> None:
    assert normalize_entity_name(left) == normalize_entity_name(right)


def test_a_company_whose_name_starts_with_a_suffix_word_keeps_it() -> None:
    """Suffixes come off the END only. "Limited Edition Media" must keep its
    first word or it collides with everything."""
    assert normalize_entity_name("Limited Edition Media") == "limited edition media"


def test_two_different_companies_do_not_collide() -> None:
    assert normalize_entity_name("Acme Corp") != normalize_entity_name("Acme Holdings")
