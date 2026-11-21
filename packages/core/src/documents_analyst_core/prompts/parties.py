"""Choosing the contracting parties from NER candidates.

The model returns **indices**, never names, so its output space is a closed set
of spans that were found in the preamble. That guarantee lives in
`taxonomy.schema.party_pick_schema` and `entities.parties`; this module only
owns the wording.

It was the last prompt still written inline, inside `entities/parties.py`, with
its own unversioned system string and a hand-rolled `<preamble>` wrapper. That
is the shape this package exists to remove: one prompt outside the shared
injection boundary is one prompt nobody audits when the boundary changes.
"""

from __future__ import annotations

from collections.abc import Sequence

from .wrapping import system_preamble, wrap_untrusted

VERSION = "parties/1"


def build(preamble: str, candidate_names: Sequence[str]) -> tuple[str, str]:
    """Returns (system, user). Pairs with `party_pick_schema(len(candidates))`.

    The candidate list is numbered here and wrapped as untrusted content,
    because every name in it came out of the document. Numbering it inside the
    wrapper is safe: the model answers with indices, and the schema bounds them
    to the list length — a forged "9." inside a company name cannot become a
    selectable index.
    """
    system = f"{system_preamble()}\n\n{TASK}"
    numbered = "\n".join(f"{i}. {name}" for i, name in enumerate(candidate_names))
    user = "\n\n".join(
        [
            wrap_untrusted(preamble, label="PREAMBLE"),
            wrap_untrusted(numbered, label="CANDIDATES"),
        ]
    )
    return system, user


TASK = """\
You identify the contracting parties in a contract preamble.

The candidates are organisation names taken verbatim from the text, numbered
from 0. Reply with the indices of the ones that are actual parties to this
agreement. You cannot reply with a name — only an index — so if a party is not
in the list, it cannot be reported, and that is correct behaviour.

Exclude: regulators, courts, arbitration bodies, banks, insurers, addresses,
and companies merely mentioned in a clause. Most contracts have exactly two
parties; some have three. A list of five is almost always wrong.

If none of the candidates is a party, reply with an empty list."""
