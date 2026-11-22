"""Commit: carry out what a human approved, then write the final document.

Runs once per run, after `HumanGate` has emptied the review queue. Everything below
happens in ONE database transaction, fenced on the pile's lease:

    1. apply the approved decisions to the slots
         conflict / amendment → the chosen value wins          (slot_state.resolve)
         label_change         → the new heading persists       (label_override)
         orphaned_claims      → the row leaves the document    (retire)
    2. render every section from the slots                     (deliverable.sections_for)
    3. compare against the sections recorded at the last write, by SHA-256
    4. if the document changed: write it (temp file + rename), bump the version, and
       record one `deliverable_versions` row per changed section
    5. record which decisions this commit carried               (deliverable_commits)
    6. ask about rows whose every source was deleted            (gated, raised once)

**Flagging is ungated, choosing and deleting are gated.** A row whose source changed, or
whose source file was deleted, is re-rendered without asking — the document states what
the sources say, and a stale row is a claim no source supports. Choosing between
disagreeing sources, renaming, and deleting a row each need an approved decision.

**A focused update, provably.** Step 3 is what makes "an update costs like an update"
checkable: a new source that moves one term changes one section's hash, gets one version
row, and every other section's hash is byte-identical to the one recorded before. The
`stage.commit.applied` event names the changed rows and counts the untouched ones.

**Crash safety.** The file is written inside the transaction, before it commits. Killed
before the commit: the transaction rolls back, the decisions are still approved-but-
uncommitted, and the next run renders the same sections (rendering is deterministic) and
records them. Killed after: the record and the file agree. `Resolve` covers the one
remaining gap — a file deleted or damaged out from under us — by rewriting it from the
record.

See docs/BACKEND_INTERNALS.md § "Nodes — what each reads and writes".
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from langchain_core.runnables import RunnableConfig

from documents_analyst import deliverable
from documents_analyst.db.repo import commits as commit_repo
from documents_analyst.db.repo import decisions as decision_repo
from documents_analyst.db.repo import documents as document_repo
from documents_analyst.db.repo import events as event_repo
from documents_analyst.db.repo import slots as slot_state
from documents_analyst.deliverable import Section
from documents_analyst.graph.context import context_of
from documents_analyst.graph.nodes import resolve
from documents_analyst.graph.state import PileRunState
from documents_analyst.register import render
from documents_analyst.register import slots as slot_repo
from documents_analyst.register.slots import Slot
from documents_analyst.worker.lease import Fence

LOG = logging.getLogger(__name__)

SLOT_DECISIONS = ("conflict", "amendment", "label_change", "orphaned_claims")
"""Decision kinds whose `ref_id` is a slot — the ones a section change can be attributed
to. A `finding` refers to a finding, not a row; approving one acknowledges it and changes
no text."""


@dataclass(slots=True)
class SectionDiff:
    added: list[Section] = field(default_factory=list)
    edited: list[tuple[Section, Section]] = field(default_factory=list)
    removed: list[Section] = field(default_factory=list)
    unchanged: int = 0

    @property
    def any(self) -> bool:
        return bool(self.added or self.edited or self.removed)


def run(state: PileRunState, config: RunnableConfig) -> dict[str, object]:
    ctx = context_of(config)
    pile_id = state["pile_id"]
    fence = Fence(pile_id=pile_id, epoch=state["lease_epoch"])

    ctx.renew_lease(fence)
    with ctx.engine.begin() as conn:
        # Hold the pile row across validation, application, and publication.
        commit_repo.lock_for_write(conn, fence)
        approved = decision_repo.approved_undecided(conn, pile_id)
        stale = [item for item in approved if not decision_repo.evidence_current(conn, item)]
        for item in stale:
            decision_repo.refresh_stale(conn, fence, item)
        if stale:
            return {"review_pending": True}
        before = {s.slot_id: s for s in slot_repo.for_pile(conn, pile_id)}

        # Apply what was decided BEFORE reading the slots we render: an approved conflict
        # is only a decision until the winning value replaces the disagreement.
        resolved = _apply_resolutions(conn, fence, approved)
        renamed = _apply_renames(conn, fence, approved, before)
        retired = _apply_removals(conn, fence, approved, before)

        slots = slot_repo.for_pile(conn, pile_id)
        asked = decision_repo.slots_already_asked(conn, pile_id, "orphaned_claims")
        orphans = _unflagged_orphans(slots, asked)

        record = commit_repo.recorded(conn, pile_id)
        previous = deliverable.recorded_sections(record["deliverable_sections"])
        sections = deliverable.sections_for(slots)
        inventory = document_repo.for_pile(conn, pile_id)
        lines = [
            "Coverage reflects the files discovered for this analysis. A verified quote proves its source text, not that every material term was identified. Human completeness review is required."
        ]
        for source in inventory:
            paths = []
            for raw in sorted(source.get("paths") or {}):
                try:
                    paths.append(
                        str(Path(raw).relative_to(ctx.settings.watch_root / str(record["name"])))
                    )
                except ValueError:
                    paths.append(Path(raw).name)
            label = "; ".join(paths) or source["filename"]
            warning = ""
            if source["status"] == "quarantined":
                warning = " — NOT ANALYSED: " + str(
                    source.get("quarantine_error") or "unreadable source"
                )
            elif source.get("text_length") == 0:
                warning = " — NO READABLE TEXT: scanned PDFs may require OCR"
            elif source.get("alias_of_document_id"):
                warning = " — duplicate text, represented by its canonical source"
            lines.append(f"{label} [{source['status']}; SHA-256 {source['sha256']}]" + warning)
        if ctx.settings.small_sample:
            lines.insert(
                0, "WARNING: sample mode was enabled; only a subset of sections was analysed."
            )
        sections.append(
            Section(
                slot_id="",
                agreement="Analysis coverage",
                heading="Source inventory and limitations",
                body="\n".join(lines),
            )
        )
        document_sha = deliverable.document_sha256(sections)
        path = deliverable.path_for(ctx.settings, str(record["name"]))
        first_write = int(record["deliverable_version"] or 0) == 0
        diff = _diff(previous, sections)

        version = int(record["deliverable_version"] or 0)
        written = first_write or document_sha != record["deliverable_sha256"]
        if written:
            commit_repo.lock_for_write(conn, fence)
            deliverable.write(path, title=render.title(str(record["name"])), sections=sections)
            version = commit_repo.publish(
                conn,
                fence,
                path=str(path),
                document_sha256=document_sha,
                sections=[s.as_record() for s in sections],
                bump=True,
            )
            _record_changes(conn, pile_id, version, diff, approved, slots, first_write)

        if approved:
            commit_repo.record_commit(
                conn,
                fence,
                decision_ids=[str(d["id"]) for d in approved],
                slot_ids=sorted({str(d["ref_id"]) for d in approved if d.get("ref_id")}),
                version=version,
                document_sha256=document_sha,
            )

        event_repo.append(
            conn, pile_id=pile_id, run_id=state["run_id"], type="run.coverage_updated", payload={}
        )
        flagged = _ask_about_orphans(conn, fence, orphans)
        ctx.telemetry.flush(conn)

        if resolved or renamed or retired:
            event_repo.append(
                conn,
                pile_id=pile_id,
                run_id=state["run_id"],
                type="stage.commit.resolved",
                payload={
                    "slots": len(resolved),
                    "chosen": resolved,
                    "renamed": len(renamed),
                    "removed": len(retired),
                },
            )
        if written:
            event_repo.append(
                conn,
                pile_id=pile_id,
                run_id=state["run_id"],
                type="stage.commit.applied",
                payload={
                    "version": version,
                    "path": str(path),
                    "document_sha256": document_sha,
                    "first_write": first_write,
                    # Named, not counted: "Corrected 3 rows" is not an answer,
                    # "Payment terms, Expiry date, Audit frequency" is.
                    "rows": [new.heading for _, new in diff.edited]
                    + [s.heading for s in diff.added]
                    + [s.heading for s in diff.removed],
                    "added": len(diff.added),
                    "edited": len(diff.edited),
                    "removed": len(diff.removed),
                    # The proof half of a focused update: these sections hash exactly as
                    # they did before this commit.
                    "unchanged": diff.unchanged,
                },
            )
        event_repo.append(
            conn,
            pile_id=pile_id,
            run_id=state["run_id"],
            type="stage.commit.done",
            payload={
                "written": written,
                "version": version,
                "sections": len(sections),
                "decisions_committed": len(approved),
                "orphans_flagged": flagged,
            },
        )
    if not written and not first_write:
        # Nothing new to say, but the file may still be wrong — reported by `Reconcile`
        # on the previous pass, with the run resuming here rather than at `Resolve`.
        with ctx.engine.connect() as conn:
            current = commit_repo.recorded(conn, pile_id)
        resolve.repair_if_needed(ctx, fence, state["run_id"], current)
    LOG.info(
        "pile %s: commit %s (version %s, %d section(s), %d decision(s))",
        pile_id,
        "wrote the document" if written else "left the document unchanged",
        version,
        len(sections),
        len(approved),
    )
    return {"review_pending": False}


def _diff(previous: list[Section], current: list[Section]) -> SectionDiff:
    """Keyed by slot id — a section's identity is the row it renders, so a renamed heading
    is an edit of the same section, not a removal plus an addition."""
    old = {s.slot_id: s for s in previous}
    new = {s.slot_id: s for s in current}
    diff = SectionDiff()
    for slot_id, section in new.items():
        prior = old.get(slot_id)
        if prior is None:
            diff.added.append(section)
        elif prior.sha256 != section.sha256:
            diff.edited.append((prior, section))
        else:
            diff.unchanged += 1
    diff.removed = [s for slot_id, s in old.items() if slot_id not in new]
    return diff


def _record_changes(
    conn: Any,
    pile_id: str,
    version: int,
    diff: SectionDiff,
    approved: list[dict[str, Any]],
    slots: list[Slot],
    first_write: bool,
) -> None:
    """One version row per changed section, attributed: to the decision that authorised
    it, or to what happened in the folder when nobody had to decide anything."""
    by_slot_decision = {
        str(d["ref_id"]): d for d in approved if d["kind"] in SLOT_DECISIONS and d.get("ref_id")
    }
    orphaned = {s.slot_id for s in slots if s.orphaned}

    def attribution(slot_id: str, default: str) -> tuple[str, str | None]:
        decision = by_slot_decision.get(slot_id)
        if decision is not None:
            return str(decision["kind"]), str(decision["id"])
        return default, None

    for section in diff.added:
        reason, decision_id = attribution(
            section.slot_id, "initial" if first_write else "new_field"
        )
        commit_repo.record_section_change(
            conn,
            pile_id=pile_id,
            version=version,
            slot_id=section.slot_id or None,
            label=section.heading,
            operation="create",
            before_text=None,
            after_text=section.body,
            before_sha256=None,
            after_sha256=section.sha256,
            reason=reason,
            decision_id=decision_id,
        )
    for prior, section in diff.edited:
        default = (
            "orphan_flag"
            if section.slot_id in orphaned and render.ORPHANED not in prior.body
            else "source_update"
        )
        reason, decision_id = attribution(section.slot_id, default)
        commit_repo.record_section_change(
            conn,
            pile_id=pile_id,
            version=version,
            slot_id=section.slot_id or None,
            label=section.heading,
            operation="edit",
            before_text=prior.body
            if prior.heading == section.heading
            else f"{prior.heading}: {prior.body}",
            after_text=section.body
            if prior.heading == section.heading
            else f"{section.heading}: {section.body}",
            before_sha256=prior.sha256,
            after_sha256=section.sha256,
            reason=reason,
            decision_id=decision_id,
        )
    for section in diff.removed:
        reason, decision_id = attribution(section.slot_id, "source_update")
        commit_repo.record_section_change(
            conn,
            pile_id=pile_id,
            version=version,
            slot_id=section.slot_id or None,
            label=section.heading,
            operation="delete",
            before_text=section.body,
            after_text=None,
            before_sha256=section.sha256,
            after_sha256=None,
            reason=reason,
            decision_id=decision_id,
        )


def _unflagged_orphans(slots: list[Slot], asked: set[str]) -> list[Slot]:
    """Rows whose every source was deleted, that nobody has been asked about. Deleting a
    contract does not delete what we extracted: the document goes `removed`, its facts stay
    `active`, and the row keeps stating the term with a removal marker (deliberate — a
    grounded claim is not dropped by a file move). So the removal is put to a human once;
    `asked` is what makes it once, since this runs every commit."""
    return [s for s in slots if s.orphaned and s.slot_id not in asked]


def _ask_about_orphans(conn: Any, fence: Fence, orphans: list[Slot]) -> int:
    """Put each flagged row to a human: keep it, or delete it. Gated, unlike the flag:
    saying "the source is gone" is reporting, deleting the row destroys a value somebody
    may rely on. Returns how many were raised."""
    raised = 0
    for slot in orphans:
        decision_id = decision_repo.raise_decision(
            conn,
            fence,
            kind="orphaned_claims",
            ref_id=slot.slot_id,
            payload={
                "label": slot.label,
                "value": slot.current.value_text if slot.current else None,
                "document_ids": sorted(
                    {doc for value in slot.values for doc in value.document_ids}
                ),
            },
            # `rule`: nothing judged anything here — every source document has
            # `status = 'removed'`, a fact about the folder.
            detected_by="rule",
            recommendation=(
                f"Every source for “{slot.label}” has been deleted from the pile folder. "
                "The document now shows the value with a removal warning. "
                "Approve to delete the row, reject to keep it as it is."
            ),
        )
        if decision_id is not None:
            raised += 1
    return raised


def _apply_resolutions(conn: Any, fence: Fence, approved: list[dict[str, Any]]) -> dict[str, int]:
    """Turn approved choices into resolved slots. `{slot_id: value_group}`.

    A conflict needs an explicit choice; an amendment does not — the later document
    supersedes, and the classifier already put that group in
    `payload.recommended_value_group`, which keeps the batch lane honest (approving twelve
    amendments is agreeing to a rule, not twelve unseen numbers). A conflict with no choice
    is skipped and left disputed rather than guessed at.
    """
    applied: dict[str, int] = {}
    for decision in approved:
        if decision["kind"] not in ("conflict", "amendment"):
            continue
        slot_id = str(decision["ref_id"] or "")
        if not slot_id:
            continue

        chosen = decision.get("chosen_value_group")
        if chosen is None and decision["kind"] == "amendment":
            chosen = (decision.get("payload") or {}).get("recommended_value_group")
        if chosen is None:
            LOG.warning(
                "decision %s approved with no chosen value group; slot %s stays disputed",
                decision["id"],
                slot_id,
            )
            continue

        winner = slot_state.resolve(
            conn,
            fence,
            slot_id,
            value_group=int(chosen),
            decided_by=str(decision.get("decided_by") or "unknown"),
        )
        if winner is None:
            # The chosen value no longer has a live fact behind it. Resolving to nothing
            # would drop the disagreement silently, so the slot keeps its state.
            LOG.warning(
                "decision %s chose value group %s on slot %s, which has no live facts",
                decision["id"],
                chosen,
                slot_id,
            )
            continue
        applied[slot_id] = int(chosen)
    return applied


def _apply_renames(
    conn: Any, fence: Fence, approved: list[dict[str, Any]], slots: dict[str, Slot]
) -> list[str]:
    renamed: list[str] = []
    for decision in approved:
        if decision["kind"] != "label_change":
            continue
        slot_id = str(decision["ref_id"] or "")
        new_label = str((decision.get("payload") or {}).get("label") or "").strip()
        if slot_id not in slots or not new_label:
            continue
        slot_repo.set_label_override(conn, fence, slot_id, new_label)
        renamed.append(slot_id)
    return renamed


def _apply_removals(
    conn: Any, fence: Fence, approved: list[dict[str, Any]], slots: dict[str, Slot]
) -> list[str]:
    """Approved `orphaned_claims`: the row leaves the document. Only a slot that is still
    orphaned — if a file was restored since the question was asked, the row is grounded
    again and deleting it would remove a claim a live source supports."""
    retired: list[str] = []
    for decision in approved:
        if decision["kind"] != "orphaned_claims":
            continue
        slot_id = str(decision["ref_id"] or "")
        slot = slots.get(slot_id)
        if slot is None:
            continue
        if not slot.orphaned:
            LOG.info("slot %s has a live source again; not removing it", slot_id)
            continue
        slot_repo.retire(conn, fence, slot_id)
        retired.append(slot_id)
    return retired
