"""The taxonomy is one list with two consumers — Phase 2B.

`FIELDS` in Python drives the extraction schema; `003_taxonomy_seed.sql` seeds
`contract_fields`. The SQL is **not** generated from the Python: migrations are
hand-written and checksummed, and generating one would make editing it trip the
integrity guard. So a test compares them instead.

Drift here is not cosmetic. `facts_and_claims.field_key` is a foreign key to
`contract_fields`, so a key the schema permits but the seed lacks produces
facts that pass every gate and then fail to insert.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from documents_analyst_core.taxonomy.fields import (
    FIELDS,
    GROUPS,
    UnknownField,
    by_key,
    is_known,
    keys,
)
from documents_analyst_core.taxonomy.schema import (
    MAX_QUOTE_CHARS,
    extraction_schema,
    party_pick_schema,
)

SEED = Path(__file__).resolve().parent.parent / "db" / "migrations" / "003_taxonomy_seed.sql"

ROW = re.compile(r"\('([a-z_]+)',\s*'([a-z]+)',\s*'([a-z_]+)',\s*'([^']*)',\s*'((?:[^']|'')*)'\)")


def seeded() -> list[tuple[str, str, str, str, str]]:
    body = SEED.read_text().split("VALUES", 1)[1]
    rows = ROW.findall(body)
    assert rows, "parsed no rows from the seed — the regex or the file changed"
    return [(k, g, v, label, desc.replace("''", "'")) for k, g, v, label, desc in rows]


# ── the two lists agree ───────────────────────────────────────────────────


def test_python_and_sql_declare_the_same_keys() -> None:
    sql_keys = [row[0] for row in seeded()]

    assert list(keys()) == sql_keys, "order matters too — it is the register's reading order"
    assert len(FIELDS) == 28


def test_every_field_matches_its_seeded_row_exactly() -> None:
    """Including the description. It goes into the extraction prompt verbatim,
    so a copy that drifted would mean the model is told one thing and the
    database documents another."""
    by_sql = {row[0]: row for row in seeded()}

    for field in FIELDS:
        _, group, value_type, label, description = by_sql[field.key]
        assert field.kind == value_type, f"{field.key}: kind"
        assert field.label == label, f"{field.key}: label"
        assert field.description == description, f"{field.key}: description"
        assert group in GROUPS


def test_the_seed_count_comment_is_still_true() -> None:
    assert "28 fields" in SEED.read_text()


# ── lookup ────────────────────────────────────────────────────────────────


def test_an_unknown_key_raises_rather_than_returning_none() -> None:
    """A None would be carried forward and fail at the database, several
    layers from the mistake."""
    with pytest.raises(UnknownField, match="not in the taxonomy"):
        by_key("late_fee")

    assert is_known("late_fee_amount")
    assert not is_known("late_fee")


def test_parties_is_the_only_multi_valued_field() -> None:
    assert [f.key for f in FIELDS if f.multi] == ["parties"]


# ── the generated schema ──────────────────────────────────────────────────


def test_field_key_is_an_enum_of_exactly_the_taxonomy() -> None:
    """This is what makes "the model cannot invent a field" a property of the
    request rather than something checked afterwards."""
    schema = extraction_schema()
    enum = schema["properties"]["facts"]["items"]["properties"]["field_key"]["enum"]

    assert enum == list(keys())


def test_the_schema_requires_a_quote_and_offsets() -> None:
    """A fact without offsets cannot pass Gate 1, so the schema refuses to
    produce one."""
    required = extraction_schema()["properties"]["facts"]["items"]["required"]

    assert set(required) == {"field_key", "value_text", "source_quote", "start", "end"}


def test_extra_keys_are_forbidden_at_both_levels() -> None:
    """Without this a model returns `{..., "notes": "I am unsure"}` and the
    extra key rides into storage unexamined."""
    schema = extraction_schema()

    assert schema["additionalProperties"] is False
    assert schema["properties"]["facts"]["items"]["additionalProperties"] is False


def test_the_quote_is_length_bounded() -> None:
    """Unbounded, models return a whole clause as the "quote" — grounded, and
    useless as provenance because it highlights half a page."""
    quote = extraction_schema()["properties"]["facts"]["items"]["properties"]["source_quote"]

    assert quote["maxLength"] == MAX_QUOTE_CHARS


def test_every_field_description_reaches_the_model() -> None:
    """The descriptions were written for the model, not for us. They are only
    worth that if they are actually in the request."""
    guide = extraction_schema()["properties"]["facts"]["items"]["properties"]["field_key"][
        "description"
    ]

    for field in FIELDS:
        assert field.key in guide
        assert field.description in guide


def test_the_generated_schema_is_valid_json_schema() -> None:
    """`check_schema` runs before every structured call, so an invalid one
    would fail every extraction at the first request."""
    from documents_analyst_core.portable.llm_client import check_schema

    check_schema(extraction_schema())
    check_schema(party_pick_schema(3))


# ── party picking is index-bounded ────────────────────────────────────────


def test_party_pick_schema_bounds_indices_to_the_candidate_list() -> None:
    """The model's output space is a closed set of spans found in the document,
    so it cannot return a party that is not there. Stronger than checking a
    returned name, which only catches inventions we thought to look for."""
    items = party_pick_schema(3)["properties"]["party_indices"]["items"]

    assert items["minimum"] == 0
    assert items["maximum"] == 2


def test_party_pick_schema_survives_an_empty_candidate_list() -> None:
    """A preamble the NER found nothing in must not produce a schema with
    `maximum: -1`, which is unsatisfiable and fails every call."""
    from documents_analyst_core.portable.llm_client import check_schema

    schema = party_pick_schema(0)
    check_schema(schema)
    assert schema["properties"]["party_indices"]["items"]["maximum"] == 0
