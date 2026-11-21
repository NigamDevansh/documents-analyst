"""Commit, resolve and reconcile — the final document, end to end, against a real Postgres.

The graph runs for real (`runner.run_pile`), with scripted models so no key and no network
is involved. What is asserted is the file on disk and the record beside it, because the
claims worth testing are about both:

- the document is written into the pile's own folder, and never read back in as a source;
- an update is focused — a new source that moves one term changes one section, and every
  other section's SHA-256 is byte-identical to the one recorded before;
- a decision reaches the document exactly once;
- a missing or damaged file is restored from the record without a new version;
- a write that does not read back as recorded is reported, never claimed as success;
- a crash between writing the file and recording it loses nothing and duplicates nothing.
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from documents_analyst import deliverable
from documents_analyst.db import migrate
from documents_analyst.db.repo import commits as commit_repo
from documents_analyst.db.repo import decisions as decision_repo
from documents_analyst.db.repo import documents as doc_repo
from documents_analyst.db.repo import piles as pile_repo
from documents_analyst.graph.nodes import commit as commit_node
from documents_analyst.models import LoadedModels
from documents_analyst.register import render
from documents_analyst.register import slots as slot_repo
from documents_analyst.settings import Settings
from documents_analyst.worker import lease, runner
from documents_analyst.worker.ingest import ingest_pile
from documents_analyst.worker.lease import Fence, LostLease
from documents_analyst_core.portable.llm_client import Usage
from documents_analyst_core.portable.ner_gliner import Span

pytestmark = pytest.mark.db

SCRATCH = "analyst_commit_test"
FILENAME = "final-document.docx"

PREAMBLE = "This Agreement is entered into between Acme Corporation and Beta Logistics Inc.\n\n"
PRICE = "The monthly fee shall be USD 10,000."
TERMS = "Invoices are payable within 30 days."
NEW_TERMS = "Invoices are payable within 45 days."
MSA = PREAMBLE + f"1. Fees\n\n{PRICE}\n\n2. Payment\n\n{TERMS}\n"
REVISED = MSA.replace(TERMS, NEW_TERMS)

NOTES = PREAMBLE + "General correspondence. No commercial terms are stated here.\n"
"""A second document with the same parties and no extractable field. A pile whose every
document is `removed` is not claimable, so deleting the contract needs a survivor for the
next run to demonstrate anything — and it is the realistic shape: you delete one contract
out of a folder, not the folder."""


# ── doubles ───────────────────────────────────────────────────────────────


def fact_at(body: str, head: str, quote: str, field_key: str, value: str) -> dict[str, Any]:
    start = body.index(head)
    local = body.index(quote) - start
    return {
        "field_key": field_key,
        "value_text": value,
        "source_quote": quote,
        "start": local,
        "end": local + len(quote),
        "confidence": 0.9,
    }


def facts_for(body: str) -> list[dict[str, Any]]:
    out = [fact_at(body, "1. Fees", PRICE, "recurring_price", "USD 10,000")]
    for terms, value in ((TERMS, "30 days"), (NEW_TERMS, "45 days")):
        if terms in body:
            out.append(fact_at(body, "2. Payment", terms, "payment_terms_days", value))
    return out


class ScriptedLLM:
    """Returns the facts whose quote is in the section it was shown — so one script
    serves the original contract and the revised one."""

    def __init__(self, *, facts: list[dict[str, Any]]) -> None:
        self.model = "scripted-1"
        self.facts = facts

    def structured(
        self, *, system: str, user: str, schema: dict[str, Any], max_tokens: int
    ) -> tuple[dict[str, Any], Usage]:
        properties = (schema.get("properties") or {}).keys()
        usage = Usage(model=self.model, input_tokens=10, output_tokens=5)
        if "party_indices" in properties:
            return {"party_indices": [0, 1]}, usage
        if "verdict" in properties:
            return {"verdict": "conflict", "reason": "they differ"}, usage
        if "findings" in properties:
            return {"findings": []}, usage
        if "subjects" in properties:
            return {"subjects": []}, usage
        return {"facts": [f for f in self.facts if f["source_quote"] in user]}, usage

    def complete(self, *, system: str, user: str, max_tokens: int) -> tuple[str, Usage]:
        raise AssertionError("no unstructured calls")


class FixedEmbeddings:
    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [[0.1] * 384 for _ in texts]


class ScriptedNER:
    enabled = True
    is_available = True

    def extract(self, text: str, labels: list[str], *, threshold: float = 0.5) -> list[Span]:
        out = []
        for name in ("Acme Corporation", "Beta Logistics Inc"):
            start = text.find(name)
            if start >= 0:
                out.append(
                    Span(
                        text=name,
                        label="organization",
                        start=start,
                        end=start + len(name),
                        score=0.9,
                    )
                )
        return out


def models() -> LoadedModels:
    return LoadedModels(  # type: ignore[arg-type]
        embeddings=FixedEmbeddings(), nli=object(), ner=ScriptedNER()
    )


def settings_for(url: str, watch_root: Path) -> Settings:
    return Settings(
        database_url=url,
        watch_root=watch_root,
        lease_ttl_s=90,
        settle_seconds=1,
        worker_id="test-worker",
    )


# ── fixtures ──────────────────────────────────────────────────────────────


def dsn(engine: Engine) -> str:
    return engine.url.render_as_string(hide_password=False)


@pytest.fixture(scope="module")
def engine() -> Iterator[Engine]:
    url = os.environ.get("DATABASE_URL")
    if not url:
        pytest.skip("DATABASE_URL not set — needs a live Postgres")

    base = url.rsplit("/", 1)[0]
    admin = create_engine(base + "/postgres", isolation_level="AUTOCOMMIT", future=True)
    try:
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{SCRATCH}"'))
            conn.execute(text(f'CREATE DATABASE "{SCRATCH}"'))
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"cannot reach Postgres: {exc}")

    scratch_url = f"{base}/{SCRATCH}"
    migrate.run(scratch_url)
    made = create_engine(scratch_url, future=True)
    yield made

    made.dispose()
    with admin.connect() as conn:
        conn.execute(
            text(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = :d AND pid <> pg_backend_pid()"
            ),
            {"d": SCRATCH},
        )
        conn.execute(text(f'DROP DATABASE IF EXISTS "{SCRATCH}"'))
    admin.dispose()


@pytest.fixture(autouse=True)
def clean(engine: Engine) -> Iterator[None]:
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE piles CASCADE"))
        conn.execute(text("TRUNCATE stage_events, stage_runs"))
    yield


def _write(path: Path, body: str) -> None:
    path.write_text(body)
    when = time.time() - 60
    os.utime(path, (when, when))


def seed(engine: Engine, tmp_path: Path, name: str, *, notes: bool = False) -> tuple[str, Path]:
    """A pile with the contract (and optionally a survivor). Returns the pile id and the
    contract's path."""
    pile_dir = tmp_path / name
    pile_dir.mkdir(parents=True, exist_ok=True)
    contract = pile_dir / "01_msa.md"
    _write(contract, MSA)
    if notes:
        _write(pile_dir / "02_notes.md", NOTES)
    with engine.begin() as conn:
        pile_id = pile_repo.ensure(conn, name=name, watch_path=pile_dir)
    rescan(engine, pile_id, pile_dir)
    return pile_id, contract


