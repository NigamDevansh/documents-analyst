"""Three tiers: rules → local NLI → LLM. And the amendment/conflict question.

One value group is settled and free. Two or more is a disagreement, and the
ladder decides what kind, cheapest test first (most facts are numbers and dates):

    TIER 0 · one passage  both quotes in one paragraph? → conflict, `same_passage`
    TIER 1 · rules        same normalised number/date?  → agree
                          scalar and different?         → disagree; prose → tier 2
    TIER 2 · NLI          local, ~0.2s. entail/neutral  → agree; contradiction → 3
    TIER 3 · LLM          the only paid tier; only genuine prose contradictions

The amendment question has teeth: two documents disagreeing is a CONFLICT
(someone chooses); a later one deliberately replacing an earlier is an AMENDMENT
(fast lane, batch-approvable). Same observable difference, opposite meaning — so
an amendment requires the later document to say so in words, and the model's
`evidence_quote` is checked verbatim against that document before the verdict
holds. A missing or paraphrased quote defaults to conflict, because guessing
amendment wrong waves a change past a reviewer.

See docs/BACKEND_INTERNALS.md § "Nodes — what each reads and writes".
"""

from __future__ import annotations

import logging
import re
from typing import Any

from langchain_core.runnables import RunnableConfig
from sqlalchemy import text as sql

from documents_analyst.db.repo import decisions as decision_repo
from documents_analyst.db.repo import events as event_repo
from documents_analyst.db.repo import facts as fact_repo
from documents_analyst.db.repo import slots as slot_repo
from documents_analyst.graph.context import RunContext, context_of
from documents_analyst.graph.state import PileRunState
from documents_analyst.worker.lease import Fence
from documents_analyst_core.portable.llm_client import LLMError
from documents_analyst_core.portable.verify import find_quote
from documents_analyst_core.prompts import adjudication as adjudication_prompt
from documents_analyst_core.taxonomy.schema import adjudication_schema
from documents_analyst_core.telemetry.sink import timed

LOG = logging.getLogger(__name__)


def run(state: PileRunState, config: RunnableConfig) -> dict[str, object]:
    ctx = context_of(config)
    fence = Fence(pile_id=state["pile_id"], epoch=state["lease_epoch"])
    settled = conflicts = amendments = corroborated = 0

    for agreement_id in _agreements(ctx, state["pile_id"]):
        for slot in _slots_with_facts(ctx, agreement_id):
            groups = slot["groups"]
            if len(groups) == 1:
                # One value, however many documents said it. Every fact after the
                # first corroborates — a second source agreeing is not an edit.
                with ctx.engine.begin() as conn:
                    slot_repo.set_state(
                        conn,
                        fence,
                        slot["id"],
                        state="settled",
                        current_fact_id=str(groups[0][0]["id"]),
                    )
                    fact_repo.set_role(
                        conn, fence, [str(f["id"]) for f in groups[0][:1]], "current"
                    )
                    fact_repo.set_role(
                        conn, fence, [str(f["id"]) for f in groups[0][1:]], "corroborating"
                    )
                settled += 1
                corroborated += max(len(groups[0]) - 1, 0)
                continue

            verdict = _adjudicate(ctx, fence, slot, groups)
            with ctx.engine.begin() as conn:
                if verdict.kind == "agree":
                    slot_repo.set_state(
                        conn,
                        fence,
                        slot["id"],
                        state="settled",
                        current_fact_id=str(groups[0][0]["id"]),
                    )
                    settled += 1
                    continue

                # A resolved slot does not go back to `disputed` (that discards
                # who decided). New evidence makes it `contested`: same question,
                # asked again, previous answer still visible.
                next_state = "contested" if slot["state"] == "resolved" else "disputed"
                slot_repo.set_state(conn, fence, slot["id"], state=next_state)
                fact_repo.set_role(
                    conn, fence, [str(f["id"]) for g in groups for f in g], "competing"
                )

                # An amendment over three or more values counts only when `_chain`
                # checked every step. Anything else answered two of the values, and
                # the batch lane would supersede one nobody looked at — downgraded to
                # a conflict, which asks.
                partial = verdict.kind == "amendment" and len(groups) > 2 and not verdict.chain
                kind = "conflict" if partial or verdict.kind != "amendment" else "amendment"
                reason = verdict.reason
                if partial:
                    reason = (
                        f"{reason} (adjudicated on two of {len(groups)} values, so it needs "
                        "an explicit choice rather than the amendment fast lane)".strip()
                    )

                decision_repo.raise_decision(
                    conn,
                    fence,
                    kind=kind,
                    ref_id=slot["id"],
                    payload=_payload(
                        slot,
                        groups,
                        recommended=None if partial else verdict.winner_group,
                    ),
                    relationship=(
                        verdict.relationship
                        or (verdict.kind if verdict.kind != "agree" else "unknown")
                    ),
                    detected_by=verdict.detected_by,
                    evidence_quote=verdict.evidence,
                    group_key=_group_key(slot),
                    recommendation=reason,
                    # The one case a settled question may be asked again: a
                    # resolved slot with new, live, disagreeing evidence. Without
                    # it a contested slot has nothing in the queue to answer it.
                    reopen=slot["state"] == "resolved",
                )
            if verdict.kind == "amendment" and (len(groups) == 2 or verdict.chain):
                amendments += 1
            else:
                conflicts += 1

    with ctx.engine.begin() as conn:
        ctx.telemetry.flush(conn)
        event_repo.append(
            conn,
            pile_id=state["pile_id"],
            run_id=state["run_id"],
            type="stage.classify.done",
            payload={
                "settled": settled,
                "corroborating": corroborated,
                "conflicts": conflicts,
                "amendments": amendments,
            },
        )
    return {}


