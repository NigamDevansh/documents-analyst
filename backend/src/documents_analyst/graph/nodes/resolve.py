"""Entered every run, first. Makes sure the final document on disk is the one recorded.

`Commit` writes the file and records its sections in one transaction, so in the normal
case there is nothing to do here and this costs one query and one file read. It exists
for the cases where the file and the record can disagree:

| we find | we do |
|---|---|
| nothing recorded yet (version 0) | nothing — no document has been committed |
| the file reads back exactly as recorded | nothing |
| the file is missing, unreadable, or differs | rewrite it from the record |

The rewrite uses the RECORDED sections, not the current slots: the record is what a
human last approved into the document, and the current slots may hold changes nobody has
reviewed yet. Repairing is not committing — the version number does not move, and the
event says the file was restored, not updated.

The worker's discovery pass calls `repair_from_record` too, for an idle pile: a pile with
no new documents is never claimed, so without that a deleted or damaged file would stay
broken until something unrelated changed in the folder.

A hand edit to `final-document.docx` is overwritten here too. The document is generated:
a row whose text differs from the record is a claim no source and no reviewer supports.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from langchain_core.runnables import RunnableConfig

from documents_analyst import deliverable
from documents_analyst.db.repo import commits as commit_repo
from documents_analyst.db.repo import events as event_repo
from documents_analyst.graph.context import RunContext, context_of
from documents_analyst.graph.state import PileRunState
from documents_analyst.register import render
from documents_analyst.settings import Settings
from documents_analyst.worker.lease import Fence

LOG = logging.getLogger(__name__)


def run(state: PileRunState, config: RunnableConfig) -> dict[str, object]:
    ctx = context_of(config)
    fence = Fence(pile_id=state["pile_id"], epoch=state["lease_epoch"])
    with ctx.engine.begin() as conn:
        record = commit_repo.recorded(conn, state["pile_id"])
    if int(record["deliverable_version"] or 0) == 0:
        return {}
    repair_if_needed(ctx, fence, state["run_id"], record)
    return {}


def repair_if_needed(ctx: RunContext, fence: Fence, run_id: str, record: dict[str, Any]) -> bool:
    """Rewrite the file from the record if it does not read back as recorded. Returns
    whether it had to. Also called by `Commit` when a resumed run has nothing new to
    write: a run resumes at the gate, not here, so without that a bad file reported by
    `Reconcile` would be reported again after every answer and never fixed."""
    return repair_from_record(
        ctx.engine, ctx.settings, fence, run_id=run_id, record=record, renew=ctx.renew_lease
    )


def repair_from_record(
    engine: Any,
    settings: Settings,
    fence: Fence,
    *,
    run_id: str,
    record: dict[str, Any],
    renew: Callable[[Fence], None],
) -> bool:
    """`repair_if_needed` without a graph context, for the worker's discovery pass. The
    caller holds the pile's lease; `renew` extends it before the write."""
    sections = deliverable.recorded_sections(record["deliverable_sections"])
    path = deliverable.path_for(settings, str(record["name"]))
    check = deliverable.verify(path, sections)
    if check.ok:
        return False

    LOG.warning("pile %s: final document needs repair: %s", fence.pile_id, check.summary())
    renew(fence)
    with engine.begin() as conn:
        commit_repo.lock_for_write(conn, fence)
        deliverable.write(path, title=render.title(str(record["name"])), sections=sections)
        commit_repo.publish(
            conn,
            fence,
            path=str(path),
            document_sha256=deliverable.document_sha256(sections),
            sections=[s.as_record() for s in sections],
            bump=False,
        )
        event_repo.append(
            conn,
            pile_id=fence.pile_id,
            run_id=run_id,
            type="stage.resolve.done",
            payload={
                "repaired": True,
                "reason": check.summary(),
                "version": int(record["deliverable_version"]),
                "sections": len(sections),
            },
        )
    return True
