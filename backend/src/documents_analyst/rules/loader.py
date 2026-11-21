"""YAML → typed Rule objects, validated at load.

A malformed rulebook fails loudly at pile scan, not silently at audit time as
"zero findings" — which is indistinguishable from a clean contract.

The fixture YAML is canonical: `fixtures/*/rulebook.yaml` and the architecture doc define
the shape, and this module matches them rather than the other way round.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml

Mode = Literal["check", "judge"]
"""`check` is SQL over `facts_and_claims` and cannot invent a finding.
`judge` asks a model and must return a verbatim quote or be discarded."""

Severity = Literal["low", "medium", "high"]

Assertion = Literal["exists", "absent", "equals", "in", "compare"]
"""Five operators, deliberately. Richer rules are future work, and every one of
these is decidable from the fact store without a model."""

Comparison = Literal["<", "<=", "==", ">=", ">"]


@dataclass(frozen=True, slots=True)
class Rule:
    rule_id: str
    """From `id:` in YAML. A closed list — the model is given these and anything outside
    them is dropped, so a rule can't be invented."""
    title: str
    mode: Mode
    severity: Severity
    rationale: str = ""

    # ── check rules ───────────────────────────────────────────────────────
    assertion: Assertion | None = None
    """From `assert:` in YAML. `assert` is a Python keyword, so the field is
    renamed here and only here."""
    field_key: str | None = None
    """From `field:`."""
    expected: str | None = None
    """`equals` only."""
    values: tuple[str | int | float, ...] = ()
    """`in` only — e.g. `[30, 45]` for the payment-terms policy."""
    left: str | None = None
    op: Comparison | None = None
    right: str | None = None
    """`compare` only. `right` may be an expression over field keys, e.g.
    `"recurring_price * 12"`, evaluated against normalised values."""

    # ── judge rules ───────────────────────────────────────────────────────
    question: str | None = None
    section_hint: str | None = None
    requires_citation: bool = True
    """A judge finding whose quote fails the verbatim gate is discarded and
    logged, never shown."""


class RulebookError(Exception):
    """Raised at load. Never deferred to audit time, where an unparsed rulebook
    would look exactly like a compliant contract."""


def load(path: Path) -> list[Rule]:
    """Parse and validate. Raises `RulebookError` with the rule id and the problem — a
    rulebook that half-loads is worse than one that refuses to. Validated per mode: a
    `check` rule needs the fields its `assert` uses, a `judge` rule needs a `question`.
    Duplicate ids are rejected, because `findings` is UNIQUE(agreement_id, rule_id) and a
    duplicate would make one rule silently overwrite the other's dismissal. `safe_load`,
    never `load`: a rulebook is a file someone drops into a watched folder, and PyYAML's
    full loader constructs arbitrary Python objects from it — RCE reached by copying a
    file into a directory."""
    try:
        raw = yaml.safe_load(path.read_text()) or {}
    except OSError as exc:
        raise RulebookError(f"{path}: could not be read — {exc}") from exc
    except yaml.YAMLError as exc:
        raise RulebookError(f"{path}: is not valid YAML — {exc}") from exc

    if not isinstance(raw, dict) or "rules" not in raw:
        raise RulebookError(f"{path}: expected a top-level `rules:` list")
    entries = raw["rules"]
    if not isinstance(entries, list):
        raise RulebookError(f"{path}: `rules:` must be a list, got {type(entries).__name__}")

    rules: list[Rule] = []
    seen: set[str] = set()
    for index, entry in enumerate(entries):
        rule = _one(entry, index, path)
        if rule.rule_id in seen:
            # `findings` is UNIQUE (agreement_id, rule_id). Two rules sharing an id would
            # make one silently overwrite the other's row — including its dismissal, so a
            # finding someone had dealt with would come back wearing the other's message.
            raise RulebookError(f"{path}: duplicate rule id {rule.rule_id!r}")
        seen.add(rule.rule_id)
        rules.append(rule)
    return rules


def _one(entry: object, index: int, path: Path) -> Rule:
    where = f"{path}: rule #{index + 1}"
    if not isinstance(entry, dict):
        raise RulebookError(f"{where}: expected a mapping, got {type(entry).__name__}")

    rule_id = str(entry.get("id") or "").strip()
    if not rule_id:
        raise RulebookError(f"{where}: needs an `id`")
    where = f"{path}: rule {rule_id}"

    mode = entry.get("mode")
    if mode not in ("check", "judge"):
        raise RulebookError(f"{where}: `mode` must be 'check' or 'judge', got {mode!r}")
    severity = entry.get("severity")
    if severity not in ("low", "medium", "high"):
        raise RulebookError(f"{where}: `severity` must be low/medium/high, got {severity!r}")

    common = {
        "rule_id": rule_id,
        "title": str(entry.get("title") or rule_id),
        "mode": mode,
        "severity": severity,
        "rationale": str(entry.get("rationale") or "").strip(),
    }
    return _judge(entry, where, common) if mode == "judge" else _check(entry, where, common)


def _check(entry: dict[str, Any], where: str, common: dict[str, Any]) -> Rule:
    """A `check` rule needs exactly the fields its assertion uses. Validated per assertion,
    not "any of these may be present", because a missing field makes a rule evaluate to
    nothing — and a rule that never fires is indistinguishable from a contract that
    complies."""
    assertion = entry.get("assert")
    if assertion not in ("exists", "absent", "equals", "in", "compare"):
        raise RulebookError(
            f"{where}: `assert` must be one of exists/absent/equals/in/compare, got {assertion!r}"
        )

    field_key = entry.get("field")
    values = entry.get("values")
    left, op, right = entry.get("left"), entry.get("op"), entry.get("right")

    if assertion in ("exists", "absent", "equals", "in") and not field_key:
        raise RulebookError(f"{where}: `assert: {assertion}` needs a `field`")
    if assertion == "equals" and entry.get("expected") is None:
        raise RulebookError(f"{where}: `assert: equals` needs an `expected`")
    if assertion == "in" and (not isinstance(values, list) or not values):
        raise RulebookError(f"{where}: `assert: in` needs a non-empty `values` list")
    if assertion == "compare":
        if not left or not right:
            raise RulebookError(f"{where}: `assert: compare` needs `left` and `right`")
        if op not in ("<", "<=", "==", ">=", ">"):
            raise RulebookError(f"{where}: `op` must be a comparison, got {op!r}")

    return Rule(
        **common,
        assertion=assertion,
        field_key=str(field_key) if field_key else None,
        expected=str(entry["expected"]) if entry.get("expected") is not None else None,
        values=tuple(values) if isinstance(values, list) else (),
        left=str(left) if left else None,
        op=op,
        right=str(right) if right else None,
    )


def _judge(entry: dict[str, Any], where: str, common: dict[str, Any]) -> Rule:
    question = str(entry.get("question") or "").strip()
    if not question:
        raise RulebookError(f"{where}: `mode: judge` needs a `question`")
    return Rule(
        **common,
        question=question,
        section_hint=str(entry["section_hint"]) if entry.get("section_hint") else None,
        requires_citation=bool(entry.get("requires_citation", True)),
    )


def rule_ids(rules: list[Rule]) -> tuple[str, ...]:
    """The closed set handed to the judge prompt. Anything the model returns
    outside this is dropped in `evaluator.py`."""
    return tuple(r.rule_id for r in rules)
