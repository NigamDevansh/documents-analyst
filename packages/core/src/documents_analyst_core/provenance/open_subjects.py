"""Terms the taxonomy has no key for — same gates, different door.

`extract.py` runs the closed taxonomy. This runs the escape hatch, and it
reuses `extract._locate` rather than writing a second grounding path: the whole
"never bluffs" claim is one verbatim check at recorded offsets, and a second
implementation of it is a second place for it to be subtly weaker.

What differs is only what comes out. An `OpenSubject` has a free-text `subject`
instead of a `field_key`, no normalised value — normalising prose invents
structure — and it lands in `facts_and_claims.subject_free`, where it is matched
by embedding rather than by key.

**The point is that this stays rare.** A model asked "anything else?" always
finds something, so the prompt works hard to make the empty answer normal and
the gates here drop anything that arrives without a verbatim quote. A register
padded with restated boilerplate is worse than one missing a clause: it buries
the terms that matter.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass

from documents_analyst_core.parsing.sections import Section
from documents_analyst_core.portable.llm_client import LLMClient, LLMError
from documents_analyst_core.prompts import open_subjects as open_prompt
from documents_analyst_core.taxonomy.fields import FIELDS
from documents_analyst_core.taxonomy.schema import open_subject_schema

from .extract import MAX_TOKENS, Rejection, _locate
from .models import Citation


@dataclass(frozen=True, slots=True)
class OpenSubject:
    """A material term with no taxonomy key.

    No `value_normalized` field, deliberately — there is nothing to normalise
    to. A subject the taxonomy does not cover has no declared `ValueKind`, so
    any normalisation would be a guess about what kind of thing it is, made by
    the code least equipped to make it.
    """

    subject: str
    value_text: str
    citation: Citation


@dataclass(frozen=True, slots=True)
class OpenSubjectResult:
    subjects: list[OpenSubject]
    rejected: list[Rejection]
    sections_seen: int
    truncated: int = 0
    """Legacy telemetry counter; extraction no longer truncates subjects by count."""
    attempts: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    failed_sections: int = 0

    @property
    def complete(self) -> bool:
        return self.failed_sections == 0


def covered_fields() -> str:
    """The taxonomy, as one line per field, for the prompt's "do not repeat".

    Built here rather than in the prompt module so `prompts/` keeps knowing
    nothing about the taxonomy — the same direction of dependency `extract.py`
    already follows.
    """
    return "\n".join(
        f"- {f.key}: {f.description}"
        + (f" Also called: {', '.join(f.aliases)}." if f.aliases else "")
        for f in FIELDS
    )


def extract_open_subjects(
    *,
    document_id: str,
    text: str,
    sections: Iterable[Section],
    llm: LLMClient,
    max_sections: int | None = None,
    document_title: str = "",
    before_section: Callable[[], None] | None = None,
) -> OpenSubjectResult:
    """One call per section, then the same offset gate the taxonomy path uses.

    ``before_section`` lets the host renew a pile lease immediately before each
    bounded model request without coupling this portable module to the worker.
    """
    schema = open_subject_schema()
    covered = covered_fields()
    subjects: list[OpenSubject] = []
    rejected: list[Rejection] = []
    seen = attempts = sent = got = failed = 0

    for section in list(sections)[:max_sections]:
        body = text[section.start : section.end]
        if not body.strip():
            continue
        seen += 1

        label = " ".join(p for p in (section.number, section.heading) if p) or "(preamble)"
        try:
            if before_section is not None:
                before_section()
            system, user = open_prompt.build(
                body,
                doc_title=document_title or document_id,
                section_label=label,
                covered=covered,
            )
            payload, usage = llm.structured(
                system=system, user=user, schema=schema, max_tokens=MAX_TOKENS
            )
        except LLMError as exc:
            failed += 1
            rejected.append(Rejection("", "", f"section {section.number or '(preamble)'}: {exc}"))
            continue

        attempts += usage.attempts
        sent += usage.input_tokens
        got += usage.output_tokens

        for raw in payload.get("subjects", []):
            subject, why = ground_subject(
                raw, document_id=document_id, text=text, offset=section.start
            )
            if subject is not None:
                subjects.append(subject)
            else:
                rejected.append(
                    Rejection(str(raw.get("subject", "")), str(raw.get("source_quote", "")), why)
                )

    truncated = 0
    return OpenSubjectResult(subjects, rejected, seen, truncated, attempts, sent, got, failed)


def ground_subject(
    raw: dict[str, object], *, document_id: str, text: str, offset: int = 0
) -> tuple[OpenSubject | None, str]:
    """The same three gates, minus taxonomy closure — there is no key to close.

    What replaces it is a shape check on `subject`: a noun phrase, not a
    sentence. A model that answers the "what is this about" question with a
    restatement of the clause produces a register whose row labels are
    paragraphs, and there is no later stage that would notice.
    """
    subject = str(raw.get("subject", "")).strip()
    quote = str(raw.get("source_quote", ""))

    if not subject:
        return None, "no subject"
    if len(subject) > 80 or subject.count(" ") > 8:
        return None, f"subject {subject[:40]!r}… is a sentence, not a noun phrase"
    if not quote:
        return None, "no source_quote"

    start, end, relocated, why = _locate(raw, text, offset, quote)
    if start is None or end is None:
        return None, why

    value_text = str(raw.get("value_text", "")).strip()
    if not value_text:
        return None, "no value_text"

    # The document's bytes, for the same reason `ground_fact` uses them: a
    # relocation may have matched across a line break, and every reader of a
    # citation — the dashboard's highlight, the MCP approval gate — assumes
    # `source_quote == extracted_text[char_start:char_end]`. This path has its
    # own grounding and was the only one still storing the model's copy, which
    # is why 26 of 129 citations on a real pile failed that invariant while the
    # taxonomy path passed.
    quote = text[start:end]

    return (
        OpenSubject(
            subject=subject,
            value_text=value_text,
            citation=Citation(
                document_id=document_id,
                chunk_id=None,
                start=start,
                end=end,
                quote=quote,
                page_number=None,
                relocated=relocated,
            ),
        ),
        "",
    )


PROMPT_VERSION = open_prompt.VERSION