class _Verdict:
    __slots__ = (
        "chain",
        "detected_by",
        "evidence",
        "kind",
        "reason",
        "relationship",
        "winner_group",
    )

    def __init__(
        self,
        kind: str,
        *,
        detected_by: str,
        reason: str = "",
        evidence: str | None = None,
        winner_group: int | None = None,
        relationship: str | None = None,
        chain: bool = False,
    ) -> None:
        self.chain = chain
        """Set by `_chain`: every neighbouring pair of 3+ values was a verified
        amendment, so the verdict covers the whole slot, not two of its values."""
        self.kind = kind
        self.detected_by = detected_by
        self.reason = reason
        self.evidence = evidence
        self.relationship = relationship
        """How the two values relate, when narrower than `kind`. `same_passage`
        is the case: both values come from one paragraph, so "conflict" is what
        the reviewer resolves but "the sources disagree" is not what happened.
        Defaults to `kind`."""
        self.winner_group = winner_group
        """Which value an *amendment* settles on — the later document's. Recorded
        so approving one resolves the slot without asking a reviewer to pick, which
        is what makes the batch lane honest. A `conflict` leaves this `None`."""


def _adjudicate(
    ctx: RunContext, fence: Fence, slot: dict[str, Any], groups: list[list[dict[str, Any]]]
) -> _Verdict:
    """The ladder. Returns as soon as a tier can answer."""
    left, right = groups[0][0], groups[1][0]

    # ── tier 0 · one passage ──────────────────────────────────────────────
    # Before asking which document is right, ask whether there are two. Every
    # group, not the first two: the rest of the ladder compares groups[0]/[1], but
    # this claim is about the whole card, so a slot with two values in one
    # sentence and six elsewhere is not "all from one paragraph".
    if _same_passage(ctx, groups):
        return _Verdict(
            "conflict",
            detected_by="rule",
            relationship="same_passage",
            reason=(
                f"{'both' if len(groups) == 2 else 'all'} of these values come from one "
                f"paragraph of {left.get('filename')} — a general statement and its "
                "exception, or one clause covering several parties. Nothing has been "
                "superseded; which value belongs in the register is a reading of that "
                "paragraph"
            ),
        )

    # Three or more values: never settled from a pair. Either every step is a verified
    # amendment of the one before (A → B → C, the latest wins), or a person chooses.
    if len(groups) > 2:
        return _chain(ctx, fence, slot, groups)

    # ── tier 1 ────────────────────────────────────────────────────────────
    if _both_scalar(left, right):
        # Already known to differ — `_by_value` grouped on the normalised value,
        # so two groups means two different numbers. Asking a model would be
        # paying for an opinion about arithmetic.
        return _amendment_across(ctx, fence, slot, groups[0], groups[1], detected_by="rule")

    # ── tier 2 ────────────────────────────────────────────────────────────
    nli = getattr(ctx.models, "nli", None)
    if nli is not None and getattr(nli, "is_available", True):
        try:
            result = nli.classify(
                str(left.get("source_quote") or ""), str(right.get("source_quote") or "")
            )
        except Exception:  # noqa: BLE001 — a model failure must not lose the pair
            LOG.warning("NLI failed on slot %s; escalating to the LLM", slot["id"])
        else:
            if result.contradiction < ctx.settings.nli_contradiction_threshold:
                # Not a contradiction: two ways of saying a compatible thing.
                return _Verdict(
                    "agree", detected_by="nli", reason="the two statements do not conflict"
                )

    # ── tier 3 ────────────────────────────────────────────────────────────
    return _amendment_across(ctx, fence, slot, groups[0], groups[1], detected_by="llm")


