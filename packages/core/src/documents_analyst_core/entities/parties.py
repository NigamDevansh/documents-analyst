"""Who the parties are — candidates, normalise, match, pick.

The design point is `pick_parties`: it returns INDICES into the candidate list,
never strings. The model's output space is a closed set of verbatim spans, so it
physically cannot invent a party name — stronger than checking its answer after.

See docs/CORE_INTERNALS.md § "entities/parties.py".
"""

from __future__ import annotations

from documents_analyst_core.portable.llm_client import LLMClient, LLMError
from documents_analyst_core.portable.ner_gliner import NERClient, Span
from documents_analyst_core.prompts import parties as parties_prompt
from documents_analyst_core.provenance.normalize import normalize_entity_name
from documents_analyst_core.taxonomy.schema import party_pick_schema

LABELS = ["organization", "company"]
MAX_TOKENS = 500


def extract_party_candidates(
    preamble: str, ner: NERClient, *, threshold: float = 0.5
) -> list[Span]:
    """Preamble only — over a whole contract this catches every company named in
    an indemnity clause. Duplicates collapse on the normalised name, keeping the
    first occurrence (the full legal name, before any short form).

    Every span is re-checked against the preamble: `ner` is a protocol, so the
    guarantee "a party name is a substring of the document" must not depend on
    which implementation arrived — without it a client returning a span for a
    company that appears nowhere lands straight in the register.
    """
    if not preamble.strip():
        return []

    seen: set[str] = set()
    unique: list[Span] = []
    for span in sorted(ner.extract(preamble, LABELS, threshold=threshold), key=lambda s: s.start):
        if not is_real_span(preamble, span):
            continue
        key = normalize_entity_name(span.text)
        if not key or key in seen:
            continue
        seen.add(key)
        unique.append(span)
    return unique


def is_real_span(text: str, span: Span) -> bool:
    """The span must be where it says it is, and say what it says. Bounds first,
    because the slice alone lets a negative `start`, `start > end` or a clamped
    `end` accept a span whose offsets address something other than its name — and
    those offsets are what a reviewer clicks to see the party in context."""
    if not span.text:
        return False
    if not 0 <= span.start < span.end <= len(text):
        return False
    return text[span.start : span.end] == span.text


def normalize(names: list[str]) -> tuple[str, ...]:
    """Pure, no model. Suffix stripping and casefolding so "Acme Corp." and "ACME
    CORPORATION" match. Sorted, because a party pair is an unordered set — "Acme
    and Beta" and "Beta and Acme" are one agreement."""
    return tuple(sorted({n for n in (normalize_entity_name(x) for x in names) if n}))


def pick_parties(preamble: str, candidates: list[Span], llm: LLMClient) -> list[int]:
    """Returns indices into `candidates`. Never strings. Degrades to "all
    candidates" if the model is unreachable — an over-inclusive list is a matching
    problem the next stage can handle, where an empty one loses the agreement."""
    if not candidates:
        return []

    system, user = parties_prompt.build(preamble, [span.text for span in candidates])
    try:
        payload, _ = llm.structured(
            system=system,
            user=user,
            schema=party_pick_schema(len(candidates)),
            max_tokens=MAX_TOKENS,
        )
    except LLMError:
        return list(range(len(candidates)))

    # The schema bounds these already; re-checking costs nothing and stops a
    # provider whose "strict" mode slipped from causing an IndexError downstream.
    picked = payload.get("party_indices", [])
    return sorted({i for i in picked if isinstance(i, int) and 0 <= i < len(candidates)})


def party_names(candidates: list[Span], indices: list[int]) -> list[str]:
    """The verbatim spans for the chosen indices, in document order.

    The only way a name reaches the register: it is a substring of the preamble
    that the NER found, not anything the model typed.
    """
    return [candidates[i].text for i in sorted(indices) if 0 <= i < len(candidates)]


PROMPT_VERSION = parties_prompt.VERSION
"""Re-exported so a caller building a telemetry record does not need to know
which prompt module this stage uses."""
