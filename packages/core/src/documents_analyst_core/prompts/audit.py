"""The judge-rule prompt, and the audit narrative.

Adapted from the Examine pattern: the judge is asked for a verdict plus the
evidence it relied on, and evidence that fails Gate 1 invalidates the verdict.
A rule that "passed" on a hallucinated quote is worse than no rule at all.
"""

from __future__ import annotations

from typing import Any

from .wrapping import system_preamble, wrap_untrusted

VERSION = "audit/1"


def build_judge(rule_text: str, evidence: list[dict[str, Any]]) -> tuple[str, str]:
    """Returns (system, user).

    `rule_text` comes from the pile's `rulebook.yaml` — a file the pile owner
    wrote, so it is ours in the sense that matters here and goes in the system
    message. The evidence is contract text and is wrapped.
    """
    system = f"{system_preamble()}\n\n{JUDGE_TASK}\n\nRule under test:\n{rule_text}"
    blocks = [
        wrap_untrusted(
            f"document: {item.get('document', '?')}\n{item.get('source_quote', '')}",
            label=f"EVIDENCE_{index}",
        )
        for index, item in enumerate(evidence)
    ]
    user = "\n\n".join(blocks) if blocks else wrap_untrusted("", label="EVIDENCE_NONE")
    return system, user


def build_summary(findings: list[dict[str, Any]]) -> tuple[str, str]:
    """The narrative paragraph that heads the register's findings section.

    Findings are **ours** — already adjudicated, already gated — so they are
    not wrapped as untrusted. Their quotes were verbatim-checked before they
    became findings; re-wrapping them here would suggest a doubt the pipeline
    has already resolved.
    """
    system = f"{system_preamble()}\n\n{SUMMARY_TASK}"
    lines = [
        f"- [{f.get('rule_id', '?')}] {f.get('severity', '?')}: {f.get('title', '')}"
        for f in findings
    ]
    user = "\n".join(lines) if lines else "(no findings)"
    return system, user


JUDGE_TASK = """\
You decide whether a policy rule is satisfied by the evidence shown, and you
show your working.

Rules:
- Answer only from the evidence blocks. If they do not settle it, the verdict
  is `unknown` — not `pass` and not `fail`.
- Every verdict must cite the evidence it relied on, quoted verbatim. The quote
  is checked against the source document; if it is not found there the verdict
  is DISCARDED entirely, not downgraded.
- `rule_id` must be one we gave you. A rule id you invent is dropped.
- A rule with no relevant evidence is `unknown`. Reporting `pass` because
  nothing contradicted it is how a compliance report becomes fiction."""


SUMMARY_TASK = """\
Write one short paragraph summarising these findings for a reader who will act
on them.

- State what was found, not what it might mean. No advice, no recommendations,
  no legal opinion.
- Do not introduce a finding that is not in the list.
- Do not soften severity. "Three high-severity findings" is the sentence, not
  "some items worth reviewing"."""
