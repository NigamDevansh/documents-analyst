"""Section text → grounded facts. Where Gate 1 actually runs, and the module the
"never bluffs" claim rests on — a mistake here produces a confident wrong
register, worse than a thin one.

Three gates, in order; a fact must pass all three:

  1. Taxonomy closure — `field_key` must exist in `FIELDS`. Belt to the schema's
     braces, since schema enforcement is a provider feature and they vary.
  2. Verbatim grounding — the quote must appear in the document, by exact string
     comparison. No fuzzy matching, no threshold.
  3. Offset agreement — the quote must be where the fact says, or relocatable to
     one unambiguous place.

Nothing here persists or reads configuration.

See docs/CORE_INTERNALS.md § "provenance/extract.py — the three gates".
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass

from documents_analyst_core.parsing.sections import Section
from documents_analyst_core.portable.llm_client import LLMClient, LLMError
from documents_analyst_core.portable.verify import find_quote, verify_quote
from documents_analyst_core.prompts import extraction as extraction_prompt
from documents_analyst_core.taxonomy.fields import ValueKind, by_key, is_known
from documents_analyst_core.taxonomy.schema import extraction_schema

from .models import Citation, GroundedFact
from .normalize import (
    normalize_date,
    normalize_duration_days,
    normalize_duration_hours,
    normalize_duration_months,
    normalize_money,
    normalize_percent,
)

MAX_TOKENS = 2000

DEDICATED_PATH_ONLY: dict[str, str] = {
    "parties": "use entities.parties.extract_party_candidates → pick_parties",
}
"""Taxonomy fields that generic extraction must NOT produce.

`parties` is grounded like anything else — a model can quote "the American
Arbitration Association" verbatim from a dispute-resolution clause and every
gate here passes it. Grounded is not the same as *contracting*, and this is the
one field where that difference matters.

