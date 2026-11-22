"""Slots → the text of the final document.

One function decides what a row says (`body`), and everything that writes or verifies the
document goes through it: the commit that writes `final-document.docx`, the resolve step
that repairs a missing file, and the reconcile step that reads the file back. Two places
deciding the same text independently is how a document starts to disagree with itself.

The document's shape: a title, a preamble, then one heading per agreement and one
sub-heading + paragraph per term. One heading per term rather than a table, because a
section is the unit the system tracks, hashes and proves unchanged — a table would make
the whole register one unit.
"""

from __future__ import annotations

from .slots import Slot

DISPUTED = "⚠ sources disagree — awaiting review"
"""What a disputed slot shows instead of a value. Not the first value with a warning
beside it: a register that displays one side of a disagreement has taken a position, and a
reader skimming it would carry away a number nobody agreed. Showing the disagreement is the
honest render."""

ORPHANED = "⚠ source document was removed"
"""Appended to a row whose sources have all left the folder. Appended, not substituted:
the value was extracted from a real document and verified against it, and a reader who saw
it yesterday may have acted on it — replacing it with a warning would erase what the
register said without anybody deciding to. Removing the row is a separate `orphaned_claims`
decision a human approves."""

PREAMBLE = (
    "This document is generated and kept up to date automatically from the "
    "source documents in this folder. Every value is maintained from source, and "
    "changes are applied only after human review. It is not a legal opinion and "
    "certifies nothing."
)
"""Sits in the document itself, not only in the UI. The document is forwarded, so the
caveat has to travel with it — a disclaimer that lives only in the app is one the
recipient never sees."""


def title(pile_name: str) -> str:
    return f"Final Document — {pile_name}"


def body(slot: Slot) -> str:
    """The row's text. The orphan marker is appended HERE, so every write carries it —
    a warning that lived anywhere else would be dropped by the next render, and the row
    would go back to stating a deleted contract's term as though the file were still
    there."""
    current = slot.current
    if slot.state in ("disputed", "contested"):
        options = "; ".join(f"{v.value_text} ({v.citation_count} source(s))" for v in slot.values)
        text = f"{DISPUTED} — {options}" if options else DISPUTED
    elif current is None:
        # No live values at all. The row is empty rather than orphaned, and appending the
        # marker to nothing would state it twice.
        return ORPHANED
    else:
        corroboration = (
            f" (stated in {len(current.document_ids)} documents)"
            if len(current.document_ids) > 1
            else ""
        )
        text = f"{current.value_text}{corroboration}"
    text = f"{text} — {ORPHANED}" if slot.orphaned else text
    if slot.resolved_by and slot.state == "resolved":
        text += f"\nReviewed choice: {slot.resolved_by}."
    sources = [source for value in slot.values for source in value.citations]
    if sources:
        text += "\nSources (verbatim evidence):" + "".join(
            f"\n- {source['filename']}, chars {source['start']}–{source['end']}: “{source['quote']}”"
            for source in sources
        )
    return text