def _chain(
    ctx: RunContext, fence: Fence, slot: dict[str, Any], groups: list[list[dict[str, Any]]]
) -> _Verdict:
    """Three or more values as an amendment chain, or a conflict.

    Groups are put in arrival order (when each value first entered the pile) and every
    neighbouring pair goes through the same check as two values: the judge must call it
    an amendment AND quote the replacing language verbatim from the later document. Only
    if every step passes is it an amendment, settling on the last value; one failed step,
    or an older value turning up again in a later document, makes it a conflict.

    Every pair is checked, not just the first two — the bug this replaced settled a whole
    slot from one comparison and silently dropped the third value.
    """

    def last(group: list[dict[str, Any]]) -> Any:
        return max(fact["document_created_at"] for fact in group)

    first = _arrival
    ordered = sorted(groups, key=first)
    detected_by = "rule" if all(_both_scalar(g[0], g[0]) for g in ordered) else "llm"
    labels = " → ".join(
        f"{_side(g[0]).get('value_text') or g[0].get('source_quote')} ({g[0].get('filename')})"
        for g in ordered
    )

    for earlier, later in zip(ordered, ordered[1:], strict=False):
        if last(earlier) > first(later):
            return _Verdict(
                "conflict",
                detected_by="rule",
                reason=(
                    f"{len(groups)} values ({labels}), and an older value appears again in a "
                    "later document, so the latest is not clearly the one that applies"
                ),
            )

    steps: list[_Verdict] = []
    for earlier, later in zip(ordered, ordered[1:], strict=False):
        step = _amendment_across(ctx, fence, slot, earlier, later, detected_by=detected_by)
        later_group = int(later[0].get("value_group") or 0)
        if step.kind != "amendment" or step.winner_group != later_group:
            return _Verdict(
                "conflict",
                detected_by=detected_by,
                reason=(
                    f"{len(groups)} values ({labels}); {later[0].get('filename')} does not "
                    f"clearly replace {earlier[0].get('filename')}"
                    + (f": {step.reason}" if step.reason else "")
                ),
            )
        steps.append(step)

    return _Verdict(
        "amendment",
        detected_by=detected_by,
        reason=f"Each later document replaces the one before: {labels}. {steps[-1].reason}".strip(),
        evidence=steps[-1].evidence,
        winner_group=int(ordered[-1][0].get("value_group") or 0),
        chain=True,
    )


def _same_passage(ctx: RunContext, groups: list[list[dict[str, Any]]]) -> bool:
    """Every quote on this slot inside one paragraph of one document. Found on a
    real 8-K: one sentence said six employment terms run to 2008 *except* one to
    2009, and the register showed `DISPUTED — sources disagree` over one sentence.
    The test is the paragraph (a blank line), not the sentence — a splitter has to
    decide whether the `.` in `Mr. Holland` ends one, and here it doesn't. The
    span checked is the whole first-to-last range, so values spread over several
    paragraphs can't be called one. A false positive costs a card's wording; a
    false negative costs nothing, the pair carries on down the ladder."""
    facts = [fact for group in groups for fact in group]
    documents = {str(fact["source_document_id"]) for fact in facts}
    if len(documents) != 1:
        return False

    start = min(int(fact["char_start"]) for fact in facts)
    end = max(int(fact["char_end"]) for fact in facts)

    with ctx.engine.begin() as conn:
        row = conn.execute(
            sql("SELECT extracted_text FROM documents WHERE id = :id"),
            {"id": documents.pop()},
        ).first()
    if row is None or not row.extracted_text:
        return False
    return _PARAGRAPH_BREAK.search(row.extracted_text[start:end]) is None


_PARAGRAPH_BREAK = re.compile(r"\n[ \t]*\n")
"""What separates two statements. Not `[.!?]` — see `_same_passage`."""


