"""Claim slots — (agreement_id, field_key).

An N-way disagreement is one slot with several `value_group`s, not N pairwise
conflicts. Three documents saying 30/45/60 days is ONE question for a human,
asked once.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

from documents_analyst.worker.lease import HOLDS_LEASE, Fence, fenced
from documents_analyst_core.taxonomy.fields import UnknownField, by_key


@dataclass(frozen=True, slots=True)
class SlotValue:
    value_group: int
    value_text: str
    value_normalized: str | None
    document_ids: tuple[str, ...]
    citation_count: int
    citations: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True, slots=True)
class Slot:
    agreement_id: str
    field_key: str
    state: str
    values: tuple[SlotValue, ...]
    slot_id: str = ""
    label_override: str | None = None
    resolved_by: str | None = None
    """Who answered the question this row used to be. A `resolved` row is the one place the
    register states something a person decided rather than something a document said, and
    it read exactly like the rest — the name is the audit trail this system exists to
    keep."""
    subject_free: str | None = None
    agreement_title: str | None = None
    agreement_parties: tuple[str, ...] = ()
    orphaned: bool = False
    """Every document behind this row's live values has been deleted. Not the same as
    having no values: an orphaned slot still states a term, with a citation that still
    resolves — `documents.extracted_text` is write-once and stays in Postgres after the
    file leaves, so the source pane still opens and highlights the sentence. What's gone is
    the file it came from, and a reader is entitled to know that before relying on the row.
    Defaults to `False` so a `Slot` built by hand in a test is an ordinary slot."""

    @property
    def label(self) -> str:
        """What the register shows as the row's name. `label_override` wins, and that's the
        whole reason the column exists: register content is generated from the taxonomy, so
        without it the next render would re-emit the taxonomy label and silently undo a
        rename a human made."""
        if self.label_override:
            return self.label_override
        if self.subject_free:
            return self.subject_free.title()
        try:
            return by_key(self.field_key).label
        except UnknownField:
            return self.field_key.replace("_", " ").title()

    @property
    def agreement_label(self) -> str:
        """Which contract this term belongs to. A pile is a folder of contracts, so most
        piles have several, and without this the register was one flat list: `Payment terms`
        three times, three values, nothing saying they came from three different agreements
        — a reader sees a contradiction that isn't one, in the document whose whole job is to
        say which terms disagree. Parties first, because that's what identifies a contract to
        a person; `title` is the first document's filename, a fallback, and an agreement
        whose parties couldn't be read gets a phrase that admits it."""
        if self.agreement_parties:
            return " ↔ ".join(party.title() for party in self.agreement_parties)
        if self.agreement_title:
            return str(self.agreement_title)
        return "Unattributed terms"

    @property
    def current(self) -> SlotValue | None:
        """The value the register displays, or None while it's disputed. None is deliberate
        and load-bearing: a disputed slot shows the disagreement rather than picking a side,
        and rendering the first group would state a value nobody chose and give it a settled
        slot's weight."""
        if self.state in ("disputed", "contested") or not self.values:
            return None
        return self.values[0]


SLOT_ROWS = text("""
    SELECT s.id, s.agreement_id, s.field_key, s.subject_free, s.state,
           s.label_override, s.resolved_by, a.title AS agreement_title,
           a.parties_normalized AS agreement_parties,
           f.id AS fact_id, f.value, f.value_norm, f.value_group,
           f.source_document_id, d.status AS source_status, d.filename, d.paths, p.watch_path AS source_root, f.source_quote, f.char_start, f.char_end
      FROM claim_slots s
      JOIN agreements a ON a.id = s.agreement_id
      JOIN piles p ON p.id = a.pile_id
 LEFT JOIN facts_and_claims f
        ON f.slot_id = s.id AND f.status = 'active' AND f.role <> 'superseded'
 LEFT JOIN documents d ON d.id = f.source_document_id
     WHERE a.pile_id = :pile_id AND s.retired_at IS NULL
  ORDER BY a.created_at, s.field_key NULLS LAST, s.subject_free, f.value_group, f.char_start, f.id
""")
"""`LEFT JOIN`, because a slot with no live facts is still a slot. Every fact behind it can
be superseded or removed while the row stays in the register — the orphaned-claims case,
and an inner join would make those rows vanish from the render instead of being flagged.
`retired_at IS NULL` because a slot whose row a human approved deleting is no longer part
of the register — left in, the next render would put the row straight back. `d.status` is why the second LEFT JOIN is here: deleting
a file marks its document `removed` but leaves its facts `active`, so "every source behind
this row is gone" can't be read from the facts alone — it has to come from the documents
they came from, and this is the only query that assembles a slot."""


