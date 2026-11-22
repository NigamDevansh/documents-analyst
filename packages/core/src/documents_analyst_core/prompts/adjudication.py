"""Tier 3 of the conflict classifier — and the amendment/conflict question.

The distinction it has to draw: two documents disagreeing is a CONFLICT, but a
later document deliberately superseding an earlier one is an AMENDMENT. Same
observable difference in values; opposite meaning to a reader. Effective dates
and amendment language are what separate them.
"""

from __future__ import annotations

from typing import Any

from .wrapping import system_preamble, wrap_untrusted

VERSION = "adjudication/4"
"""2 · dropped `effective_from`, which the column could never fill.

`facts_and_claims.effective_from` was never written by anything, so this line
read `effective_from: not stated` on every adjudication ever made — a field the
model was told to weigh and that could only ever say nothing. The column went in
the schema and the line with it."""


def build(
    field_label: str,
    left: dict[str, Any],
    right: dict[str, Any],
    *,
    doc_relationship: str,
) -> tuple[str, str]:
    """Returns (system, user).

    Both sides arrive as quotes from real documents, so both are wrapped. The
    *relationship* between the documents — which is ours, derived from the
    register, not from the text — goes in the system message where it is
    authoritative.
    """
    system = f"{system_preamble()}\n\n{TASK}\n\nRelationship: {doc_relationship}"
    user = "\n\n".join(
        [
            f"Field: {field_label}",
            wrap_untrusted(_render(left), label="SIDE_A"),
            wrap_untrusted(_render(right), label="SIDE_B"),
        ]
    )
    return system, user


def _render(side: dict[str, Any]) -> str:
    lines = [
        f"document: {side.get('document', '?')}",
        f"value: {side.get('value_text', '')}",
        f"quote: {side.get('source_quote', '')}",
    ]
    if side.get("context"):
        lines.append(f"paragraph: {side['context']}")
    return "\n".join(lines)


TASK = """\
Two documents in one agreement state different values for the same field.
Decide which of these it is:

- `conflict`     — both are meant to be in force, and they disagree. Someone
                   has to choose.
- `amendment`    — the later document deliberately replaces the earlier. Look
                   for language that replaces, amends or supersedes the earlier
                   term, or an effective date that plainly follows.
- `agree`        — the two are compatible: different scopes, different
                   periods, or the same value written differently.

Each side shows its value, the quote it came from, and the paragraph around it.

Rules:
- An amendment needs EVIDENCE. Copy the words that make it one character for
  character from SIDE_B's quote or paragraph — never paraphrase, and never
  write words that are not there. A quote that is not in that document
  invalidates the verdict, and the verdict is discarded.
- The evidence must change THIS field. A general clause that a document
  "supersedes conflicting terms" or "prevails" is not evidence for any one
  field on its own.
- A document's own effective date (the day an amendment or letter starts to
  apply) is not a change to the agreement's effective date.
- A later effective date alone is not an amendment. Two live contracts can have
  different dates and still both apply.
- If the evidence is not there, say `conflict` and let a human decide. Guessing
  `amendment` silently deletes one of the two values from the register."""
