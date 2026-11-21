"""Facts → claim slots, and facts asserting the same value into one group.

A slot is one question ("what does this agreement say about payment terms?");
every fact answering it the same way lands in the same `value_group`. Grouping
turns N pairwise comparisons into one question: three documents saying 30, 30, 45
are two groups a reviewer answers once, not three near-identical cards.

Two nets, because a slot key is not always available:

- Net 1, structured: taxonomy facts carry `field_key`, so the slot is an exact,
  free lookup — almost everything.
- Net 2, vector: open subjects have no key ("data residency region" vs "data
  storage location"), so cosine over `fact_key_embedding` above a threshold, or
  a new subject. Not run over taxonomy facts, where a near-miss could wrongly
  merge two exact keys.

See docs/BACKEND_INTERNALS.md § "Nodes — what each reads and writes".
"""

from __future__ import annotations

import logging
from typing import Any

from langchain_core.runnables import RunnableConfig
from sqlalchemy import text as sql

from documents_analyst.db.repo import events as event_repo
from documents_analyst.db.repo import facts as fact_repo
from documents_analyst.db.repo import slots as slot_repo
from documents_analyst.graph.context import context_of
from documents_analyst.graph.state import PileRunState
from documents_analyst.worker.lease import Fence

LOG = logging.getLogger(__name__)


def run(state: PileRunState, config: RunnableConfig) -> dict[str, object]:
    ctx = context_of(config)
    fence = Fence(pile_id=state["pile_id"], epoch=state["lease_epoch"])
    slots = groups = repaired = 0

    for agreement_id in _agreements(ctx, state["pile_id"]):
        repaired += _repair_stale_vectors(ctx, fence, agreement_id)
        with ctx.engine.begin() as conn:
            facts = fact_repo.unmatched_for_agreement(conn, agreement_id)
        if not facts:
            continue

        for key, members in _by_subject(facts, threshold=ctx.settings.subject_similarity).items():
            with ctx.engine.begin() as conn:
                slot = slot_repo.ensure(
                    conn,
                    fence,
                    agreement_id=agreement_id,
                    **_slot_key(key),
                )
                by_value = _by_value(members)
                slot_repo.attach_facts(
                    conn,
                    fence,
                    str(slot["id"]),
                    {str(i): [str(f["id"]) for f in g] for i, g in enumerate(by_value)},
                )
            slots += 1
            groups += len(by_value)

    with ctx.engine.begin() as conn:
        event_repo.append(
            conn,
            pile_id=state["pile_id"],
            run_id=state["run_id"],
            type="stage.match.done",
            payload={"slots": slots, "value_groups": groups, "re_embedded": repaired},
        )
    return {}


def _repair_stale_vectors(ctx: Any, fence: Fence, agreement_id: str) -> int:
    """Re-embed open subjects whose vector came from a different model. Two
    embedders can share a width and no meaning: bge-small and
    `text-embedding-3-small@384` both fit `vector(384)` and `<=>` returns a
    number, but it is noise — same subjects drift apart, unrelated ones clear
    0.86 and merge into a false conflict. An existing pile plus one new document
    is that mixture with no symptom, so anything not from the configured model is
    rebuilt from `subject_free` first.

    Only open subjects: taxonomy facts carry a vector nothing compares (matched by
    `field_key`, Net 1), so they keep their label until something reads it. The
    migration backfill assumes every pre-existing vector is bge-small — the
    asymmetry is deliberate: a mislabelled OpenAI vector costs one re-embed, a
    mislabelled bge vector leaves the silent corruption this removes.
    """
    model = getattr(ctx.models.embeddings, "model_name", "")
    with ctx.engine.begin() as conn:
        stale = fact_repo.stale_open_subjects(conn, agreement_id, model=model)
    if not stale:
        return 0

    LOG.info(
        "agreement %s: re-embedding %d open subject(s) written by another model",
        agreement_id,
        len(stale),
    )
    ctx.renew_lease(fence)
    vectors = ctx.models.embeddings.embed_documents(
        [str(row["subject_free"] or "") for row in stale]
    )
    with ctx.engine.begin() as conn:
        return fact_repo.reembed(
            conn,
            fence,
            updates=[(str(row["id"]), vector) for row, vector in zip(stale, vectors, strict=True)],
            model=model,
        )