def for_pile(conn: Connection, pile_id: str) -> list[Slot]:
    """Every slot in the pile, with its value groups assembled. One query and a fold rather
    than a query per slot: the register is rendered on every commit and reconcile, and N+1
    over a 28-field agreement is 28 round trips to draw one page."""
    rows = conn.execute(SLOT_ROWS, {"pile_id": pile_id}).all()
    building: dict[str, dict[str, Any]] = {}
    live_slots = {
        str(row.id)
        for row in rows
        if row.fact_id is not None
        and row.source_status not in ("removed", "superseded", "quarantined")
    }

    for row in rows:
        slot = building.setdefault(
            str(row.id),
            {
                "slot_id": str(row.id),
                "agreement_id": str(row.agreement_id),
                "field_key": row.field_key or "",
                "subject_free": row.subject_free,
                "state": row.state,
                "label_override": row.label_override,
                "resolved_by": row.resolved_by,
                "agreement_title": row.agreement_title,
                "agreement_parties": tuple(row.agreement_parties or ()),
                "groups": {},
                "live_sources": 0,
            },
        )
        if row.fact_id is None:
            continue
        if str(row.id) in live_slots and row.source_status in (
            "removed",
            "superseded",
            "quarantined",
        ):
            continue
        # Counted per FACT, not per document: one surviving source is enough to keep the
        # row grounded, and a slot with three values where two files were deleted is still
        # answerable from the third.
        if row.source_status != "removed":
            slot["live_sources"] += 1
        group = slot["groups"].setdefault(
            int(row.value_group or 0),
            {
                "value_text": "",
                "value_norm": row.value_norm,
                "documents": [],
                "citations": 0,
                "sources": [],
            },
        )
        value = row.value or {}
        group["value_text"] = group["value_text"] or str(value.get("text") or "")
        group["documents"].append(str(row.source_document_id))
        group["citations"] += 1
        relative = []
        for raw in sorted(row.paths or {}):
            try:
                relative.append(str(Path(raw).relative_to(row.source_root)))
            except ValueError:
                relative.append(Path(raw).name)
        filename = "; ".join(relative) or row.filename
        group["sources"].append(
            {
                "filename": filename,
                "document_id": str(row.source_document_id),
                "quote": row.source_quote,
                "start": row.char_start,
                "end": row.char_end,
            }
        )

    return [
        Slot(
            slot_id=s["slot_id"],
            agreement_id=s["agreement_id"],
            field_key=s["field_key"],
            subject_free=s["subject_free"],
            state=s["state"],
            label_override=s["label_override"],
            resolved_by=s["resolved_by"],
            agreement_title=s["agreement_title"],
            agreement_parties=s["agreement_parties"],
            # A slot with no values at all is not orphaned, it's empty — and `_body`
            # already renders that. Orphaned is the narrower, more misleading state: a row
            # still stating a value whose every source has left the folder.
            orphaned=bool(s["groups"]) and s["live_sources"] == 0,
            values=tuple(
                SlotValue(
                    value_group=index,
                    value_text=g["value_text"],
                    value_normalized=g["value_norm"],
                    document_ids=tuple(dict.fromkeys(g["documents"])),
                    citation_count=g["citations"],
                    citations=tuple(g["sources"]),
                )
                for index, g in sorted(s["groups"].items())
            ),
        )
        for s in building.values()
    ]


def retire(conn: Connection, fence: Fence, slot_id: str) -> None:
    """An approved removal: this slot leaves the document. The facts stay — this is a
    decision about the deliverable, not the evidence — so the citations remain answerable
    and the version history still says what the row held and who removed it."""
    fenced(
        conn,
        text(
            "UPDATE claim_slots SET retired_at = now(), "
            f"updated_at = now() WHERE id = :id AND {HOLDS_LEASE}"
        ),
        {"id": slot_id, "pile_id": fence.pile_id, "epoch": fence.epoch},
    )


def set_label_override(conn: Connection, fence: Fence, slot_id: str, label: str) -> None:
    """A rename a human approved. The document is generated from the taxonomy, so without
    this the next render would put the taxonomy label back and silently undo it."""
    fenced(
        conn,
        text(
            "UPDATE claim_slots SET label_override = :l, updated_at = now() WHERE id = :id "
            f"AND {HOLDS_LEASE}"
        ),
        {"id": slot_id, "l": label, "pile_id": fence.pile_id, "epoch": fence.epoch},
    )