The dedicated path closes it structurally: candidates come from NER over the
preamble, and the model picks **indices** into that list, so its output space is
a closed set of spans that were found where parties are named. Letting the
generic path also emit `parties` would leave a second door into the same field
with none of that constraint — the safeguard would be a convention rather than a
property, and conventions are discovered to have failed by someone else."""


@dataclass(frozen=True, slots=True)
class Rejection:
    """A fact the gates refused, and why. Returned, not logged: a silent drop and
    a section that stated nothing look identical, and the difference is what tells
    you extraction is working, so the count and reason travel with the result."""

    field_key: str
    quote: str
    reason: str


@dataclass(frozen=True, slots=True)
class ExtractionResult:
    facts: list[GroundedFact]
    rejected: list[Rejection]
    sections_seen: int
    attempts: int = 0
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def rejection_rate(self) -> float:
        total = len(self.facts) + len(self.rejected)
        return len(self.rejected) / total if total else 0.0


def extract_facts(
    *,
    document_id: str,
    text: str,
    sections: Iterable[Section],
    llm: LLMClient,
    max_sections: int | None = None,
    document_title: str = "",
    before_section: Callable[[], None] | None = None,
) -> ExtractionResult:
    """One structured call per section, then the gates. Per section, not per
    chunk: a chunk is sized for retrieval and can cut a clause in half, and half
    a clause is where a model invents the rest. `max_sections` is passed in,
    never read from the environment."""
    schema = extraction_schema()
    facts: list[GroundedFact] = []
    rejected: list[Rejection] = []
    seen = attempts = sent = got = 0

    for section in list(sections)[:max_sections]:
        body = text[section.start : section.end]
        if not body.strip():
            continue
        seen += 1

        try:
            # The portable core owns the per-section loop, while the host owns
            # operational concerns such as a pile lease. An optional callback
            # keeps that boundary intact and lets the host renew immediately
            # before each bounded model request.
            if before_section is not None:
                before_section()
            system, user = _render_section(section, body, document_title or document_id)
            payload, usage = llm.structured(
                system=system, user=user, schema=schema, max_tokens=MAX_TOKENS
            )
        except LLMError as exc:
            # One unusable section must not lose the whole document. It is
            # recorded as a rejection so the count cannot be mistaken for a
            # section that simply stated nothing.
            rejected.append(Rejection("", "", f"section {section.number or '(preamble)'}: {exc}"))
            continue

        attempts += usage.attempts
        sent += usage.input_tokens
        got += usage.output_tokens

        for raw in payload.get("facts", []):
            fact, why = ground_fact(raw, document_id=document_id, text=text, offset=section.start)
            if fact is not None:
                facts.append(fact)
            else:
                rejected.append(
                    Rejection(str(raw.get("field_key", "")), str(raw.get("source_quote", "")), why)
                )

    return ExtractionResult(facts, rejected, seen, attempts, sent, got)


def ground_fact(
    raw: dict[str, object],
    *,
    document_id: str,
    text: str,
    offset: int = 0,
) -> tuple[GroundedFact | None, str]:
    """Apply the three gates to one candidate. `(fact, "")` or `(None, why)`.

    `offset` is where the section began in the full document. The model is
    given one section and answers in that section's coordinates, so its numbers
    are shifted here — every stored offset indexes into the whole
    `extracted_text`, which is the only string Gate 1 knows about.
    """
    field_key = str(raw.get("field_key", ""))
    quote = str(raw.get("source_quote", ""))

    if not is_known(field_key):
        return None, f"{field_key!r} is not in the taxonomy"
    if field_key in DEDICATED_PATH_ONLY:
        return (
            None,
            f"{field_key!r} does not come from generic extraction — {DEDICATED_PATH_ONLY[field_key]}",
        )
    if not quote:
        return None, "no source_quote"

    start, end, relocated, why = _locate(raw, text, offset, quote)
    if start is None or end is None:
        return None, why

    value_text = str(raw.get("value_text", "")).strip()
    if not value_text:
        return None, "no value_text"

    # The document's bytes, not the model's copy: where they differ it is only
    # whitespace (relocation allows that alone), keeping the invariant every
    # reader depends on — source_quote == extracted_text[start:end]. The dashboard
    # highlights by those offsets and the MCP gate checks against the stored
    # quote; both would otherwise compare against a string the document lacks.
    quote = text[start:end]

    return (
        GroundedFact(
            field_key=field_key,
            value_text=value_text,
            value_normalized=_normalized(by_key(field_key).kind, value_text, quote),
            citation=Citation(
                document_id=document_id,
                chunk_id=None,
                start=start,
                end=end,
                quote=quote,
                page_number=None,
                relocated=relocated,
            ),
            confidence=_confidence(raw, relocated),
        ),
        "",
    )


def _locate(
    raw: dict[str, object], text: str, offset: int, quote: str
) -> tuple[int | None, int | None, bool, str]:
    """Where the quote really is, or why the fact cannot be kept. Models copy
    reliably and count badly, so when the stated offsets don't hold we look for
    the quote: one occurrence is taken, several are resolved by the model's
    nearest stated offset, and only a quote with no usable offset is dropped. A
    relocation is recorded (`citation.relocated`) and the confidence halved."""
    start, end = _int(raw.get("start")), _int(raw.get("end"))
    claimed: int | None = None
    if start is None or end is None:
        # -1 cannot pass the bounds check in `verify_quote`, so a fact with
        # missing or unparseable offsets falls straight through to relocation
        # rather than needing a branch of its own.
        start = end = -1
    else:
        start, end = start + offset, end + offset
        claimed = start

    if verify_quote(text, start, end, quote):
        return start, end, False, ""

    found = find_quote(text, quote)
    if found is not None:
        return found[0], found[1], True, ""

    if quote not in text:
        return None, None, False, "quote is not in the document"

    # Repeated quote. The model read one section and answered in its
    # coordinates, so its `start` — shifted to document coordinates above — says
    # which occurrence it meant even when the arithmetic around it is off by a
    # few characters. Take the nearest one.
    #
    # Dropping the fact instead is not the safe option it looks like. Every
    # occurrence is the same bytes, so the *value* is identical whichever we
    # pick and the citation still lands on text that reads exactly as quoted;
    # the worst case is a highlight on the second of two identical sentences.
    # Refusing costs the whole fact — and a term missing from the register is
    # a term that is never compared to the other documents, which is how a
    # priced amendment stops being a conflict and starts being silence.
    if claimed is None:
        # No usable offsets and several occurrences: nothing points at one of
        # them, and this is where guessing would start.
        return None, None, False, "quote appears more than once and no offset was given"

    nearest = _nearest_occurrence(text, quote, claimed)
    return nearest[0], nearest[1], True, ""


def _nearest_occurrence(text: str, quote: str, near: int) -> tuple[int, int]:
    """The occurrence of `quote` whose start is closest to `near`.

    Only called when the quote is known to occur at least twice, so the loop
    always has something to return. Ties go to the earlier occurrence, which
    keeps the choice deterministic across runs — a citation that moved between
    two identical passages on a re-run would look like a change that a reviewer
    then has to explain.
    """
    best = text.find(quote)
    index = text.find(quote, best + 1)
    while index != -1:
        if abs(index - near) < abs(best - near):
            best = index
        index = text.find(quote, index + 1)
    return best, best + len(quote)


def _int(value: object) -> int | None:
    """Models return offsets as ints, occasionally as strings. A non-integer is
    treated as absent and the quote relocated instead."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value)
    return None


