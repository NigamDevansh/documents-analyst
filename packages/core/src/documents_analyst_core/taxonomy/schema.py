"""JSON Schema generated from `fields.FIELDS`.

`field_key` becomes an enum of exactly the known keys, which makes "the model
cannot invent a field" a property of the request, not something we check after.
Generated, not hand-written, so a schema that drifted from `FIELDS` cannot permit
a key that would fail at the database instead of the boundary.

See docs/CORE_INTERNALS.md § "taxonomy/fields.py".
"""

from __future__ import annotations

from typing import Any

from .fields import FIELDS, keys

MAX_QUOTE_CHARS = 600
"""A citation is a sentence or two, not a section. Unbounded, a model returns a
whole clause as the "quote" — grounded, but it highlights half a page in the
source pane, useless as provenance."""


def extraction_schema() -> dict[str, Any]:
    """Schema for one section's extraction call.

    Requires `source_quote`, `start` and `end` on every fact — a fact without
    offsets cannot pass Gate 1, so the schema refuses to produce one.

    `additionalProperties: false` at both levels is load-bearing: without it a
    model's extra key rides along into storage unexamined.
    """
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["facts"],
        "properties": {
            "facts": {
                "type": "array",
                "description": (
                    "Every material term stated in this section. Return an empty "
                    "array if the section states none — do not guess."
                ),
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["field_key", "value_text", "source_quote", "start", "end"],
                    "properties": {
                        "field_key": {
                            "type": "string",
                            "enum": list(keys()),
                            "description": _field_guide(),
                        },
                        "value_text": {
                            "type": "string",
                            "description": (
                                "The value exactly as the contract writes it — "
                                "'thirty (30) days', not '30'."
                            ),
                        },
                        "source_quote": {
                            "type": "string",
                            "maxLength": MAX_QUOTE_CHARS,
                            "description": (
                                "The shortest verbatim span of the document that states "
                                "this value. Copied character for character; it is checked "
                                "against the source and the fact is dropped if it differs."
                            ),
                        },
                        "start": {
                            "type": "integer",
                            "minimum": 0,
                            "description": "Character offset where source_quote begins.",
                        },
                        "end": {
                            "type": "integer",
                            "minimum": 1,
                            "description": "Character offset just past source_quote.",
                        },
                        "confidence": {
                            "type": "number",
                            "minimum": 0,
                            "maximum": 1,
                        },
                    },
                },
            }
        },
    }


def _field_guide() -> str:
    """The whole taxonomy, inline, as the enum's description. Each
    `FieldDef.description` is written for the model, so it sees what every key
    means in the same request that constrains it to those keys."""
    return "Which taxonomy field this fact states. " + " ".join(
        f"`{f.key}`: {f.description}" for f in FIELDS
    )


def party_pick_schema(candidate_count: int) -> dict[str, Any]:
    """Indices into a candidate list, never names. `maximum: count - 1` is the
    guarantee — the output space is a closed set of spans found in the document,
    so it cannot return a party that is not there. Checking a name afterwards only
    catches inventions we thought to look for."""
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["party_indices"],
        "properties": {
            "party_indices": {
                "type": "array",
                "maxItems": max(candidate_count, 1),
                "items": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": max(candidate_count - 1, 0),
                },
                "description": (
                    "Indices of the candidates that are actual contracting parties. "
                    "Exclude regulators, addresses, and companies merely mentioned."
                ),
            }
        },
    }


def adjudication_schema() -> dict[str, Any]:
    """Tier 3 of the classifier: is this a conflict, an amendment, or neither?

    `evidence_quote` is required whenever the verdict is `amendment`, and that
    is the whole safety property of this schema. An amendment is the fast lane —
    it arrives pre-filled and batch-approvable — so a model that could call
    something an amendment without pointing at the words "is hereby amended"
    would be able to wave a value change past a reviewer by asserting it.

    The schema cannot express "amendment, required-by-convention"; a caller
    still checks the quote verbatim. Requiring it here means the failure is a
    schema violation the client retries, rather than a missing key discovered
    three frames later.
    """
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["verdict", "reason"],
        "properties": {
            "verdict": {
                "type": "string",
                "enum": ["conflict", "amendment", "agree"],
                "description": (
                    "`conflict` — both are meant to be in force and they disagree. "
                    "`amendment` — the later document deliberately replaces the earlier; "
                    "only when the text says so. "
                    "`agree` — the values mean the same thing written differently."
                ),
            },
            "evidence_quote": {
                "type": "string",
                "description": (
                    "REQUIRED for `amendment`: the exact words from the later document "
                    "that say it replaces the earlier one. Copy them verbatim. If no such "
                    "words exist, the verdict is `conflict`, not `amendment`."
                ),
            },
            "reason": {
                "type": "string",
                "description": "One sentence, for a reviewer who has not read either document.",
            },
        },
    }


def judge_schema(rule_ids: tuple[str, ...]) -> dict[str, Any]:
    """Rulebook `judge` rules — the ones that need reading rather than arithmetic.

    `rule_id` is an **enum of the ids we sent**, which is what stops a model
    inventing a policy. `source_quote` is required and verified verbatim by the
    caller: "not mentioned" is not a violation, and a finding whose quote is not
    in the document is discarded rather than shown.

    No `severity` here on purpose. How serious a rule is was decided by whoever
    wrote the rulebook, and letting the model grade its own finding would make
    the policy negotiable by the document under review.
    """
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["findings"],
        "properties": {
            "findings": {
                "type": "array",
                "description": (
                    "One entry per rule that is VIOLATED. Return an empty array if none "
                    "are — a rule the document does not address is not a violation."
                ),
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["rule_id", "message", "source_quote"],
                    "properties": {
                        "rule_id": {"type": "string", "enum": list(rule_ids)},
                        "message": {
                            "type": "string",
                            "description": "Why this rule is violated, in one sentence.",
                        },
                        "source_quote": {
                            "type": "string",
                            "description": (
                                "The exact sentence from the document that shows the "
                                "violation, copied character for character. A finding "
                                "without one is discarded."
                            ),
                        },
                    },
                },
            }
        },
    }


def open_subject_schema() -> dict[str, Any]:
    """Material terms that the 28-key taxonomy has no room for.

    A closed taxonomy is what stops the model inventing fields, and it is worth
    keeping — but a pile whose side letter says "all customer data stored in the
    Frankfurt region" holds a real obligation that `field_key` cannot express.
    Refusing to record it means the register silently omits a term the contract
    actually states.

    So this is a **separate call with a separate schema**, and the separation is
    the safeguard: `subject` is free text here and nowhere else, these facts land
    in `subject_free` rather than `field_key`, and they are matched by embedding
    rather than by key. Nothing that reads the taxonomy can be confused by one.
    """
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["subjects"],
        "properties": {
            "subjects": {
                "type": "array",
                "description": (
                    "Material terms this section states that do NOT fit any taxonomy "
                    "field. Empty array is the normal answer — do not restate a term "
                    "the taxonomy already covers, and do not record background prose."
                ),
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["subject", "value_text", "source_quote", "start", "end"],
                    "properties": {
                        "subject": {
                            "type": "string",
                            "description": (
                                "A short noun phrase naming what this term is about — "
                                "'data residency region', 'audit rights'. Not a sentence."
                            ),
                        },
                        "value_text": {
                            "type": "string",
                            "description": "What the contract says it is, as written.",
                        },
                        "source_quote": {
                            "type": "string",
                            "description": "Copied character for character from the section.",
                        },
                        "start": {"type": "integer", "minimum": 0},
                        "end": {"type": "integer", "minimum": 0},
                    },
                },
            }
        },
    }