def _slot_key(key: str) -> dict[str, str]:
    """A bucket key back into the column it belongs in. The `~` prefix is an
    in-memory marker, never storage — writing it would put a sentinel in a column
    a register renders."""
    if key.startswith(_OPEN_PREFIX):
        return {"subject_free": key[len(_OPEN_PREFIX) :]}
    return {"field_key": key}


def _agreements(ctx: Any, pile_id: str) -> list[str]:
    with ctx.engine.begin() as conn:
        rows = conn.execute(
            sql("SELECT id FROM agreements WHERE pile_id = :p ORDER BY created_at"),
            {"p": pile_id},
        ).all()
    return [str(row.id) for row in rows]


def _by_subject(
    facts: list[dict[str, Any]], *, threshold: float
) -> dict[str, list[dict[str, Any]]]:
    """Net 1 then Net 2, in that order and never both on one fact."""
    buckets: dict[str, list[dict[str, Any]]] = {}
    open_facts: list[dict[str, Any]] = []

    for fact in facts:
        if fact.get("field_key"):
            buckets.setdefault(str(fact["field_key"]), []).append(fact)
        else:
            open_facts.append(fact)

    for fact in open_facts:
        buckets.setdefault(_nearest_subject(fact, buckets, threshold=threshold), []).append(fact)
    return buckets


def _nearest_subject(
    fact: dict[str, Any], buckets: dict[str, list[dict[str, Any]]], *, threshold: float
) -> str:
    """Which existing open subject this one belongs to, or a new one. Compared
    only against other OPEN subjects — joining a taxonomy bucket would render a
    free-text claim under a taxonomy label nothing downstream would question."""
    mine = _vector(fact.get("fact_key_embedding"))
    own = _subject_key(fact)
    if mine is None:
        return own

    best, score = own, 0.0
    for key, members in buckets.items():
        if not key.startswith(_OPEN_PREFIX):
            continue
        for other in members:
            theirs = _vector(other.get("fact_key_embedding"))
            if theirs is None:
                continue
            candidate_similarity = _cosine(mine, theirs)
            if candidate_similarity > score:
                best, score = key, candidate_similarity

    if score >= threshold:
        return best
    return own


_OPEN_PREFIX = "~"
"""Marks a bucket as an open subject rather than a taxonomy key — a prefix that
cannot appear in a taxonomy key, so the two namespaces cannot collide."""


def _subject_key(fact: dict[str, Any]) -> str:
    return f"{_OPEN_PREFIX}{str(fact.get('subject_free') or '').strip().casefold()}"


def _vector(raw: object) -> list[float] | None:
    """pgvector comes back as a string like `[0.1,0.2,…]` over psycopg."""
    if raw is None:
        return None
    if isinstance(raw, list):
        return [float(x) for x in raw]
    try:
        return [float(part) for part in str(raw).strip("[]").split(",") if part]
    except ValueError:
        return None


def _cosine(a: list[float], b: list[float]) -> float:
    if len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(y * y for y in b) ** 0.5
    if not norm_a or not norm_b:
        return 0.0
    return dot / (norm_a * norm_b)


def _by_value(facts: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Facts asserting the same value, grouped. Compared on `value_norm` where
    there is one ("thirty (30) days" and "Net 30" are one value); free text falls
    back to a casefolded comparison. Order is stable (first appearance wins group
    0), so `chosen_value_group` on a decision keeps pointing at the value the
    reviewer saw across runs."""
    groups: list[list[dict[str, Any]]] = []
    seen: dict[str, int] = {}
    for fact in facts:
        key = _value_key(fact)
        if key not in seen:
            seen[key] = len(groups)
            groups.append([])
        groups[seen[key]].append(fact)
    return groups


def _value_key(fact: dict[str, Any]) -> str:
    norm = fact.get("value_norm")
    if norm is not None and str(norm).strip():
        return str(norm).strip().casefold()
    value = fact.get("value") or {}
    text = value.get("text") if isinstance(value, dict) else None
    return str(text or fact.get("source_quote") or "").strip().casefold()
