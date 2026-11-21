"""Section-aware chunking.

We write our own rather than using a generic text splitter, for one specific
reason: generic splitters return strings and discard character offsets. Gate 1
is defined in terms of offsets, so a splitter that loses them makes verbatim
verification impossible.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .sections import Section


@dataclass(frozen=True, slots=True)
class Chunk:
    ordinal: int
    text: str
    start: int
    end: int
    section_number: str | None
    section_heading: str | None
    context_header: str = ""
    """A short prefix ("MSA · 4.2 Payment Terms"), kept in its own column.
    Never part of `text`, and never counted in the offsets — otherwise every
    citation would be shifted by its length."""


CHARS_PER_TOKEN = 4
"""Deliberately a constant, not a tokenizer.

Chunk size decides how finely a document is mapped; being 15% out costs nothing
measurable. Importing a tokenizer to size a chunk would add a model load to a
pure function and make `parsing/` untestable without a download.
"""

_BOUNDARY = re.compile(r"(?<=[.;:!?])\s+|\n\n+")
"""Split at sentence ends and blank lines. A chunk that begins mid-clause makes
a worse map of the document than one that begins where a drafter began."""


def chunk_sections(
    text: str,
    sections: list[Section],
    *,
    target_tokens: int = 400,
    overlap_tokens: int = 60,
) -> list[Chunk]:
    """Chunks never cross a section boundary. A clause split across two
    sections would produce a citation pointing at two places at once.

    Every chunk's `text` is `text[start:end]` exactly — asserted by a test.
    That equality is the whole contract of this module: a chunk whose text has
    been cleaned, joined or stripped can no longer locate its own quote.
    """
    target = max(target_tokens, 1) * CHARS_PER_TOKEN
    overlap = min(max(overlap_tokens, 0) * CHARS_PER_TOKEN, target - 1)

    chunks: list[Chunk] = []
    for section in sections:
        for start, end in _windows(text, section.start, section.end, target, overlap):
            chunks.append(
                Chunk(
                    ordinal=len(chunks),
                    text=text[start:end],
                    start=start,
                    end=end,
                    section_number=section.number,
                    section_heading=section.heading,
                    context_header=context_header(section),
                )
            )
    return chunks


def _windows(text: str, start: int, end: int, target: int, overlap: int) -> list[tuple[int, int]]:
    """Offset pairs for one section, snapped to sentence boundaries."""
    if end <= start:
        return []
    if end - start <= target:
        return [(start, end)]

    spans: list[tuple[int, int]] = []
    cursor = start
    while cursor < end:
        stop = min(cursor + target, end)
        if stop < end:
            stop = _snap(text, cursor, stop, end)
        spans.append((cursor, stop))
        if stop >= end:
            break
        # Overlap is what stops a term stated across a sentence break from
        # falling into the gap between two chunks and being retrievable from
        # neither. `max(..., cursor + 1)` guarantees forward progress.
        cursor = max(stop - overlap, cursor + 1)
    return spans


def _snap(text: str, start: int, stop: int, limit: int) -> int:
    """Move `stop` back to the last sentence boundary in this window.

    Backwards, never forwards: reaching ahead for a boundary can overshoot the
    section and produce a chunk that crosses one, which is the single thing
    this module promises not to do.
    """
    window = text[start:stop]
    last = None
    for match in _BOUNDARY.finditer(window):
        last = match.end()
    if last is None or start + last <= start:
        return min(stop, limit)
    return min(start + last, limit)


def context_header(section: Section) -> str:
    """ "4.2 · Payment Terms" — stored beside the text, never inside it.

    It says where in the document a chunk came from, which is what makes a row
    reading "Net 30" locatable at all. Kept out of `text` because prepending it
    would shift every offset in the chunk by its own length, silently breaking
    Gate 1 for every fact inside.
    """
    parts = [p for p in (section.number, section.heading) if p]
    return " · ".join(parts)