def rescan(engine: Engine, pile_id: str, pile_dir: Path) -> None:
    with engine.begin() as conn:
        epoch = lease.acquire(conn, pile_id, "seeder", ttl_s=90)
    assert epoch is not None
    ingest_pile(
        engine,
        fence=Fence(pile_id=pile_id, epoch=epoch),
        watch_path=pile_dir,
        settle_seconds=1,
        deep=True,
    )
    with engine.begin() as conn:
        lease.release(conn, pile_id, epoch)


def run(engine: Engine, tmp_path: Path, *, body: str = MSA) -> Any:
    return runner.run_pile(
        engine,
        InMemorySaver(),
        settings_for(dsn(engine), tmp_path),
        models(),
        ScriptedLLM(facts=facts_for(body)),
        worker_id="test-worker",
    )


def rewind(engine: Engine, pile_id: str) -> None:
    """Make the pile claimable again without resurrecting a deleted document."""
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE documents SET status = 'ingested' "
                "WHERE pile_id = :p AND status NOT IN ('removed', 'quarantined')"
            ),
            {"p": pile_id},
        )
        conn.execute(text("UPDATE piles SET status = 'idle' WHERE id = :p"), {"p": pile_id})


def record(engine: Engine, pile_id: str) -> dict[str, Any]:
    with engine.connect() as conn:
        return commit_repo.recorded(conn, pile_id)