def _both_scalar(left: dict[str, Any], right: dict[str, Any]) -> bool:
    return bool(str(left.get("value_norm") or "").strip()) and bool(
        str(right.get("value_norm") or "").strip()
    )


NEVER_AMENDED = frozenset({"effective_date"})
"""Fields an `amendment` verdict is never taken for. Every later document has its
own effective date, so two different ones are the normal case, not a replacement."""

AMENDMENT_CANDIDATES = 3
"""How many documents stating the later value are shown to the judge, newest first,
before it is called a conflict. Bounds the extra judge calls per disputed field."""


def _arrival(group: list[dict[str, Any]]) -> Any:
    """When a value first entered the pile: its earliest document's arrival."""
    return min(fact["document_created_at"] for fact in group)


def _amendment_across(
    ctx: RunContext,
    fence: Fence,
    slot: dict[str, Any],
    first: list[dict[str, Any]],
    second: list[dict[str, Any]],
    *,
    detected_by: str,
) -> _Verdict:
    """Is the later value an amendment of the earlier one — asked of each document that
    states it, not just the first.

    A value group can be stated by several documents. Showing the judge only the
    earliest one asked the wrong question: on the demo pile USD 12,000 appears first in
    an invoice and then in the amendment that replaces the fee, so the judge was asked
    whether the INVOICE replaced the SOW's price, correctly said no, and never saw the
    amendment's wording. Now each document stating the later value is tried, newest
    first (amendments tend to arrive last), up to `AMENDMENT_CANDIDATES`, and the first
    verified amendment wins. With none, the newest document's verdict stands.
    """
    earlier, later = (first, second) if _arrival(first) <= _arrival(second) else (second, first)
    original = min(earlier, key=lambda fact: fact["document_created_at"])
    seen: set[str] = set()
    candidates: list[dict[str, Any]] = []
    for fact in sorted(later, key=lambda f: f["document_created_at"], reverse=True):
        document = str(fact["source_document_id"])
        if document not in seen:
            seen.add(document)
            candidates.append(fact)
    verdicts: list[_Verdict] = []
    for candidate in candidates[:AMENDMENT_CANDIDATES]:
        # The candidate is passed first so a tie in arrival (one document stating both
        # values) still treats it as the later side.
        verdict = _amendment_or_conflict(
            ctx, fence, slot, candidate, original, detected_by=detected_by
        )
        if verdict.kind == "amendment":
            return verdict
        verdicts.append(verdict)
    return verdicts[0]


def _amendment_or_conflict(
    ctx: RunContext,
    fence: Fence,
    slot: dict[str, Any],
    left: dict[str, Any],
    right: dict[str, Any],
    *,
    detected_by: str,
) -> _Verdict:
    """Ask the model which it is, then refuse to take `amendment` on trust.

    The verdict is only half of what comes back; the other half is the quote
    that is supposed to prove it. An amendment whose evidence is not verbatim in
    the later document is downgraded to a conflict — and the downgrade is the
    safe direction, because it asks a question rather than skipping one.
    """
    later, earlier = _order(left, right)
    ctx.renew_lease(fence)
    try:
        # Each side carries the paragraph its quote sits in. The fact's own quote is
        # the value ("the monthly fee shall be USD 12,000"); the words that make it an
        # amendment ("Section 4.2 ... is hereby deleted ... and replaced with") are
        # usually just before it. Shown only the quote, the judge recognised the
        # amendment and then invented the evidence, which the verbatim gate discarded.
        earlier_context = _paragraph(ctx, earlier)
        later_context = _paragraph(ctx, later)
        system, user = adjudication_prompt.build(
            str(slot["field_key"]),
            _side(earlier, earlier_context[0]),
            _side(later, later_context[0]),
            doc_relationship=_relationship_line(earlier, later),
        )
        with timed() as elapsed:
            payload, usage = ctx.judge.structured(
                system=system, user=user, schema=adjudication_schema(), max_tokens=600
            )
        ctx.record("classify", usage, latency_ms=elapsed())
    except LLMError as exc:
        # Unreachable model, exhausted quota, malformed answer. Defaults to
        # conflict: the safe failure is asking a person, not deciding for them.
        LOG.warning("adjudication failed on slot %s: %s", slot["id"], exc)
        return _Verdict("conflict", detected_by=detected_by, reason=f"could not adjudicate: {exc}")

    verdict = str(payload.get("verdict") or "conflict")
    reason = str(payload.get("reason") or "").strip()
    quote = str(payload.get("evidence_quote") or "")

    if verdict != "amendment":
        return _Verdict(
            "agree" if verdict == "agree" else "conflict", detected_by=detected_by, reason=reason
        )

    if str(slot.get("field_key")) in NEVER_AMENDED:
        # An amendment, side letter or SOW has its own start date. Extracted under the
        # agreement's `effective_date`, it reads as a change to it, and the judge
        # accepted a general "supersedes conflicting terms" clause as proof even when
        # told not to. Enforced here rather than trusted to the prompt: a person
        # chooses which date the register shows.
        return _Verdict(
            "conflict",
            detected_by=detected_by,
            reason=(
                f"{later.get('filename')} states its own effective date; a document's "
                "start date does not change the agreement's — choose which one the "
                "register shows"
            ),
        )
    verified = _verify_in_document(
        ctx, str(later["source_document_id"]), quote, within=later_context[0]
    )
    if not verified:
        LOG.info(
            "slot %s: amendment claimed but its evidence is not verbatim in %s — treating as "
            "a conflict",
            slot["id"],
            later["filename"],
        )
        return _Verdict(
            "conflict",
            detected_by=detected_by,
            reason=f"{reason} (amendment claimed without verifiable evidence)".strip(),
        )
    return _Verdict(
        "amendment",
        detected_by=detected_by,
        reason=reason,
        evidence=quote,
        winner_group=int(later.get("value_group") or 0),
    )


