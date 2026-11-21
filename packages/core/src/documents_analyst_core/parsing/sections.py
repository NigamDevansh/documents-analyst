"""Split a contract into numbered sections.

Heading detection is heuristic and offsets are exact. Those two facts sit
together on purpose: getting a section boundary slightly wrong costs retrieval
quality, whereas getting an offset wrong breaks Gate 1. Only one of those is
allowed to be approximate.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Section:
    number: str | None
    """As printed — "4.2", "IV", "(c)". Not renumbered by us."""
    heading: str | None
    start: int
    end: int
    level: int


HEADING = re.compile(
    r"""^
    (?P<number>
        \d+(?:\.\d+)*\.           |   # 4.  ·  4.2.   — trailing dot
        \d+(?:\.\d+)+             |   # 4.2 ·  4.2.1  — dotted, no trailing dot
        \d+(?=:)                  |   # 4:
        [IVXLC]+\.                |   # IV.
        \([a-z]\)                 |   # (c)
        (?:ARTICLE|SECTION|SCHEDULE|EXHIBIT|APPENDIX|ANNEX)\s+[\w.]+
    )
    [\s.:—-]+                    # separator, including an em dash
    (?P<heading>\S.*?)                # the title itself
    \s*$""",
    re.VERBOSE | re.IGNORECASE,
)
"""A bare integer must carry punctuation to count as a section number.

`4 Payment Terms` and `200 Freight Lane` are the same shape, and only one of
them is a heading. Accepting the bare form parsed the second line of every
block-formatted address — `200 Freight Lane`, `100 Industrial Way` — as the
start of a new section, which truncated the preamble in the middle of the party
block: the window closed after the first party was named and before the second
one was. Party extraction then read one party instead of two, the document was
filed under a party set that matched nothing else, and it was never compared to
the contract it belonged to. A missed heading costs retrieval quality; a missed
party costs every conflict in the agreement.
"""

MAX_HEADING_CHARS = 120
"""A heading is a label, not a sentence.

Contracts open clauses with numbered prose — "4.2 The Supplier shall maintain
insurance of not less than …" — which matches the pattern perfectly. Length is
what separates a heading from a numbered paragraph, and getting it wrong merges
or shatters sections without ever being visibly wrong.
"""


def _level(number: str) -> int:
    """Depth from the dotted number. `4` → 1, `4.2` → 2, `4.2.1` → 3."""
    core = number.rstrip(".")
    if re.fullmatch(r"\d+(?:\.\d+)*", core):
        return core.count(".") + 1
    return 1


def split_sections(text: str, *, min_chars: int = 120) -> list[Section]:
    """Sections cover the text with no gaps: a preamble with no heading is
    still a Section, with `number=None`. A gap would mean text that no chunk
    can cite.

    `min_chars` merges a heading into the one above when the body between them
    is tiny — a title page of six one-line headings otherwise becomes six
    sections holding no terms, and every retrieval hit lands on a heading.
    """
    if not text:
        return []

    starts: list[tuple[int, str, str, int]] = []
    offset = 0
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        match = HEADING.match(stripped) if 0 < len(stripped) <= MAX_HEADING_CHARS else None
        if match:
            number = match.group("number").strip()
            heading = match.group("heading").strip()
            starts.append((offset, number, heading, _level(number)))
        offset += len(line)

    if not starts:
        return [Section(number=None, heading=None, start=0, end=len(text), level=1)]

    sections: list[Section] = []
    if starts[0][0] > 0:
        # Everything before the first heading is the preamble. It holds the
        # parties, so it is never dropped for being unnumbered.
        sections.append(Section(None, None, 0, starts[0][0], 1))

    for index, (start, number, heading, level) in enumerate(starts):
        end = starts[index + 1][0] if index + 1 < len(starts) else len(text)
        sections.append(Section(number, heading, start, end, level))

    return _merge_runts(sections, min_chars)


def _merge_runts(sections: list[Section], min_chars: int) -> list[Section]:
    """Absorb a too-short section into its predecessor, keeping coverage exact.

    The merged section keeps the EARLIER heading, because that is the one a
    citation will be displayed under and the one a reader was last told about.
    """
    merged: list[Section] = []
    for section in sections:
        too_short = section.end - section.start < min_chars
        # Never absorb a NUMBERED section into the unnumbered preamble. The
        # preamble has no number to keep, so the merge would silently delete
        # "5." from the document's own reference scheme — and a citation that
        # cannot name its clause is most of the value of a citation.
        into_preamble = bool(merged) and merged[-1].number is None and section.number is not None
        if too_short and merged and not into_preamble:
            previous = merged[-1]
            merged[-1] = Section(
                previous.number, previous.heading, previous.start, section.end, previous.level
            )
            continue
        merged.append(section)
    return merged


MAX_PREAMBLE_CHARS = 1200
"""A hard ceiling on the party window, for documents that have no headings.

An invoice or a letter is often one unnumbered block from top to bottom, so
"everything before the first numbered section" is the entire document — and
party extraction over a whole document is the exact failure the window exists
to prevent: the remittance block at the bottom of an invoice names a bank, the
bank becomes a third party, and the document is filed under a party set no
other document shares.

Generous on purpose. A preamble names its parties in the first paragraph or
two; 1200 characters clears that several times over while still stopping well
short of the payment details, the indemnity, and the signature block.
"""


def find_preamble(text: str, sections: list[Section]) -> tuple[int, int]:
    """The party-identification window — the front matter plus the first
    numbered section. Bounding it matters: party extraction over a whole
    contract picks up every company mentioned in an indemnity clause.

    Both real shapes have to work, and they disagree about where the parties
    sit. Some contracts open straight into "1. Parties"; most open with a title
    line ("MASTER SERVICES AGREEMENT") and name the parties inside section 1
    below it. Taking only the front matter would hand the second shape its title
    and nothing else, so every document would resolve to no parties and the
    whole pile would collapse into one unassigned agreement — unrelated
    contracts then compare against each other and every differing term reads as
    a conflict.

    So the window runs from the top of the document to the END of the first
    numbered section, which covers both shapes and still stops well before the
    indemnity. A document that numbers nothing gets the capped opening instead.

    Capped at `MAX_PREAMBLE_CHARS` either way, because "before the first
    numbered section" is unbounded in a document that never numbers one.
    """
    if not text:
        return (0, 0)
    if not sections:
        return (0, min(len(text), MAX_PREAMBLE_CHARS))

    start = sections[0].start
    first_numbered = next((s for s in sections if s.number is not None), None)
    # Nothing is numbered: the capped opening is the only safe bound.
    end = first_numbered.end if first_numbered is not None else len(text)
    return (start, min(end, start + MAX_PREAMBLE_CHARS))