def versions(engine: Engine, pile_id: str) -> list[Any]:
    with engine.connect() as conn:
        return conn.execute(
            text(
                "SELECT version, section_label, operation, before_text, after_text, "
                "before_sha256, after_sha256, reason, decision_id "
                "FROM deliverable_versions WHERE pile_id = :p ORDER BY version, created_at"
            ),
            {"p": pile_id},
        ).all()


def events(engine: Engine, pile_id: str, run_id: str | None = None) -> list[dict[str, Any]]:
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT type, payload FROM stage_events WHERE pile_id = :p "
                + ("AND run_id = :r " if run_id else "")
                + "ORDER BY id"
            ),
            {"p": pile_id, "r": run_id},
        ).mappings()
        return [dict(r) for r in rows]


def approve(engine: Engine, pile_id: str, kind: str, slot_id: str, payload: str = "{}") -> str:
    with engine.begin() as conn:
        return str(
            conn.execute(
                text(
                    "INSERT INTO pending_decisions (pile_id, kind, ref_id, status, decided_by, "
                    "decided_at, payload) VALUES (:p, :k, :ref, 'approved', 'reviewer', now(), "
                    "CAST(:payload AS jsonb)) RETURNING id"
                ),
                {"p": pile_id, "k": kind, "ref": slot_id, "payload": payload},
            ).scalar_one()
        )


def slot_for(engine: Engine, pile_id: str, field_key: str) -> slot_repo.Slot:
    with engine.connect() as conn:
        return next(s for s in slot_repo.for_pile(conn, pile_id) if s.field_key == field_key)


# ── the document comes into existence ─────────────────────────────────────


def test_the_first_run_writes_the_final_document_into_the_pile_folder(
    engine: Engine, tmp_path: Path
) -> None:
    pile_id, contract = seed(engine, tmp_path, "first")
    outcome = run(engine, tmp_path)
    assert outcome is not None and not outcome.paused

    path = contract.parent / FILENAME
    assert path.is_file(), "written next to the sources it was built from"
    assert not list(contract.parent.glob(".*.tmp")), "no temp file left behind"

    rec = record(engine, pile_id)
    assert rec["deliverable_version"] == 1
    recorded = deliverable.recorded_sections(rec["deliverable_sections"])
    assert {s.body for s in recorded} == {"USD 10,000", "30 days"}
    assert rec["deliverable_sha256"] == deliverable.document_sha256(recorded)

    on_disk = deliverable.read(path)
    assert [(s.agreement, s.heading, s.body) for s in on_disk] == [
        (s.agreement, s.heading, s.body) for s in recorded
    ], "the file says exactly what was recorded"

    rows = versions(engine, pile_id)
    assert [(r.operation, r.reason) for r in rows] == [("create", "initial")] * 2

    types = [e["type"] for e in events(engine, pile_id, outcome.run_id)]
    assert "stage.commit.applied" in types
    assert "stage.reconcile.done" in types, "and it was read back and verified"
    assert types[-1] == "run.finished"


