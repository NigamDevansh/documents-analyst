"""The second extraction pass — material terms the taxonomy has no key for.

A closed taxonomy is what stops a model inventing fields, and it stays closed.
But a side letter saying "all customer data to be stored in the Frankfurt
region" states a real obligation that none of the 28 keys can express, and a
register that silently drops it is a register that is quietly incomplete.

So the escape hatch is a **separate call with its own prompt and schema**, and
the separation is the safeguard rather than a convenience. These facts land in
`subject_free`, never `field_key`; they are matched by embedding, never by key;
and nothing that reads the taxonomy can be handed one by mistake.

The prompt's whole job is to make the answer usually empty. A model asked
"anything else?" will always find something, and a register full of restated
background prose is worse than one missing a clause — it buries the terms that
matter under the ones that do not.
"""

from __future__ import annotations

from .wrapping import system_preamble, wrap_untrusted

VERSION = "open_subjects/1"


def build(
    section_text: str, *, doc_title: str, section_label: str, covered: str
) -> tuple[str, str]:
    """Returns (system, user). Pairs with `taxonomy.schema.open_subject_schema()`.

    `covered` is the taxonomy's own field list, passed in rather than imported,
    so the prompt module keeps knowing nothing about the taxonomy. It goes in
    the **system** message: what the taxonomy already handles is our
    instruction, not something the document gets a say in.

    Same split as `extraction.build` — instruction in the system message,
    document in the user message, context in its own wrapped block so the
    section's coordinate system starts at character zero.
    """
    system = f"{system_preamble()}\n\n{TASK}\n\nAlready covered, do not repeat:\n{covered}"
    context = wrap_untrusted(f"{doc_title} — {section_label}", label="CONTEXT")
    body = wrap_untrusted(section_text, label="SECTION")
    return system, f"{context}\n\n{body}"


TASK = """\
You find material contract terms that a fixed list of fields does not cover.

An empty answer is the normal, correct answer. Most sections state nothing that
the list below misses, and returning something anyway makes the report worse.

Return a term only when ALL of these hold:

- It is an obligation, a restriction, a right, or a stated quantity.
- It is stated in this section, not implied by it.
- None of the covered fields express it.
- A reader deciding whether to sign would want it in a summary.

Do NOT return:

- Background, recitals, definitions, or boilerplate.
- A restatement of a covered field under a different name.
- Anything you had to infer, combine, or read between the lines for.

For each term you do return:

- `subject` is a short noun phrase naming what it is about — "data residency
  region", "audit rights", "insurance minimum". Not a sentence, not a value.
- `value_text` is what the contract says it is, in the contract's own words.
- `source_quote` is copied character for character from the section. It must
  appear in the section exactly as you write it. If you cannot copy it exactly,
  leave the term out.
- `start` and `end` are that quote's position in the section text you were
  shown, counting from zero.
"""