def _confidence(raw: dict[str, object], relocated: bool) -> float:
    """A relocated fact is grounded but its citation was corrected by us. Halving
    keeps a repaired fact behind an unrepaired one for the same slot — a ranking
    nudge, nothing more. `citation.relocated` records the repair, because 0.5 is
    indistinguishable from a model that was genuinely half sure."""
    supplied = raw.get("confidence", 1.0)
    value = float(supplied) if isinstance(supplied, (int, float)) else 1.0
    value = min(max(value, 0.0), 1.0)
    return value * 0.5 if relocated else value


def _normalized(kind: ValueKind, value_text: str, quote: str) -> str | None:
    """Normalise the value; fall back to the quote it came from. Models return
    the answer, not the phrase — asked for payment terms they reply `"thirty"` as
    often as `"thirty days"`, and a bare number has no unit, so the normaliser
    refuses it and the fact carries `value_norm = NULL`, producing a confidently
    wrong finding against a contract that says thirty days. The quote is the safe
    second source (Gate 1 verified it verbatim), tried second because a clean
    value is what the model meant and disambiguates a quote with two numbers."""
    direct = normalize_for(kind, value_text)
    if direct is not None or not quote or quote == value_text:
        return direct
    return normalize_for(kind, quote)


def normalize_for(kind: ValueKind, value_text: str) -> str | None:
    """Comparable form, or None. None is a valid, common answer.

    The register always shows `value_text`; this exists only so two documents
    can be compared. Anything that fails to normalise still appears — it just
    does not take part in numeric comparison.
    """
    if kind == "money":
        money = normalize_money(value_text)
        return f"{money[0]} {money[1]}" if money else None
    if kind == "percent":
        percent = normalize_percent(value_text)
        return str(percent) if percent is not None else None
    if kind == "date":
        return normalize_date(value_text)
    if kind == "duration_days":
        days = normalize_duration_days(value_text)
        return str(days) if days is not None else None
    if kind == "duration_months":
        months = normalize_duration_months(value_text)
        return str(months) if months is not None else None
    if kind == "duration_hours":
        hours = normalize_duration_hours(value_text)
        return str(hours) if hours is not None else None
    if kind == "boolean":
        lowered = value_text.strip().casefold()
        if lowered in {"true", "yes", "present", "included", "required"}:
            return "true"
        if lowered in {"false", "no", "absent", "not present", "none"}:
            return "false"
        return None
    if kind == "enum":
        return value_text.strip().casefold().replace(" ", "_")
    # text and party_list: normalising would invent structure the contract
    # did not state. Comparison for these happens elsewhere, on the entity
    # normaliser or not at all.
    return None


def _render_section(section: Section, body: str, document_title: str) -> tuple[str, str]:
    """The (system, user) pair for one section, from `prompts/extraction.py`. The
    wording lives there, not here — this module owns the gates that judge what
    comes back, and a prompt in two places drifts in one. `test_prompts.py`
    snapshot-pins the text."""
    label = " ".join(p for p in (section.number, section.heading) if p) or "(preamble)"
    return extraction_prompt.build(body, doc_title=document_title, section_label=label)


PROMPT_VERSION = extraction_prompt.VERSION
"""Re-exported so a caller building a telemetry record does not have to know
which prompt module this stage uses."""