def test_the_final_document_is_never_ingested_as_a_source(engine: Engine, tmp_path: Path) -> None:
    """The document lives in the watched folder, so the scanner must skip exactly that
    name at the pile root — or every commit would produce a new "source" and the system
    would start extracting facts from its own output."""
    pile_id, contract = seed(engine, tmp_path, "selfread")
    assert run(engine, tmp_path) is not None
    assert (contract.parent / FILENAME).is_file()

    rescan(engine, pile_id, contract.parent)

    with engine.connect() as conn:
        paths = sorted(Path(p).name for p in doc_repo.path_map(conn, pile_id))
    assert FILENAME not in paths
    assert paths == ["01_msa.md"]


def test_a_subfolder_file_with_the_same_name_is_still_a_source(tmp_path: Path) -> None:
    """Skipped at the pile root only — a person's own file in a subfolder is read."""
    from documents_analyst.worker.scanner import scan_pile

    pile = tmp_path / "pile"
    (pile / "inbox").mkdir(parents=True)
    (pile / FILENAME).write_bytes(b"x")
    (pile / "inbox" / FILENAME).write_bytes(b"x")
    names = [str(e.path.relative_to(pile)) for e in scan_pile(pile, skip_root_names=(FILENAME,))]
    assert names == [f"inbox/{FILENAME}"]


# ── a focused update ──────────────────────────────────────────────────────


def test_a_changed_source_updates_only_the_section_it_affects(
    engine: Engine, tmp_path: Path
) -> None:
    """Edit one term in a contract. The section for that term changes; the other section
    is byte-identical — same SHA-256 as before — and gets no version row. That is the
    proof an update cost like an update rather than a rewrite."""
    pile_id, contract = seed(engine, tmp_path, "focused")
    assert run(engine, tmp_path) is not None
    before = {
        s.slot_id: s
        for s in deliverable.recorded_sections(record(engine, pile_id)["deliverable_sections"])
    }

    _write(contract, REVISED)
    rescan(engine, pile_id, contract.parent)
    outcome = run(engine, tmp_path, body=REVISED)
    assert outcome is not None

    rec = record(engine, pile_id)
    after = {s.slot_id: s for s in deliverable.recorded_sections(rec["deliverable_sections"])}
    price = slot_for(engine, pile_id, "recurring_price").slot_id
    terms = slot_for(engine, pile_id, "payment_terms_days").slot_id

    assert rec["deliverable_version"] == 2
    assert after[price].sha256 == before[price].sha256, "the untouched section is identical"
    assert after[terms].body == "45 days"
    assert after[terms].sha256 != before[terms].sha256

    changed = [r for r in versions(engine, pile_id) if r.version == 2]
    assert len(changed) == 1, "one changed section, one row"
    assert changed[0].operation == "edit"
    assert changed[0].reason == "source_update", "no decision was needed — one live value"
    assert (changed[0].before_text, changed[0].after_text) == ("30 days", "45 days")
    assert changed[0].before_sha256 == before[terms].sha256

    applied = [
        e for e in events(engine, pile_id, outcome.run_id) if e["type"] == "stage.commit.applied"
    ]
    assert len(applied) == 1
    assert applied[0]["payload"]["edited"] == 1
    assert applied[0]["payload"]["unchanged"] == 1
    assert "45 days" in [x.body for x in deliverable.read(contract.parent / FILENAME)]


def test_a_document_that_already_agrees_is_left_alone(engine: Engine, tmp_path: Path) -> None:
    """Nothing changed, so nothing is written: no new version, no rewrite of the file."""
    pile_id, contract = seed(engine, tmp_path, "steady")
    assert run(engine, tmp_path) is not None
    path = contract.parent / FILENAME
    written = path.stat().st_mtime_ns

    rewind(engine, pile_id)
    outcome = run(engine, tmp_path)
    assert outcome is not None

    assert record(engine, pile_id)["deliverable_version"] == 1
    assert path.stat().st_mtime_ns == written, "the file was not rewritten"
    types = [e["type"] for e in events(engine, pile_id, outcome.run_id)]
    assert "stage.commit.applied" not in types
    done = [e for e in events(engine, pile_id, outcome.run_id) if e["type"] == "stage.commit.done"]
    assert done and done[-1]["payload"]["written"] is False


