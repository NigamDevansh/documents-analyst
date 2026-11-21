"""Fact extraction — the bulk of the token spend.

One call per section, not per document: a whole contract in one prompt loses
recall on later clauses, and per-chunk loses the clause context a value needs.

This module owns the wording. `provenance/extract.py` owns the gates that judge
what comes back, and imports from here rather than carrying its own copy — one
prompt, one place to diff, one snapshot test.
"""

from __future__ import annotations

from .wrapping import system_preamble, wrap_untrusted

VERSION = "extraction/1"


def build(section_text: str, *, doc_title: str, section_label: str) -> tuple[str, str]:
    """Returns (system, user). Pairs with `taxonomy.schema.extraction_schema()`.

    The instruction lives entirely in the system message and the document
    entirely in the user message. That split is what
    `test_instructions_are_composed_not_concatenated` asserts: no document text
    is ever appended to an instruction, so a section cannot extend the task it
    is supposed to be the subject of.

    `doc_title` and `section_label` are document-derived too — they come from a
    filename and a heading — so they are wrapped as untrusted content as well,
    in their **own** block.

    Keeping them out of the section block is not tidiness. The model returns
    character offsets into the text it was shown, and those offsets are shifted
    into whole-document coordinates by `provenance/extract.py`. Any preamble
    inside the same block would displace every offset by its own length, and
    the model would have to guess where "the text" started. One block, one
    coordinate system.
    """
    system = f"{system_preamble()}\n\n{TASK}"
    context = wrap_untrusted(f"{doc_title} — {section_label}", label="CONTEXT")
    body = wrap_untrusted(section_text, label="SECTION")
    return system, f"{context}\n\n{body}"


TASK = """\
You extract stated contract terms. You do not interpret, infer, or summarise.

Rules:
- Report only what the section states in words. If a term is not stated, omit
  it. An empty list is a correct answer, and a common one.
- `source_quote` must be copied character for character from the section, and
  must be the shortest span that states the value. It is checked against the
  source; a quote that is not verbatim causes the fact to be dropped.
- `start` and `end` are character offsets into the <untrusted-SECTION> text,
  counting from its first character. The <untrusted-CONTEXT> block is not part
  of that count.
- `value_text` is the value as the contract writes it — "thirty (30) days", not
  "30". Normalisation happens elsewhere.
- One fact per stated term. If a term is stated twice in this section, report
  the clearer statement once."""