def _relationship_line(earlier: dict[str, Any], later: dict[str, Any]) -> str:
    """What the model is told about where the two sides came from. It used to say
    "SIDE_B entered the pile after SIDE_A" unconditionally, but a document can
    disagree with itself (two effective dates in one lease), and the model then
    answered about a second document that does not exist. Arrival order is
    meaningless within one file, so that case is stated as what it is."""
    if str(earlier["source_document_id"]) == str(later["source_document_id"]):
        return (
            f"SIDE_A and SIDE_B are both from {later['filename']}, in different "
            "places in the same document. Neither can have superseded the other "
            "by arriving later"
        )
    return f"SIDE_B ({later['filename']}) entered the pile after SIDE_A ({earlier['filename']})"


CONTEXT_CHARS = 1000
"""The most of a paragraph shown to the judge per side. A clause long enough to
exceed this is cut to a window centred on the quote."""


def _paragraph(ctx: RunContext, fact: dict[str, Any]) -> tuple[str, int]:
    """The paragraph around a fact's quote, and where it starts in the document.

    Bounded by blank lines, as `_same_passage` defines a paragraph, and capped at
    `CONTEXT_CHARS`. Taken from the stored extracted text by the fact's own offsets,
    so it is exactly what the document says — it is quoted to the judge, and the
    judge's evidence is checked against it."""
    with ctx.engine.begin() as conn:
        row = conn.execute(
            sql("SELECT extracted_text FROM documents WHERE id = :id"),
            {"id": str(fact["source_document_id"])},
        ).first()
    body = (row.extracted_text if row is not None else "") or ""
    start, end = int(fact.get("char_start") or 0), int(fact.get("char_end") or 0)
    if not body or end <= start:
        return "", 0
    breaks = list(_PARAGRAPH_BREAK.finditer(body))
    begin = max((b.end() for b in breaks if b.end() <= start), default=0)
    finish = min((b.start() for b in breaks if b.start() >= end), default=len(body))
    if finish - begin > CONTEXT_CHARS:
        spare = max(CONTEXT_CHARS - (end - start), 0) // 2
        begin = max(begin, start - spare)
        finish = min(finish, end + spare)
    return body[begin:finish].strip(), begin


def _verify_in_document(ctx: RunContext, document_id: str, quote: str, *, within: str = "") -> bool:
    """Verbatim, exactly once, in that document's own text. "Exactly once" matters
    as much as "present": an amendment whose evidence points at one of two
    identical sentences looks precise and is not."""
    if not quote.strip():
        return False
    # First inside the paragraph the judge was shown, where boilerplate that recurs
    # in the document ("is hereby deleted in its entirety and replaced with the
    # following" opens two sections of the demo amendment) is still unambiguous.
    # Still verbatim and still exactly once — just within the passage it came from.
    if within and find_quote(within, quote) is not None:
        return True
    with ctx.engine.begin() as conn:
        row = conn.execute(
            sql("SELECT extracted_text FROM documents WHERE id = :id"), {"id": document_id}
        ).first()
    if row is None or not row.extracted_text:
        return False
    return find_quote(row.extracted_text, quote) is not None