# ── decisions reach the document once ─────────────────────────────────────


def test_an_approved_rename_reaches_the_document_exactly_once(
    engine: Engine, tmp_path: Path
) -> None:
    pile_id, contract = seed(engine, tmp_path, "rename")
    assert run(engine, tmp_path) is not None
    slot_id = slot_for(engine, pile_id, "recurring_price").slot_id
    decision_id = approve(engine, pile_id, "label_change", slot_id, '{"label": "Monthly Fee"}')

    rewind(engine, pile_id)
    assert run(engine, tmp_path) is not None

    headings = [s.heading for s in deliverable.read(contract.parent / FILENAME)]
    assert "Monthly Fee" in headings
    rows = [r for r in versions(engine, pile_id) if r.version == 2]
    assert [(r.operation, r.reason, str(r.decision_id)) for r in rows] == [
        ("edit", "label_change", decision_id)
    ], "attributed to the decision that authorised it"
    with engine.connect() as conn:
        assert decision_repo.approved_undecided(conn, pile_id) == []
        carried = conn.execute(
            text("SELECT count(*) FROM deliverable_commits WHERE :d = ANY(decision_ids)"),
            {"d": decision_id},
        ).scalar_one()
    assert carried == 1

    rewind(engine, pile_id)
    assert run(engine, tmp_path) is not None
    assert record(engine, pile_id)["deliverable_version"] == 2, "not applied a second time"
    with engine.connect() as conn:
        again = conn.execute(
            text("SELECT count(*) FROM deliverable_commits WHERE :d = ANY(decision_ids)"),
            {"d": decision_id},
        ).scalar_one()
    assert again == 1


# ── a source file is deleted ──────────────────────────────────────────────


def test_deleting_a_contract_flags_its_terms_and_asks_whether_to_remove_them(
    engine: Engine, tmp_path: Path
) -> None:
    """Flagging is ungated — the row is misleading right now. Removing it is gated,
    because deleting a grounded claim someone may have acted on is not a consequence a
    file move gets to have."""
    pile_id, contract = seed(engine, tmp_path, "orphaned", notes=True)
    assert run(engine, tmp_path) is not None

    contract.unlink()
    rescan(engine, pile_id, contract.parent)
    rewind(engine, pile_id)
    assert run(engine, tmp_path) is not None

    with engine.connect() as conn:
        live = slot_repo.for_pile(conn, pile_id)
        items = decision_repo.open_items(conn, pile_id)
    assert all(s.orphaned for s in live)
    bodies = [s.body for s in deliverable.read(contract.parent / FILENAME)]
    assert f"USD 10,000 — {render.ORPHANED}" in bodies, "the warning travels with the value"

    flagged = [r for r in versions(engine, pile_id) if r.version == 2]
    assert {r.reason for r in flagged} == {"orphan_flag"}
    assert all(r.decision_id is None for r in flagged), "ungated: no decision behind it"

    asked = [i for i in items if i["kind"] == "orphaned_claims"]
    assert len(asked) == len(live), "a human is asked about each row"
    assert "deleted from the pile folder" in asked[0]["recommendation"]

    rewind(engine, pile_id)
    assert run(engine, tmp_path) is not None
    with engine.connect() as conn:
        asked_again = [
            i for i in decision_repo.open_items(conn, pile_id) if i["kind"] == "orphaned_claims"
        ]
    assert len(asked_again) == len(asked), "asked once, not once per run"


