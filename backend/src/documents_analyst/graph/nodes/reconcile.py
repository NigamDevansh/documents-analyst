"""Read the final document back off disk, verify it, then re-audit (pass 2, report only).

Runs once, after `Commit`. Two jobs:

- **Verify against the file, not our own belief.** `Commit` recorded what it wrote; this
  reads `final-document.docx` back and compares every section, in order, by SHA-256. A
  success event here means the bytes on disk say what the record says — nothing weaker.
  A mismatch raises an `unverified_document` review item and routes back to the gate
  instead of ending the run as though all were well.
- **Bump `audit_pass` to 2 and route back to Audit:** approving a change can create a
  violation an earlier audit never saw.

Pass 2 records and ENDS the run; its findings wait for the next one, or a fix that
creates a finding that creates a fix loops forever.

See docs/BACKEND_INTERNALS.md § "The post-commit re-audit, and why it terminates".
"""

from __future__ import annotations

import logging

from langchain_core.runnables import RunnableConfig

from documents_analyst import deliverable
from documents_analyst.db.repo import commits as commit_repo
from documents_analyst.db.repo import decisions as decision_repo
from documents_analyst.db.repo import events as event_repo
from documents_analyst.graph.context import RunContext, context_of
from documents_analyst.graph.state import PileRunState
from documents_analyst.worker.lease import Fence

LOG = logging.getLogger(__name__)


def run(state: PileRunState, config: RunnableConfig) -> dict[str, object]:
    ctx = context_of(config)
    pile_id = state["pile_id"]
    fence = Fence(pile_id=pile_id, epoch=state["lease_epoch"])

    with ctx.engine.begin() as conn:
        record = commit_repo.recorded(conn, pile_id)

    if int(record["deliverable_version"] or 0) > 0:
        sections = deliverable.recorded_sections(record["deliverable_sections"])
        path = deliverable.path_for(ctx.settings, str(record["name"]))
        check = deliverable.verify(path, sections)
        if not check.ok:
            return _unverified(ctx, fence, state, check.summary())

        with ctx.engine.begin() as conn:
            ctx.telemetry.flush(conn)
            event_repo.append(
                conn,
                pile_id=pile_id,
                run_id=state["run_id"],
                type="stage.reconcile.done",
                payload={
                    "sections_verified": check.sections_checked,
                    "version": int(record["deliverable_version"]),
                    "document_sha256": record["deliverable_sha256"],
                    "path": str(path),
                },
            )

    # The bump is the whole control-flow contribution of this node: `Audit` reads
    # `audit_pass` and pass 2 refuses to re-open the gate.
    return {"audit_pass": 2}


def _unverified(
    ctx: RunContext, fence: Fence, state: PileRunState, reason: str
) -> dict[str, object]:
    """Fail shut at the human boundary when the document cannot be verified."""
    LOG.warning("pile %s: final document unverified: %s", state["pile_id"], reason)
    with ctx.engine.begin() as conn:
        decision_repo.raise_decision(
            conn,
            fence,
            kind="unverified_document",
            payload={"reason": reason, "stage": "reconcile"},
            detected_by="rule",
            recommendation=(
                "The final document on disk does not match what was recorded as written. "
                "Answering this resumes the run, which restores the file from the record."
            ),
        )
        event_repo.append(
            conn,
            pile_id=state["pile_id"],
            run_id=state["run_id"],
            type="stage.reconcile.unverified",
            payload={"reason": reason},
        )
    # `Audit` will see pass 1 and route to HumanGate, which now has this item.
    return {"audit_pass": 1}
