"""The rulebook — Phase 4B. Pure, no database, no model.

That is the point of this file rather than an accident of scope. The check
operators are the half of compliance that a document under review cannot argue
with, and every test here runs without a model to prove it: if any of these
needed one, the injection claim in `evaluator.py` would be prose rather than a
property.

The arithmetic is taken from `fixtures/ANSWER_KEY.json`, including the case that
passes by exactly zero.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from documents_analyst.rules.evaluator import FactView, Violation, evaluate, postprocess_judge
from documents_analyst.rules.loader import RulebookError, load, rule_ids

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
RULEBOOK = FIXTURES / "conflict_pile" / "rulebook.yaml"


def fact(field_key: str, norm: str | None, text: str = "", *, at: int = 0) -> FactView:
    return FactView(
        field_key=field_key,
        value_norm=norm,
        value_text=text or (norm or ""),
        document_id="doc-1",
        char_start=at,
        char_end=at + 10,
        source_quote="the clause",
    )


def rules() -> dict[str, object]:
    return {r.rule_id: r for r in load(RULEBOOK)}


# ── loading ───────────────────────────────────────────────────────────────


def test_the_shipped_rulebook_loads() -> None:
    """The fixture YAML is canonical, not this module.

    An earlier draft of the loader invented its own vocabulary (`op`, no
    `mode`) and would have failed to read the very file the answer key asserts
    against — while every test of it passed, because they used the invented
    shape too.
    """
    loaded = load(RULEBOOK)

    assert rule_ids(loaded) == ("R001", "R002", "R003", "R010")
    assert [r.mode for r in loaded] == ["check", "check", "check", "judge"]


def test_a_duplicate_rule_id_is_refused(tmp_path: Path) -> None:
    """`findings` is UNIQUE (agreement_id, rule_id).

    Two rules sharing an id would make one silently overwrite the other's row —
    including its dismissal, so a finding someone had already dealt with would
    come back wearing the other rule's message.
    """
    book = tmp_path / "rulebook.yaml"
    book.write_text(
        "rules:\n"
        "  - id: R001\n    mode: check\n    severity: high\n    assert: exists\n    field: a\n"
        "  - id: R001\n    mode: check\n    severity: low\n    assert: exists\n    field: b\n"
    )

    with pytest.raises(RulebookError, match="duplicate rule id"):
        load(book)


@pytest.mark.parametrize(
    ("body", "problem"),
    [
        ("rules:\n  - id: R1\n    mode: nonsense\n    severity: high\n", "mode"),
        ("rules:\n  - id: R1\n    mode: check\n    severity: urgent\n", "severity"),
        ("rules:\n  - id: R1\n    mode: check\n    severity: high\n    assert: exists\n", "field"),
        ("rules:\n  - id: R1\n    mode: judge\n    severity: high\n", "question"),
        (
            "rules:\n  - id: R1\n    mode: check\n    severity: high\n    assert: in\n"
            "    field: x\n",
            "values",
        ),
        (
            "rules:\n  - id: R1\n    mode: check\n    severity: high\n    assert: compare\n"
            "    left: a\n    right: b\n    op: '~'\n",
            "op",
        ),
    ],
)
def test_a_malformed_rule_fails_at_load(tmp_path: Path, body: str, problem: str) -> None:
    """Loudly, at scan time — not silently at audit time as zero findings.

    A rulebook that half-loads is worse than one that refuses to: "no
    violations" is exactly what a compliant pile looks like, so a rule that
    never ran is invisible in the one place it matters.
    """
    book = tmp_path / "rulebook.yaml"
    book.write_text(body)

    with pytest.raises(RulebookError, match=problem):
        load(book)


def test_a_rulebook_is_not_executed_while_being_read(tmp_path: Path) -> None:
    """`yaml.safe_load`, never `yaml.load`.

    A rulebook is a file someone drops into a watched folder. PyYAML's full
    loader constructs arbitrary Python from a `!!python/object/apply` tag, which
    would make copying a file into a directory a way to run code.
    """
    book = tmp_path / "rulebook.yaml"
    book.write_text("rules: !!python/object/apply:os.system ['echo pwned']\n")

    with pytest.raises(RulebookError):
        load(book)


# ── the operators, on the answer key's own numbers ────────────────────────


def test_a_missing_liability_cap_is_a_finding() -> None:
    """R001, and the subtle one: absence IS the evidence.

    Cobalt/Sparrow state no cap at all, so there is no quote to point at. A
    violation with no citations is correct here — demanding one would make the
    rule unable to fire on the case it exists for.
    """
    violation = evaluate(rules()["R001"], [fact("payment_terms_days", "60")])

    assert isinstance(violation, Violation)
    assert violation.rule_id == "R001" and violation.severity == "high"
    assert violation.citations == (), "there is no clause to cite — that is the finding"


def test_a_stated_liability_cap_passes() -> None:
    assert evaluate(rules()["R001"], [fact("liability_cap_amount", "120000")]) is None


def test_net_60_is_out_of_policy_and_net_30_is_not() -> None:
    """R002. Compared on the normalised integer, so "thirty (30) days",
    "Net 30" and "30 days" are one value — normalisation is for comparison and
    never changes what the register displays."""
    assert evaluate(rules()["R002"], [fact("payment_terms_days", "30", "thirty (30) days")]) is None
    assert evaluate(rules()["R002"], [fact("payment_terms_days", "45", "Net 45")]) is None

    violation = evaluate(rules()["R002"], [fact("payment_terms_days", "60", "Net 60")])
    assert isinstance(violation, Violation)
    assert "Net 60" in violation.message
    assert violation.citations, "there IS a clause here, so it must be cited"


def test_the_cap_rule_passes_by_exactly_zero_and_then_fails() -> None:
    """R003 — the sharpest test in the corpus.

    Before the amendment: 120,000 >= 10,000 × 12 = 120,000, which passes by
    exactly nothing. Approving the price amendment makes it 12,000 × 12 =
    144,000 and the same rule fails. The violation is *created by the human's
    approval*, which is what proves the audit has to re-run after a commit
    rather than only at ingest.
    """
    before = [fact("liability_cap_amount", "120000"), fact("recurring_price", "10000")]
    after = [fact("liability_cap_amount", "120000"), fact("recurring_price", "12000")]

    assert evaluate(rules()["R003"], before) is None, "120000 >= 120000 passes"

    violation = evaluate(rules()["R003"], after)
    assert isinstance(violation, Violation)
    assert "144000" in violation.message


def test_a_comparison_against_a_missing_value_is_not_a_violation() -> None:
    """An unanswered question, not a failure.

    Reporting it would make every incomplete agreement fail every comparison
    rule at once, and the real findings would drown in them. `exists` is how a
    rulebook says a value is required.
    """
    assert evaluate(rules()["R003"], [fact("liability_cap_amount", "1")]) is None


def test_a_disputed_price_is_compared_at_its_lowest() -> None:
    """Two documents disagree about the price while the rule needs one number.

    The smallest is used, because a cap that only covers the cheapest reading
    of the price is not a cap. Understating exposure is the failure mode that
    matters here, so the arithmetic leans the other way.
    """
    facts = [
        fact("liability_cap_amount", "130000"),
        fact("recurring_price", "10000"),
        fact("recurring_price", "12000"),
    ]

    assert evaluate(rules()["R003"], facts) is None, "130000 >= 10000*12 — the low reading passes"


def test_a_judge_rule_is_refused_by_the_deterministic_evaluator() -> None:
    """Returning `None` would report "passed" for a rule nobody ran."""
    with pytest.raises(ValueError, match="judge rule"):
        evaluate(rules()["R010"], [])


def test_a_compare_expression_is_not_evaluated_as_code() -> None:
    """The right-hand side comes from a user-supplied YAML file.

    Anything that `eval`s it turns copying a file into a folder into arbitrary
    code execution. Unsupported expressions are refused, not run.
    """
    from documents_analyst.rules.loader import Rule

    rule = Rule(
        rule_id="RX",
        title="x",
        mode="check",
        severity="high",
        assertion="compare",
        left="liability_cap_amount",
        op=">=",
        right="__import__('os').system('echo pwned')",
    )

    assert evaluate(rule, [fact("liability_cap_amount", "1")]) is None


# ── the judge's output is untrusted until checked ─────────────────────────

DOC = "Beta Logistics LLC may terminate for convenience. Acme Corporation may not."


def judged(**overrides: object) -> list[dict[str, object]]:
    item = {
        "rule_id": "R010",
        "message": "termination is one-sided",
        "source_quote": "Acme Corporation may not.",
    }
    item.update(overrides)
    return [item]


def keep(raw: list[dict[str, object]], text: str = DOC) -> list[Violation]:
    return postprocess_judge(
        raw,
        allowed_ids=("R010",),
        extracted_text=text,
        document_id="doc-1",
        severities={"R010": "medium"},
    )


def test_a_verbatim_judge_finding_is_kept_and_located() -> None:
    """We never ask the model for offsets. It copies; we count."""
    kept = keep(judged())

    assert len(kept) == 1
    citation = kept[0].citations[0]
    assert DOC[citation["char_start"] : citation["char_end"]] == "Acme Corporation may not."
    assert kept[0].evaluated_by == "judge"


def test_the_model_cannot_invent_a_rule() -> None:
    """A finding under a rule id nobody wrote is a policy the model made up."""
    assert keep(judged(rule_id="R999")) == []


def test_a_judge_finding_whose_quote_is_not_in_the_document_is_discarded() -> None:
    """Not softened, not shown with a caveat. Dropped."""
    assert keep(judged(source_quote="Acme Corporation may terminate freely.")) == []


def test_a_judge_finding_with_no_quote_is_discarded() -> None:
    assert keep(judged(source_quote="")) == []


def test_a_quote_occurring_twice_is_discarded() -> None:
    """Choosing between two locations would be the guess the gate removes.

    A citation pointing at the wrong one of two identical sentences is worse
    than no citation at all: it looks precise.
    """
    twice = "The parties agree. The parties agree."

    assert keep(judged(source_quote="The parties agree."), text=twice) == []


def test_severity_comes_from_the_rulebook_not_the_model() -> None:
    """Letting a model grade its own finding makes the policy negotiable by the
    document under review."""
    kept = keep(judged(severity="low"))

    assert kept[0].severity == "medium", "the rulebook said medium"


# ── an outage is not a pass ───────────────────────────────────────────────


class _Rule:
    """The two fields `_evaluate_agreement` cares about."""

    def __init__(self, rule_id: str, mode: str, assertion: str | None = "in") -> None:
        self.rule_id, self.mode, self.severity, self.title, self.question = (
            rule_id,
            mode,
            "high",
            rule_id,
            "?",
        )
        self.assertion = assertion
        """Defaults to a rule that reads facts it can see. `exists` is the one
        that concludes from absence, so tests opt into it explicitly."""


def test_a_judge_outage_is_not_reported_as_compliant(monkeypatch: pytest.MonkeyPatch) -> None:
    """A rate limit must mean "not evaluated", never "passed".

    `_evaluate_agreement` returns the rules that passed so their standing
    findings can be cleared. A judge rule whose model call failed contributes no
    violation, and that absence used to be read as a pass — so Gemini's free
    tier, measured live at 15 requests a minute, silently cleared real
    compliance findings and told a reviewer the pile was clean.

    Deterministic `check` rules are unaffected *by a judge outage*: they read
    facts already in the database and cannot half-run.
    """
    from documents_analyst.graph.nodes import audit as audit_node

    rules = [_Rule("R001", "check"), _Rule("R010", "judge")]
    monkeypatch.setattr(audit_node, "_facts", lambda *a, **k: [])
    monkeypatch.setattr(audit_node, "evaluate", lambda *a, **k: None)
    monkeypatch.setattr(audit_node, "_unread", lambda *a, **k: [])

    monkeypatch.setattr(audit_node, "_judge", lambda *a, **k: ([], True))
    _, passed = audit_node._evaluate_agreement(object(), object(), "agreement", rules)
    assert passed == ["R001", "R010"], "a clean, complete pass clears both"

    monkeypatch.setattr(audit_node, "_judge", lambda *a, **k: ([], False))
    _, passed = audit_node._evaluate_agreement(object(), object(), "agreement", rules)
    assert passed == ["R001"], "an unevaluated judge rule keeps its finding"


def test_an_unread_document_does_not_become_an_absence_finding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`assert: exists` concludes from absence, so it must not run over a
    document nobody could read.

    Observed live: `LLM_MODEL_EXTRACT` named a model Google had retired, every
    section call returned 404, and the pile produced zero facts. R001 then
    reported "no liability cap is stated in this agreement" against an MSA whose
    section 7 reads "total liability shall not exceed USD 120,000" — a
    high-severity finding, confidently worded, landing in the review queue ahead
    of the real conflicts.

    Skipped, not passed: the rule is also kept out of the cleared list, so a
    standing finding survives a run that could not check it.
    """
    from documents_analyst.graph.nodes import audit as audit_node

    rules = [_Rule("R001", "check", "exists"), _Rule("R002", "check", "in")]
    fired: list[str] = []

    def _evaluate(rule: object, _facts: object) -> None:
        fired.append(rule.rule_id)  # type: ignore[attr-defined]
        return None

    monkeypatch.setattr(audit_node, "_facts", lambda *a, **k: [])
    monkeypatch.setattr(audit_node, "evaluate", _evaluate)
    monkeypatch.setattr(audit_node, "_judge", lambda *a, **k: ([], True))
    monkeypatch.setattr(audit_node, "_unread", lambda *a, **k: ["01_msa.docx"])

    _, passed = audit_node._evaluate_agreement(object(), object(), "agreement", rules)

    assert "R001" not in fired, "an absence rule must not run over an unread document"
    assert "R002" in fired, "rules that read facts they can see still run"
    assert "R001" not in passed, "a skipped rule is not a passed rule"