def test_an_approved_removal_deletes_the_row(engine: Engine, tmp_path: Path) -> None:
    pile_id, contract = seed(engine, tmp_path, "removed", notes=True)
    assert run(engine, tmp_path) is not None
    contract.unlink()
    rescan(engine, pile_id, contract.parent)
    slot_id = slot_for(engine, pile_id, "recurring_price").slot_id
    decision_id = approve(engine, pile_id, "orphaned_claims", slot_id)

    rewind(engine, pile_id)
    assert run(engine, tmp_path) is not None

    with engine.connect() as conn:
        retired = conn.execute(
            text("SELECT retired_at FROM claim_slots WHERE id = :s"), {"s": slot_id}
        ).scalar_one()
        facts = conn.execute(
            text("SELECT count(*) FROM facts_and_claims WHERE slot_id = :s"), {"s": slot_id}
        ).scalar_one()
        left = [i["kind"] for i in decision_repo.open_items(conn, pile_id)]
    assert retired is not None
    assert facts == 1, "the evidence is kept — this was a decision about the document"
    assert "unverified_document" not in left
    headings = [s.heading for s in deliverable.read(contract.parent / FILENAME)]
    assert slot_for_label(engine, slot_id) not in headings
    deleted = [r for r in versions(engine, pile_id) if r.operation == "delete"]
    assert [(r.reason, str(r.decision_id)) for r in deleted] == [("orphaned_claims", decision_id)]


def slot_for_label(engine: Engine, slot_id: str) -> str:
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT field_key FROM claim_slots WHERE id = :s"), {"s": slot_id}
        ).one()
    from documents_analyst_core.taxonomy.fields import by_key

    return by_key(row.field_key).label


# ── re-entry and honesty ──────────────────────────────────────────────────


@pytest.mark.parametrize("damage", ["deleted", "edited by hand"])
def test_a_missing_or_damaged_document_is_restored_from_the_record(
    engine: Engine, tmp_path: Path, damage: str
) -> None:
    """Restoring is not committing: the version does not move."""
    pile_id, contract = seed(engine, tmp_path, f"repair-{damage.split()[0]}")
    assert run(engine, tmp_path) is not None
    path = contract.parent / FILENAME
    recorded = deliverable.recorded_sections(record(engine, pile_id)["deliverable_sections"])

    if damage == "deleted":
        path.unlink()
    else:
        tampered = [
            deliverable.Section(s.slot_id, s.agreement, s.heading, "USD 1") for s in recorded
        ]
        deliverable.write(path, title="tampered", sections=tampered)

    rewind(engine, pile_id)
    outcome = run(engine, tmp_path)
    assert outcome is not None

    assert deliverable.verify(path, recorded).ok
    assert record(engine, pile_id)["deliverable_version"] == 1
    repaired = [
        e for e in events(engine, pile_id, outcome.run_id) if e["type"] == "stage.resolve.done"
    ]
    assert repaired and repaired[0]["payload"]["repaired"] is True


