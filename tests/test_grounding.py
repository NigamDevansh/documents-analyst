"""Gate 1 and the extraction gates — Phase 2B.

This is where the claim "it never bluffs" is proved. Everything in this file runs
offline against a `FakeLLM`, because the claim is not "a good
model behaves" — it is "a bad model cannot get through".
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from documents_analyst_core.parsing.sections import Section, split_sections
from documents_analyst_core.portable.document_text import extract
from documents_analyst_core.portable.llm_client import FakeLLM
from documents_analyst_core.portable.verify import find_quote, verify_quote
from documents_analyst_core.provenance.extract import extract_facts, ground_fact
from documents_analyst_core.provenance.open_subjects import extract_open_subjects
from documents_analyst_core.taxonomy.schema import extraction_schema

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
TEXT = "Payment is due Net 30 from invoice date. Late fees accrue at 1.5% monthly."


def fact(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "field_key": "payment_terms_days",
        "value_text": "Net 30",
        "source_quote": "Net 30",
        "start": TEXT.index("Net 30"),
        "end": TEXT.index("Net 30") + len("Net 30"),
    }
    return {**base, **overrides}


def ground(**overrides: Any):  # noqa: ANN201
    return ground_fact(fact(**overrides), document_id="d1", text=TEXT)


# ── Gate 1 itself ─────────────────────────────────────────────────────────


def test_verbatim_gate_accepts_an_exact_quote() -> None:
    assert verify_quote(TEXT, 15, 21, "Net 30")


def test_verbatim_gate_rejects_paraphrase() -> None:
    """No fuzzy matching, no threshold. "thirty days" is not "Net 30"."""
    assert not verify_quote(TEXT, 15, 21, "thirty days")


@pytest.mark.parametrize(
    ("start", "end", "quote", "why"),
    [
        (-len(TEXT), len(TEXT), TEXT, "negative start slices from the END"),
        (-11, len(TEXT), TEXT[-11:], "negative start that still matches"),
        (40, 5, "", "reversed offsets slice to '' — a fact with NO quote"),
        (15, 9999, TEXT[15:], "end past the string clamps silently"),
        (len(TEXT) + 5, len(TEXT) + 9, "", "entirely out of range"),
    ],
)
def test_gate_one_refuses_offsets_that_are_not_real(
    start: int, end: int, quote: str, why: str
) -> None:
    """Python slicing is lenient in three ways, and every one of them lets a
    false claim through the gate the whole system rests on.

    The empty-quote case is the worst: `start > end` slices to `""`, so a fact
    carrying no quote at all verified — against a gate whose entire purpose is
    to require one.
    """
    assert not verify_quote(TEXT, start, end, quote), why


def test_an_empty_quote_never_verifies() -> None:
    assert not verify_quote(TEXT, 0, 0, "")
    assert not verify_quote(TEXT, 5, 5, "")


def test_find_quote_refuses_an_ambiguous_match() -> None:
    """Guessing which of two occurrences was meant would reintroduce exactly
    the uncertainty this module removes."""
    twice = "Net 30 applies. Net 30 applies."
    assert find_quote(twice, "Net 30") is None
    assert find_quote(TEXT, "Net 30") == (15, 21)
    assert find_quote(TEXT, "Net 45") is None


# ── the three extraction gates ────────────────────────────────────────────


def test_a_clean_fact_passes_and_is_normalised() -> None:
    grounded, why = ground()

    assert why == ""
    assert grounded is not None
    assert grounded.field_key == "payment_terms_days"
    assert grounded.value_text == "Net 30", "the register shows what the contract said"
    assert grounded.value_normalized == "30", "comparison uses the normalised form"
    assert TEXT[grounded.citation.start : grounded.citation.end] == "Net 30"


def test_parties_cannot_come_from_generic_extraction() -> None:
    """`parties` is grounded like anything else, and grounded is not the same
    as *contracting*.

    A model can quote "the American Arbitration Association" verbatim out of a
    dispute-resolution clause and pass every gate in this module. The dedicated
    path closes that structurally — candidates come from NER over the preamble
    and the model picks indices — so a second, unconstrained door into the same
    field would make the safeguard a convention rather than a property.
    """
    text = "Disputes are heard by the American Arbitration Association."
    grounded, why = ground_fact(
        {
            "field_key": "parties",
            "value_text": "American Arbitration Association",
            "source_quote": "American Arbitration Association",
            "start": text.index("American"),
            "end": len(text) - 1,
        },
        document_id="d1",
        text=text,
    )

    assert grounded is None
    assert "does not come from generic extraction" in why
    assert "pick_parties" in why, "the error must say where the field does come from"


def test_a_field_outside_the_taxonomy_is_dropped() -> None:
    """The schema constrains this too. Checked again here because schema
    enforcement is a provider feature and provider features vary."""
    grounded, why = ground(field_key="late_payment_penalty")

    assert grounded is None
    assert "not in the taxonomy" in why


def test_the_schema_refuses_an_invented_field_before_the_gate_sees_it() -> None:
    """The first line of defence: `field_key` is an enum of the 28 keys, so an
    invented one is a schema violation at the boundary rather than something
    caught afterwards. This is why the taxonomy is a closed list at all."""
    from documents_analyst_core.portable.llm_client import LLMSchemaViolation

    llm = FakeLLM(responses=[{"facts": [fact(field_key="late_payment_penalty")]}])

    with pytest.raises(LLMSchemaViolation):
        llm.structured(system="s", user="u", schema=extraction_schema(), max_tokens=10)


def test_a_quote_that_is_not_in_the_document_is_dropped() -> None:
    """The bluff case: a plausible sentence the contract never contained."""
    grounded, why = ground(source_quote="Payment is due within ninety days.")

    assert grounded is None
    assert "not in the document" in why


def test_a_fact_with_no_quote_is_dropped() -> None:
    grounded, why = ground(source_quote="")

    assert grounded is None
    assert "no source_quote" in why


def test_a_fact_with_no_value_is_dropped() -> None:
    grounded, why = ground(value_text="   ")

    assert grounded is None
    assert "no value_text" in why


# ── relocation ────────────────────────────────────────────────────────────


def test_a_verbatim_quote_with_wrong_offsets_is_relocated() -> None:
    """Models are reliably bad at character arithmetic and reliably good at
    copying. Dropping this would throw away a true fact over a number the model
    was never going to get right."""
    grounded, why = ground(start=0, end=6)

    assert why == ""
    assert grounded is not None
    assert (grounded.citation.start, grounded.citation.end) == (15, 21)
    assert TEXT[grounded.citation.start : grounded.citation.end] == "Net 30"


def test_relocation_is_recorded_on_the_citation() -> None:
    """A reviewer must be able to tell which citations the model got right.

    Confidence alone cannot say it. A relocated fact the model was sure about
    and an honest fact it was half sure about both arrive at 0.5, so the one
    question the record exists to answer becomes unanswerable. The flag is a
    fact about the offsets; the confidence nudge is only a ranking tiebreak.
    """
    exact, _ = ground()
    moved, _ = ground(start=0, end=6)

    assert exact is not None and moved is not None
    assert exact.citation.relocated is False
    assert moved.citation.relocated is True
    assert moved.confidence < exact.confidence


def test_a_relocated_fact_is_distinguishable_from_an_uncertain_one() -> None:
    """The exact collision that confidence-halving could not express."""
    relocated, _ = ground(start=0, end=6, confidence=1.0)
    uncertain, _ = ground(confidence=0.5)

    assert relocated is not None and uncertain is not None
    assert relocated.confidence == uncertain.confidence
    assert relocated.citation.relocated != uncertain.citation.relocated


def test_an_ambiguous_quote_is_placed_at_the_occurrence_the_offsets_point_at() -> None:
    """Two occurrences of the same bytes are not two candidate meanings.

    The value is identical whichever is chosen and the citation lands on text
    that reads exactly as quoted, so the stated offset is enough to settle it.
    Dropping the fact was the costlier failure: the term left the register
    entirely, and a term that is not in the register is never compared to the
    other documents in the agreement.
    """
    twice = "Net 30 applies. Net 30 applies."
    grounded, why = ground_fact(fact(start=0, end=1), document_id="d1", text=twice)

    assert grounded is not None, why
    assert (grounded.citation.start, grounded.citation.end) == (0, 6)
    assert twice[grounded.citation.start : grounded.citation.end] == "Net 30"
    assert grounded.citation.relocated is True, "a repaired citation is never silent"


def test_an_ambiguous_quote_prefers_the_later_occurrence_when_offsets_say_so() -> None:
    """The offset chooses, so it must be able to choose either one."""
    twice = "Net 30 applies. Net 30 applies."
    grounded, why = ground_fact(fact(start=16, end=17), document_id="d1", text=twice)

    assert grounded is not None, why
    assert (grounded.citation.start, grounded.citation.end) == (16, 22)


def test_an_ambiguous_quote_with_no_offsets_is_still_dropped() -> None:
    """Nothing points at an occurrence, and this is where guessing would start."""
    twice = "Net 30 applies. Net 30 applies."
    grounded, why = ground_fact(fact(start=None, end=None), document_id="d1", text=twice)

    assert grounded is None
    assert "more than once" in why


def test_missing_or_junk_offsets_fall_through_to_relocation() -> None:
    for broken in ({"start": None, "end": None}, {"start": "x", "end": "y"}):
        grounded, why = ground(**broken)
        assert grounded is not None, why
        assert (grounded.citation.start, grounded.citation.end) == (15, 21)


# ── the whole path, with a fake model ─────────────────────────────────────


def test_extraction_keeps_the_good_and_names_the_bad() -> None:
    """A rejection is returned, not logged. A silent drop and a section that
    genuinely stated nothing look identical, and the difference is what tells
    you whether extraction is working at all."""
    # `enforce_schema=False` on purpose. With it on, `FakeLLM` refuses to
    # return `not_a_field` at all — which is the schema doing its job, and is
    # asserted separately. Here we simulate the case the second gate exists
    # for: a provider whose "strict" mode let a key through anyway.
    llm = FakeLLM(
        enforce_schema=False,
        responses=[
            {
                "facts": [
                    fact(),
                    fact(field_key="not_a_field"),
                    fact(source_quote="a sentence the contract never contained"),
                ]
            }
        ],
    )
    sections = [Section(None, None, 0, len(TEXT), 1)]

    result = extract_facts(document_id="d1", text=TEXT, sections=sections, llm=llm)

    assert [f.field_key for f in result.facts] == ["payment_terms_days"]
    assert len(result.rejected) == 2
    assert result.rejection_rate == pytest.approx(2 / 3)


def test_offsets_are_shifted_into_whole_document_coordinates() -> None:
    """The model is handed one section and answers in that section's
    coordinates. Every stored offset indexes into the FULL `extracted_text`,
    which is the only string Gate 1 knows about — so an unshifted offset would
    verify against the wrong part of a long contract."""
    document = "Preamble text that is quite long.\n\n" + TEXT
    offset = document.index(TEXT)
    section = Section("1.", "Payment", offset, len(document), 1)

    llm = FakeLLM(responses=[{"facts": [fact()]}])
    result = extract_facts(document_id="d1", text=document, sections=[section], llm=llm)

    assert len(result.facts) == 1
    citation = result.facts[0].citation
    assert document[citation.start : citation.end] == "Net 30"
    assert citation.start == offset + 15


def test_a_failing_section_does_not_lose_the_document() -> None:
    from documents_analyst_core.portable.llm_client import LLMTransportError

    llm = FakeLLM(responses=[LLMTransportError("provider down"), {"facts": [fact()]}])
    sections = [Section("1.", "A", 0, 40, 1), Section("2.", "B", 40, len(TEXT), 1)]

    result = extract_facts(document_id="d1", text=TEXT, sections=sections, llm=llm)

    assert len(result.facts) == 1, "the second section still produced its fact"
    assert any("provider down" in r.reason for r in result.rejected)


def test_max_sections_bounds_the_work() -> None:
    """Backs the SMALL_SAMPLE dev guard — passed in, never read from env."""
    llm = FakeLLM(responses=[{"facts": []}])
    sections = [Section(str(i), None, i * 10, i * 10 + 10, 1) for i in range(5)]

    result = extract_facts(
        document_id="d1", text="x" * 60, sections=sections, llm=llm, max_sections=1
    )

    assert result.sections_seen == 1
    assert len(llm.calls) == 1


def test_before_section_runs_at_each_actual_model_request() -> None:
    """The host can renew a lease without making portable extraction stateful."""
    calls = 0

    def renew() -> None:
        nonlocal calls
        calls += 1

    llm = FakeLLM(responses=[{"facts": []}, {"facts": []}])
    sections = [Section("1.", None, 0, 10, 1), Section("2.", None, 10, len(TEXT), 1)]

    extract_facts(document_id="d1", text=TEXT, sections=sections, llm=llm, before_section=renew)

    assert calls == 2


def test_open_subjects_renew_before_each_actual_model_request() -> None:
    """The escape-hatch path has the same host-controlled lease boundary."""
    calls = 0

    def renew() -> None:
        nonlocal calls
        calls += 1

    llm = FakeLLM(responses=[{"subjects": []}, {"subjects": []}])
    sections = [Section("1.", None, 0, 10, 1), Section("2.", None, 10, len(TEXT), 1)]

    extract_open_subjects(
        document_id="d1", text=TEXT, sections=sections, llm=llm, before_section=renew
    )

    assert calls == 2


# ── the prompt boundary ───────────────────────────────────────────────────


def test_instructions_are_composed_not_concatenated() -> None:
    """A scored invariant, asserted as a property rather than a string.

    The instruction lives entirely in `system`, the document entirely in
    `user`, and no document text reaches the system message by any route. That
    is what stops a section extending the task it is the subject of.
    """
    from documents_analyst_core.prompts.wrapping import system_preamble

    llm = FakeLLM(responses=[{"facts": []}])
    sections = [Section("1.", "Payment", 0, len(TEXT), 1)]

    extract_facts(document_id="d1", text=TEXT, sections=sections, llm=llm)

    call = llm.calls[0]
    assert system_preamble() in call.system, "the untrusted-content rules are stated"
    assert TEXT not in call.system, "no document text in the instruction"
    assert TEXT in call.user
    assert "<untrusted-SECTION>" in call.user, "and it arrives delimited"


def test_the_injection_fixture_reaches_the_model_only_as_data() -> None:
    """`injection_pile` contains text addressed to an AI. It must arrive inside
    the section delimiter like any other contract prose."""
    document = extract(FIXTURES / "injection_pile" / "01_msa_with_injection.md")
    sections = split_sections(document.text)
    llm = FakeLLM(responses=[{"facts": []}] * len(sections))

    extract_facts(document_id="d1", text=document.text, sections=sections, llm=llm)

    for call in llm.calls:
        assert "<untrusted-SECTION>" in call.user
        assert call.schema == extraction_schema(), "same closed schema every time"

    # The injected command is present as CONTENT and absent from every
    # instruction — which is the whole claim, and is not the same as it being
    # filtered out. We quote it; we do not obey it.
    injected = "ignore all previous instructions"
    users = " ".join(c.user for c in llm.calls).lower()
    systems = " ".join(c.system for c in llm.calls).lower()
    assert injected in users, "the fixture's attack text really is in the document"
    assert injected not in systems


# ── real documents have line breaks inside sentences ──────────────────────


def test_a_quote_broken_across_a_line_is_still_verbatim() -> None:
    """The gate must not lose a true fact to a newline.

    A `.md` fixture puts a paragraph on one line, so a model's quote matches
    byte for byte and the exact search always wins. A contract that arrived as
    PDF, DOCX or converted HTML wraps sentences, and a model asked to quote one
    returns it with single spaces. The quote IS in the document; `find` says it
    is not.

    Measured on four real SEC-filed contracts before this: thirteen facts
    dropped as "quote is not in the document", every one of them present. That
    is the gate failing on real input while passing on the fixtures.
    """
    document = "Fees.\nPayment is due Net\n45 days from the invoice date.\nOther text."
    quote = "Payment is due Net 45 days from the invoice date."

    assert quote not in document, "the premise: exact search cannot find it"
    span = find_quote(document, quote)
    assert span is not None
    start, end = span
    # Offsets land in the ORIGINAL text, so a highlight drawn from them is right.
    assert document[start:end] == "Payment is due Net\n45 days from the invoice date."
    assert verify_quote(document, start, end, document[start:end])


def test_whitespace_tolerance_is_not_fuzzy_matching() -> None:
    """Every non-whitespace character still has to be there, in order.

    This is the one concession in `verify.py`, and it has to stay a narrow one:
    no threshold, no similarity, no "close enough". A model that changes a
    number, drops a word or invents a clause is refused exactly as before.
    """
    document = "Payment is due Net\n45 days from the invoice date."

    assert find_quote(document, "Payment is due Net 90 days from the invoice date.") is None
    assert find_quote(document, "Payment is due from the invoice date.") is None
    assert (
        find_quote(document, "Payment is due Net 45 days from the invoice date, plus VAT.") is None
    )
    # Ambiguity is still refused in the fallback. Both occurrences here are
    # broken across a line, so the exact search finds neither and the collapsed
    # search finds two — which is the case that must not guess.
    twice = "Net\n45 days apply.\nNet\n45 days apply."
    assert find_quote(twice, "Net 45 days apply.") is None

    # An exact match that is unique still wins, even when a whitespace variant
    # of it exists elsewhere. Both spans are the same characters, so the value
    # is the same either way and the highlight lands on text that reads exactly
    # as quoted — the reasoning `_locate` already applies to repeated quotes.
    mixed = "Net\n45 days apply.\nNet 45 days apply."
    assert find_quote(mixed, "Net 45 days apply.") == (19, 37)


def test_a_relocated_citation_quotes_the_document_not_the_model() -> None:
    """`source_quote == extracted_text[char_start:char_end]`, always.

    The dashboard highlights by those offsets and the MCP approval gate checks
    an agent's confirmation against the stored quote. Storing the model's
    whitespace-normalised copy would leave both comparing against a string the
    document does not contain.
    """
    document = "Fees.\nPayment is due Net\n45 days from invoice.\n"
    fact, why = ground_fact(
        {
            "field_key": "payment_terms_days",
            "value_text": "45",
            "source_quote": "Payment is due Net 45 days from invoice.",
            "start": 0,
            "end": 10,
        },
        document_id="doc-1",
        text=document,
        offset=0,
    )
    assert fact is not None, why
    citation = fact.citation
    assert document[citation.start : citation.end] == citation.quote
    assert citation.relocated, "a repair is recorded, never silent"


def test_every_grounding_path_quotes_the_document() -> None:
    """One invariant, asserted across both paths that can create a citation.

    `source_quote == extracted_text[char_start:char_end]` is what the
    dashboard's highlight and the MCP approval gate both assume. There are two
    grounders — taxonomy fields and open subjects — and fixing only the first
    left 26 of 129 citations on a real pile pointing at text that differed from
    the stored quote by a newline. Parameterised so a third grounder cannot be
    added without meeting the same bar.
    """
    from documents_analyst_core.provenance.open_subjects import ground_subject

    document = "Fees.\nPayment is due Net\n45 days from invoice.\n"
    spanning = "Payment is due Net 45 days from invoice."

    field, why = ground_fact(
        {
            "field_key": "payment_terms_days",
            "value_text": "45",
            "source_quote": spanning,
            "start": 0,
            "end": 10,
        },
        document_id="doc-1",
        text=document,
        offset=0,
    )
    subject, why_subject = ground_subject(
        {
            "subject": "payment window",
            "value_text": "45 days",
            "source_quote": spanning,
            "start": 0,
            "end": 10,
        },
        document_id="doc-1",
        text=document,
        offset=0,
    )

    assert field is not None, why
    assert subject is not None, why_subject
    for name, citation in (("field", field.citation), ("open subject", subject.citation)):
        assert document[citation.start : citation.end] == citation.quote, (
            f"the {name} grounder stored a quote the document does not contain at its own offsets"
        )


def test_html_is_read_as_a_contract_not_refused(tmp_path: Path) -> None:
    """`.htm` is the format real contracts arrive in.

    Every agreement filed with the SEC, every contract saved from a browser.
    Refusing it meant the analyst read a folder of tidy `.md` fixtures and
    turned away the most common source of real ones — for a document it can
    read perfectly well once the tags are gone.

    Markup is stripped here, unlike `.md`, and the difference is not
    inconsistency: nobody cites a `<td>`, while `##` is characters a citation
    can legitimately point into.
    """
    page = tmp_path / "agreement.htm"
    page.write_text(
        "<html><head><style>p{color:red}</style><script>x=1</script></head>"
        "<body><h1>SUPPLY AGREEMENT</h1>"
        "<p>This Agreement is made between Acme&nbsp;Corp and Beta&nbsp;LLC.</p>"
        "<table><tr><td>Payment terms</td><td>Net 45</td></tr></table>"
        "</body></html>"
    )
    doc = extract(page)

    assert doc.meta["format"] == "html"
    assert "<" not in doc.text, "tags are not content"
    assert "x=1" not in doc.text and "color:red" not in doc.text, "script and style are not content"
    assert "SUPPLY AGREEMENT" in doc.text
    # Entities decoded, and the pieces a citation would quote are intact.
    assert "Acme Corp and Beta LLC" in doc.text
    assert "Net 45" in doc.text
    # Blocks separated the same way every other format separates them, so an
    # `.htm` and a `.docx` of one agreement still hash alike.
    assert "\n\n" in doc.text


def test_a_quote_from_html_still_grounds(tmp_path: Path) -> None:
    """The point of reading HTML at all: facts from it must survive Gate 1."""
    page = tmp_path / "supply.htm"
    page.write_text(
        "<body><p>Section 5. Payment.</p>"
        "<p>All invoices are payable Net 45 days from the date of invoice.</p></body>"
    )
    text = extract(page).text
    quote = "All invoices are payable Net 45 days from the date of invoice."

    fact, why = ground_fact(
        {"field_key": "payment_terms_days", "value_text": "45", "source_quote": quote},
        document_id="doc-1",
        text=text,
        offset=0,
    )
    assert fact is not None, why
    assert text[fact.citation.start : fact.citation.end] == fact.citation.quote
    assert fact.value_normalized == "45"


@pytest.mark.parametrize("has_primary_fact", [False, True])
@pytest.mark.parametrize("subject_name", ["Payment_Terms_Days", "Invoice Payment Period"])
def test_known_open_subject_is_consolidated_without_losing_unique_terms(
    has_primary_fact: bool,
    subject_name: str,
) -> None:
    from documents_analyst.graph.nodes.extract import _consolidate_known_subjects
    from documents_analyst_core.provenance.extract import ground_fact
    from documents_analyst_core.provenance.models import Citation
    from documents_analyst_core.provenance.open_subjects import OpenSubject, OpenSubjectResult

    quote = "Invoices are payable within forty-five (45) days."
    other = "Customer may audit records annually."
    body = quote + "\n" + other
    fact, _ = ground_fact(
        {
            "field_key": "payment_terms_days",
            "value_text": "45 days",
            "source_quote": quote[:-1],
            "start": 0,
            "end": len(quote) - 1,
        },
        document_id="source",
        text=body,
    )
    assert fact is not None
    facts = [fact] if has_primary_fact else []
    subjects = [
        OpenSubject(
            subject_name,
            "payable within forty-five (45) days",
            Citation("source", None, 0, len(quote), quote),
        ),
        OpenSubject(
            "Audit rights", "annually", Citation("source", None, len(quote) + 1, len(body), other)
        ),
    ]
    result = _consolidate_known_subjects(facts, OpenSubjectResult(subjects, [], 1), "source", body)
    assert len(facts) == 1
    assert facts[0].field_key == "payment_terms_days" and facts[0].value_normalized == "45"
    assert [s.subject for s in result.subjects] == ["Audit rights"]


def test_billing_period_is_not_routed_to_invoice_payment_deadline() -> None:
    from documents_analyst.graph.nodes.extract import _consolidate_known_subjects
    from documents_analyst_core.provenance.models import Citation
    from documents_analyst_core.provenance.open_subjects import OpenSubject, OpenSubjectResult

    quote = "Each payment period is one month, invoiced at the beginning of the period."
    body = quote + "\nInvoices are payable within 45 days."
    fact, _ = ground_fact(
        {
            "field_key": "payment_terms_days",
            "value_text": "45 days",
            "source_quote": body[len(quote) + 1 :],
        },
        document_id="source",
        text=body,
    )
    assert fact is not None
    facts = [fact]
    subject = OpenSubject(
        "Payment period", "one month", Citation("source", None, 0, len(quote), quote)
    )
    result = _consolidate_known_subjects(facts, OpenSubjectResult([subject], [], 1), "source", body)
    assert facts == [fact]
    assert result.subjects == [subject]


def test_a_name_match_with_the_wrong_kind_of_value_stays_an_open_subject() -> None:
    """ "Indemnity" names the yes/no field, but "mutual indemnification for third-party
    claims" is not a yes or no. Folded in, it became a second value beside the real
    answer and read as a disagreement."""
    from documents_analyst.graph.nodes.extract import _consolidate_known_subjects
    from documents_analyst_core.provenance.models import Citation
    from documents_analyst_core.provenance.open_subjects import OpenSubject, OpenSubjectResult

    body = "Each party shall provide mutual indemnification for third-party claims."
    subject = OpenSubject(
        "Indemnity",
        "mutual indemnification for third-party claims",
        Citation("source", None, 0, len(body), body),
    )
    facts: list[Any] = []
    result = _consolidate_known_subjects(facts, OpenSubjectResult([subject], [], 1), "source", body)
    assert facts == []
    assert result.subjects == [subject]


def test_a_monthly_fee_is_the_recurring_price_not_a_second_field() -> None:
    """The same fee extracted twice — once as Recurring price, once as an open subject
    "Monthly fee" — printed every agreement's price twice in the final document."""
    from documents_analyst.graph.nodes.extract import _consolidate_known_subjects
    from documents_analyst_core.provenance.models import Citation
    from documents_analyst_core.provenance.open_subjects import OpenSubject, OpenSubjectResult

    body = "The monthly fee for the Services is USD 12,000."
    subject = OpenSubject("Monthly fee", "USD 12,000", Citation("source", None, 0, len(body), body))
    facts: list[Any] = []
    result = _consolidate_known_subjects(facts, OpenSubjectResult([subject], [], 1), "source", body)
    assert [(f.field_key, f.value_normalized) for f in facts] == [("recurring_price", "12000 USD")]
    assert result.subjects == []
