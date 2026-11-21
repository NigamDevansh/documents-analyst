"""What KIND of contract a document is, so two kinds are not compared.

Party set alone decides too much: `agreements` is keyed on it, but two companies
sign more than one contract, so an MSA and an NDA between the same pair became
one agreement and their different terms were reported as conflicts. A dependent
document is not a different kind, though — an amendment must be compared with what
it amends, so it takes its parent's family.

Two classes:

  * standalone — NDA, lease, employment agreement. Its own family.
  * dependent — amendment, side letter, SOW, exhibit. Takes its parent's family
    when the title names one ("Amendment No. 2 to Supply Agreement"), else none,
    which the caller resolves against the pile's existing agreement.

Pure and offline: keywords over the opening text, no model — a judgement call
would make a wrong split unreviewable, the same reason Gate 1 has no threshold.
"""

from __future__ import annotations

import re

TITLE_WINDOW = 1500
"""How much of the document to read for a STANDALONE type. Contracts do not
always carry a heading — this one opens `1. Parties and Agreement` and only
names itself in the recital below — so the type has to be looked for in the
opening prose too. Past that a supply agreement starts discussing
confidentiality and every keyword appears somewhere."""

_SELF_DESCRIBED = re.compile(
    r"\bthis\s+(?:amendment|addendum|side\s+letter|statement\s+of\s+work"
    r"|work\s+order|change\s+order|order\s+form)\b",
    re.IGNORECASE,
)
"""A document calling ITSELF a dependent: "**This Amendment** No. 1 to …".

Dependency cannot be decided by looking for the word anywhere — every contract
mentions exhibits, schedules and amendments in its body, and a Master Services
Agreement whose preamble referred to an exhibit was classified as a dependent,
putting the MSA in one agreement and its own SOW, invoice and amendment in
another. Nor by the title line alone: this pile's amendment opens `1. Parties
and Background` and only says what it is in the sentence below.

What separates them is self-reference. `This Amendment modifies…` is the
document describing itself; `Any amendment must be in writing` is a clause about
future amendments, and only the first begins with `this`."""

_TITLE_DEPENDENT = re.compile(
    r"^\W*(?:amendment|addendum|side\s+letter|statement\s+of\s+work|invoice"
    r"|work\s+order|change\s+order|order\s+form|exhibit|schedule)\b",
    re.IGNORECASE,
)
"""…or announcing it as the very first thing on the page: `INVOICE`,
`AMENDMENT NO. 2 TO SUPPLY AGREEMENT`."""

_STANDALONE: tuple[tuple[str, str], ...] = (
    ("non-disclosure", "nda"),
    ("nondisclosure", "nda"),
    ("confidentiality agreement", "nda"),
    ("employment agreement", "employment"),
    ("offer letter", "employment"),
    ("severance agreement", "employment"),
    ("lease agreement", "lease"),
    ("lease", "lease"),
    ("loan agreement", "loan"),
    ("credit agreement", "loan"),
    ("promissory note", "loan"),
    ("supply agreement", "supply"),
    ("purchase agreement", "purchase"),
    ("distribution agreement", "distribution"),
    ("reseller agreement", "distribution"),
    ("license agreement", "licence"),
    ("licence agreement", "licence"),
    ("consulting agreement", "consulting"),
    ("master services agreement", "services"),
    ("services agreement", "services"),
    ("service agreement", "services"),
    ("master agreement", "services"),
)
"""Ordered: the first match wins, so the more specific phrase comes first.
`master services agreement` must be tried before `services agreement`, and
`lease agreement` before the bare `lease` that would also match "leasehold"."""

_PARENT = re.compile(
    r"\b(?:to|under|of|amends?|amending)\s+(?:that\s+certain\s+|the\s+|this\s+)?"
    r"(?P<name>[A-Za-z ]{0,40}?agreement)\b",
    re.IGNORECASE,
)
"""'Amendment No. 2 **to** the Supply **Agreement**' — the phrase that names a
dependent's parent. Bounded so it cannot run across a sentence into an
unrelated agreement mentioned later."""


def detect_family(text: str) -> str | None:
    """The contract family, or None when the document does not declare one.

    None is a real answer with a defined meaning: *this document does not stand
    on its own*. The caller joins it to the pile's existing agreement for the
    same parties rather than starting a rival one — which is what keeps a bare
    "Statement of Work No. 3" with the master agreement it implements.
    """
    head = text[:TITLE_WINDOW].casefold()
    if not head.strip():
        return None

    title = next((line for line in head.splitlines() if line.strip()), "")
    if _SELF_DESCRIBED.search(head) or _TITLE_DEPENDENT.match(title):
        parent = _PARENT.search(head)
        # The parent's own name is classified with the standalone table, so
        # "Amendment No. 2 to Supply Agreement" and "Supply Agreement" agree.
        return _standalone(parent.group("name")) if parent else None

    return _standalone(head)


def _standalone(text: str) -> str | None:
    for phrase, family in _STANDALONE:
        if phrase in text:
            return family
    return None