@pytest.mark.parametrize("damage", ["deleted", "edited by hand"])
def test_an_idle_pile_gets_its_document_back_on_the_next_scan(
    engine: Engine, tmp_path: Path, damage: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An idle pile has no work, so no run is ever claimed and Resolve never sees it. The
    discovery pass checks the file instead — without that, deleting final-document.docx
    left it gone until some unrelated change in the folder triggered a run."""
    from documents_analyst.worker import main as worker_main

    monkeypatch.setattr(worker_main, "_VERIFIED", {})
    pile_id, contract = seed(engine, tmp_path, f"idle-{damage.split()[0]}")
    assert run(engine, tmp_path) is not None
    path = contract.parent / FILENAME
    recorded = deliverable.recorded_sections(record(engine, pile_id)["deliverable_sections"])
    settings = settings_for(dsn(engine), tmp_path)

    def discovery_pass() -> bool:
        with engine.begin() as conn:
            epoch = lease.acquire(conn, pile_id, "scanner", settings.lease_ttl_s)
        assert epoch is not None
        try:
            return worker_main.check_final_document(
                engine, settings, Fence(pile_id=pile_id, epoch=epoch)
            )
        finally:
            with engine.begin() as conn:
                lease.release(conn, pile_id, epoch)

    assert discovery_pass() is False, "an intact file is left alone"

    if damage == "deleted":
        path.unlink()
    else:
        tampered = [
            deliverable.Section(s.slot_id, s.agreement, s.heading, "USD 1") for s in recorded
        ]
        deliverable.write(path, title="tampered", sections=tampered)

    assert discovery_pass() is True
    assert deliverable.verify(path, recorded).ok
    assert record(engine, pile_id)["deliverable_version"] == 1, "restoring is not committing"

    calls: list[Path] = []
    real_verify = deliverable.verify
    monkeypatch.setattr(deliverable, "verify", lambda p, s: calls.append(p) or real_verify(p, s))
    assert discovery_pass() is False
    assert calls == [], "an unchanged file is not re-read every pass"


def test_a_write_that_does_not_read_back_is_reported_not_claimed(
    engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reconcile reads the file off disk. If it doesn't say what was recorded, the run
    does not end in success — it raises a review item and stops at the gate."""
    real_write = deliverable.write

    def lossy(path: Path, *, title: str, sections: list[deliverable.Section]) -> None:
        real_write(path, title=title, sections=sections[:-1])

    monkeypatch.setattr(deliverable, "write", lossy)
    pile_id, _ = seed(engine, tmp_path, "lossy")
    outcome = run(engine, tmp_path)
    assert outcome is not None

    types = [e["type"] for e in events(engine, pile_id, outcome.run_id)]
    assert "stage.reconcile.unverified" in types
    assert "stage.reconcile.done" not in types
    with engine.connect() as conn:
        kinds = [i["kind"] for i in decision_repo.open_items(conn, pile_id)]
    assert "unverified_document" in kinds
    assert outcome.paused, "a person is asked before anything else happens"


def test_answering_an_unverified_document_repairs_it_instead_of_asking_again(
    engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run resumes at the gate, not at Resolve. So after the reviewer answers the
    `unverified_document` item, Commit — with nothing new to write — must restore the
    file itself, or Reconcile would report the same bad file after every answer."""
    real_write = deliverable.write

    def lossy(path: Path, *, title: str, sections: list[deliverable.Section]) -> None:
        real_write(path, title=title, sections=sections[:-1])

    monkeypatch.setattr(deliverable, "write", lossy)
    pile_id, contract = seed(engine, tmp_path, "answered")
    saver = InMemorySaver()
    settings = settings_for(dsn(engine), tmp_path)
    llm = ScriptedLLM(facts=facts_for(MSA))
    first = runner.run_pile(engine, saver, settings, models(), llm, worker_id="test-worker")
    assert first is not None and first.paused
    monkeypatch.undo()

    with engine.begin() as conn:
        item = next(
            i for i in decision_repo.open_items(conn, pile_id) if i["kind"] == "unverified_document"
        )
        decision_repo.settle(conn, str(item["id"]), status="approved", actor="reviewer")

    resumed = runner.resume_after_approval(
        engine, saver, settings, models(), llm, pile_id=pile_id, worker_id="test-worker"
    )
    assert resumed is not None and not resumed.paused, "the run finishes"

    rec = record(engine, pile_id)
    recorded = deliverable.recorded_sections(rec["deliverable_sections"])
    assert deliverable.verify(contract.parent / FILENAME, recorded).ok, "the file was restored"
    assert rec["deliverable_version"] == 1, "restoring is not a new version"
    with engine.connect() as conn:
        again = [
            i for i in decision_repo.open_items(conn, pile_id) if i["kind"] == "unverified_document"
        ]
    assert again == [], "and nobody is asked the same question twice"


def test_a_worker_that_lost_its_lease_cannot_take_the_write_lock(
    engine: Engine, tmp_path: Path
) -> None:
    pile_id, _ = seed(engine, tmp_path, "stale")
    with engine.begin() as conn:
        epoch = lease.acquire(conn, pile_id, "first", ttl_s=90)
    assert epoch is not None
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE piles SET lease_epoch = lease_epoch + 1 WHERE id = :p"), {"p": pile_id}
        )
    with pytest.raises(LostLease), engine.begin() as conn:
        commit_repo.lock_for_write(conn, Fence(pile_id=pile_id, epoch=epoch))


def test_a_crash_after_writing_the_file_loses_nothing_and_duplicates_nothing(
    engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The file is written inside the transaction that records it. Killed between the
    two, the record rolls back; the next run renders the same sections and records them
    once."""
    pile_id, contract = seed(engine, tmp_path, "crash")
    slot_ids: list[str] = []

    def boom(*_a: Any, **_k: Any) -> int:
        slot_ids.append("written")
        raise RuntimeError("killed mid-commit")

    monkeypatch.setattr(commit_node.commit_repo, "publish", boom)
    with pytest.raises(RuntimeError, match="killed mid-commit"):
        run(engine, tmp_path)
    assert slot_ids == ["written"]
    assert (contract.parent / FILENAME).is_file(), "the file got ahead of the record"
    assert record(engine, pile_id)["deliverable_version"] == 0, "and the record rolled back"

    monkeypatch.undo()
    rewind(engine, pile_id)
    assert run(engine, tmp_path) is not None

    rec = record(engine, pile_id)
    assert rec["deliverable_version"] == 1
    assert len(versions(engine, pile_id)) == 2, "each section recorded once"
    recorded = deliverable.recorded_sections(rec["deliverable_sections"])
    assert deliverable.verify(contract.parent / FILENAME, recorded).ok


# ── the graph's shape ─────────────────────────────────────────────────────


def test_resolve_runs_before_a_new_commit_is_opened() -> None:
    """A document lost or damaged since the last run is restored before a new version is
    written on top of it."""
    from documents_analyst.graph import build

    assert build.NODES[0] == build.RESOLVE
    assert build.NODES.index(build.RESOLVE) < build.NODES.index(build.COMMIT)


def test_the_run_audits_a_second_time_and_then_ends(engine: Engine, tmp_path: Path) -> None:
    """Approving a change can CREATE a violation, so Reconcile routes back to Audit once —
    and pass 2 records what it finds and stops, or a fix that creates a finding that
    creates a fix would loop for ever."""
    pile_id, _ = seed(engine, tmp_path, "twopass")
    outcome = run(engine, tmp_path)
    assert outcome is not None

    types = [e["type"] for e in events(engine, pile_id, outcome.run_id)]
    assert len([t for t in types if t == "stage.audit.done"]) == 2, types
    assert types[-1] == "run.finished"


# ── units ─────────────────────────────────────────────────────────────────


def test_the_marker_is_appended_to_the_value_never_substituted_for_it() -> None:
    value = slot_repo.SlotValue(
        value_group=0,
        value_text="USD 10,000",
        value_normalized=None,
        document_ids=("d1",),
        citation_count=1,
    )
    live = slot_repo.Slot(
        agreement_id="a", field_key="recurring_price", state="settled", values=(value,)
    )
    gone = slot_repo.Slot(
        agreement_id="a",
        field_key="recurring_price",
        state="settled",
        values=(value,),
        orphaned=True,
    )
    assert render.body(live) == "USD 10,000"
    assert render.body(gone) == f"USD 10,000 — {render.ORPHANED}"


def test_a_rename_is_an_edit_of_the_same_section_not_a_removal() -> None:
    old = [deliverable.Section("s1", "A", "Recurring Price", "USD 10,000")]
    new = [deliverable.Section("s1", "A", "Monthly Fee", "USD 10,000")]
    diff = commit_node._diff(old, new)
    assert (len(diff.added), len(diff.edited), len(diff.removed)) == (0, 1, 0)


def test_section_hashes_respect_field_boundaries() -> None:
    assert deliverable.section_sha256("a b", "c", "d") != deliverable.section_sha256(
        "a", "b c", "d"
    )