def _order(left: dict[str, Any], right: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """`(later, earlier)` by when the document entered the pile — arrival order,
    not any date inside the text, which the document under review chose for
    itself."""
    if left["document_created_at"] >= right["document_created_at"]:
        return left, right
    return right, left


def _side(fact: dict[str, Any], context: str = "") -> dict[str, Any]:
    value = fact.get("value") or {}
    return {
        "document": fact.get("filename"),
        "value_text": value.get("text") if isinstance(value, dict) else None,
        "source_quote": fact.get("source_quote"),
        "context": context,
    }


def _payload(
    slot: dict[str, Any], groups: list[list[dict[str, Any]]], *, recommended: int | None = None
) -> dict[str, Any]:
    """What the reviewer sees: every option, with who says it. Whole groups, not a
    winning pair — hiding the third option would make the answer look unanimous.
    `value_group` is the stored number, not this list's index: an approval carries
    `chosen_value_group` into a `facts.value_group` comparison, so reading the real
    number cannot drift. `recommended` is set for an amendment, `None` for a
    conflict."""
    return {
        "field_key": slot["field_key"],
        "recommended_value_group": recommended,
        "options": [
            {
                "value_group": int(g[0].get("value_group") or 0),
                "value_text": (g[0].get("value") or {}).get("text"),
                "value_norm": g[0].get("value_norm"),
                "sources": [
                    {
                        "document_id": str(f["source_document_id"]),
                        "filename": f["filename"],
                        "char_start": f["char_start"],
                        "char_end": f["char_end"],
                        "quote": f["source_quote"],
                    }
                    for f in g
                ],
            }
            for index, g in enumerate(groups)
        ],
    }


def _group_key(slot: dict[str, Any]) -> str:
    """Stable identity for the register question the classifier produced. Audit
    resolves a judge quote to this slot only when its citation overlaps a fact in
    it, so the slot id merges shared evidence without merging different clauses."""
    return f"slot:{slot['id']}"


def _agreements(ctx: RunContext, pile_id: str) -> list[str]:
    with ctx.engine.begin() as conn:
        rows = conn.execute(
            sql("SELECT id FROM agreements WHERE pile_id = :p ORDER BY created_at"),
            {"p": pile_id},
        ).all()
    return [str(row.id) for row in rows]


def _slots_with_facts(ctx: RunContext, agreement_id: str) -> list[dict[str, Any]]:
    """Every slot in this agreement, with its live facts grouped by value.
    `role <> 'superseded'` stops a resolved slot re-opening itself: without it a
    later run re-read rejected values, saw two groups, moved `resolved` back to
    `contested`, and `raise_decision` refused to ask again — a disagreement with
    no way to answer it. A superseded fact is not deleted (it is evidence set
    aside), but it is not a candidate for what the register should say now."""
    with ctx.engine.begin() as conn:
        rows = conn.execute(
            sql(
                "SELECT s.id, s.field_key, s.subject_free, s.state, s.agreement_id, "
                "f.id AS fact_id, f.value, f.value_norm, f.source_quote, f.char_start, "
                "f.char_end, f.source_document_id, f.value_group, d.filename, "
                "d.created_at AS document_created_at "
                "FROM claim_slots s JOIN facts_and_claims f ON f.slot_id = s.id "
                "JOIN documents d ON d.id = f.source_document_id "
                "WHERE s.agreement_id = :a AND f.status = 'active' "
                "  AND f.role <> 'superseded' AND d.status NOT IN ('removed', 'superseded', 'quarantined') "
                "ORDER BY s.field_key NULLS LAST, s.subject_free, f.value_group, d.created_at"
            ),
            {"a": agreement_id},
        ).all()

    slots: dict[str, dict[str, Any]] = {}
    for row in rows:
        slot = slots.setdefault(
            str(row.id),
            {
                "id": str(row.id),
                "field_key": row.field_key or row.subject_free,
                "state": row.state,
                "agreement_id": str(row.agreement_id),
                "by_group": {},
            },
        )
        fact = dict(row._mapping)
        fact["id"] = str(row.fact_id)
        slot["by_group"].setdefault(int(row.value_group or 0), []).append(fact)

    for slot in slots.values():
        slot["groups"] = [slot["by_group"][k] for k in sorted(slot["by_group"])]
        del slot["by_group"]
    return list(slots.values())
