"""User-level regressions: immutable sources, complete extraction, and honest commits."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import test_commit as fixture
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import text
from sqlalchemy.engine import Engine

from documents_analyst.db.repo import decisions, piles, telemetry
from documents_analyst.worker import lease, queue, runner
from documents_analyst.worker.lease import Fence
from documents_analyst_core.portable.llm_client import LLMError

engine = fixture.engine
clean = fixture.clean
pytestmark = pytest.mark.db


def invoke(engine: Engine, root: Path, llm: Any, saver: Any = None, **overrides: Any) -> Any:
    settings = fixture.settings_for(fixture.dsn(engine), root).model_copy(update=overrides)
    return runner.run_pile(
        engine, saver or InMemorySaver(), settings, fixture.models(), llm, worker_id="regression"
    )


def active_terms(engine: Engine, pile_id: str) -> list[str]:
    with engine.connect() as conn:
        return list(
            conn.execute(
                text(
                    "SELECT f.value_norm FROM facts_and_claims f JOIN documents d ON d.id=f.source_document_id "
                    "WHERE d.pile_id=:p AND f.field_key='payment_terms_days' AND f.status='active' "
                    "AND d.status NOT IN ('removed','superseded')"
                ),
                {"p": pile_id},
            ).scalars()
        )


@pytest.mark.parametrize("copy_survives", [False, True])
def test_reverting_source_restores_its_actual_terms(
    engine: Engine, tmp_path: Path, copy_survives: bool
) -> None:
    pile_id, source = fixture.seed(engine, tmp_path, "reversion")
    llm = fixture.ScriptedLLM(
        facts=fixture.facts_for(fixture.MSA) + fixture.facts_for(fixture.REVISED)
    )
    if copy_survives:
        fixture._write(source.parent / "copy.md", fixture.MSA)
        fixture.rescan(engine, pile_id, source.parent)
    assert invoke(engine, tmp_path, llm)
    fixture._write(source, fixture.REVISED)
    fixture.rescan(engine, pile_id, source.parent)
    changed = invoke(engine, tmp_path, llm)
    if changed.paused:
        with engine.begin() as conn:
            conn.execute(
                text("UPDATE pending_decisions SET status='rejected' WHERE pile_id=:p"),
                {"p": pile_id},
            )
            conn.execute(text("UPDATE piles SET status='idle' WHERE id=:p"), {"p": pile_id})
    fixture._write(source, fixture.MSA)
    fixture.rescan(engine, pile_id, source.parent)
    assert invoke(engine, tmp_path, llm)
    assert active_terms(engine, pile_id) == ["30"]


def test_restoring_an_original_after_alias_promotion_does_not_duplicate_terms(
    engine: Engine, tmp_path: Path
) -> None:
    pile_id, source = fixture.seed(engine, tmp_path, "alias-return")
    # Markdown and text parse to the same canonical text, with different byte hashes.
    from docx import Document

    document = Document()
    for line in fixture.MSA.splitlines():
        document.add_paragraph(line)
    document.save(source.parent / "export.docx")
    import os
    import time

    os.utime(source.parent / "export.docx", (time.time() - 60, time.time() - 60))
    fixture.rescan(engine, pile_id, source.parent)
    llm = fixture.ScriptedLLM(facts=fixture.facts_for(fixture.MSA))
    assert invoke(engine, tmp_path, llm)
    source.unlink()
    fixture.rescan(engine, pile_id, source.parent)
    invoke(engine, tmp_path, llm)
    fixture._write(source, fixture.MSA)
    fixture.rescan(engine, pile_id, source.parent)
    invoke(engine, tmp_path, llm)
    assert active_terms(engine, pile_id) == ["30"]

    (source.parent / "export.docx").unlink()
    fixture.rescan(engine, pile_id, source.parent)
    assert invoke(engine, tmp_path, llm)
    assert active_terms(engine, pile_id) == ["30"]


@pytest.mark.parametrize("failed_pass", ["facts", "subjects"])
def test_partial_extraction_retries_without_publishing_or_duplicating(
    engine: Engine, tmp_path: Path, failed_pass: str
) -> None:
    pile_id, source = fixture.seed(engine, tmp_path, "partial")

    class Flaky(fixture.ScriptedLLM):
        fail = True

        def structured(self, **kwargs: Any) -> Any:
            if (
                self.fail
                and failed_pass in kwargs["schema"].get("properties", {})
                and fixture.TERMS in kwargs["user"]
            ):
                raise LLMError("temporary provider failure")
            return super().structured(**kwargs)

    llm = Flaky(facts=fixture.facts_for(fixture.MSA))
    first = invoke(engine, tmp_path, llm)
    assert not first.progressed
    assert not (source.parent / "final-document.docx").exists()
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM facts_and_claims")).scalar_one() == 0
        assert (
            conn.execute(
                text("SELECT extraction_ok FROM documents WHERE pile_id=:p"), {"p": pile_id}
            ).scalar_one()
            is False
        )
    llm.fail = False
    assert invoke(engine, tmp_path, llm).progressed
    assert active_terms(engine, pile_id) == ["30"]


def test_a_section_that_always_fails_is_quarantined_after_the_attempt_limit(
    engine: Engine, tmp_path: Path
) -> None:
    """It used to stay `ingested` for ever: re-claimed on every scan, every section paid
    for again, the pile never reaching review. Now each failure is an attempt, the last
    one quarantines the file with its reason, and Retry puts it back in the queue."""
    pile_id, source = fixture.seed(engine, tmp_path, "poison")

    class Broken(fixture.ScriptedLLM):
        fail = True

        def structured(self, **kwargs: Any) -> Any:
            if self.fail and "facts" in kwargs["schema"].get("properties", {}):
                raise LLMError("the answer was cut off mid-JSON")
            return super().structured(**kwargs)

    llm = Broken(facts=fixture.facts_for(fixture.MSA))
    limit = fixture.settings_for(fixture.dsn(engine), tmp_path).job_max_attempts
    for _ in range(limit):
        invoke(engine, tmp_path, llm)
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT status, attempts, quarantine_error FROM documents WHERE pile_id=:p"),
            {"p": pile_id},
        ).one()
        assert row.status == "quarantined" and row.attempts == limit
        assert row.quarantine_error.startswith("extraction failed after")
        assert "cut off mid-JSON" in row.quarantine_error
        summary = next(p for p in piles.summaries(conn) if str(p["id"]) == pile_id)
        assert summary["retryable"] == 1
    # Nothing left to claim: the poison file no longer brings the pile back each scan.
    with engine.begin() as conn:
        assert (
            conn.execute(text(f"SELECT count(*) FROM ({queue.CLAIMABLE_SQL}) c")).scalar_one() == 0
        )

    with engine.begin() as conn:
        assert piles.retry(conn, pile_id)
        row = conn.execute(
            text("SELECT status, attempts FROM documents WHERE pile_id=:p"), {"p": pile_id}
        ).one()
        assert (row.status, row.attempts) == ("ingested", 0)
    llm.fail = False
    assert invoke(engine, tmp_path, llm).progressed
    assert active_terms(engine, pile_id) == ["30"]


def test_document_batch_limit_waits_for_the_whole_pile_before_publication(
    engine: Engine, tmp_path: Path
) -> None:
    pile_id, source = fixture.seed(engine, tmp_path, "batches")
    fixture._write(source.parent / "notes.md", fixture.NOTES)
    fixture.rescan(engine, pile_id, source.parent)
    llm = fixture.ScriptedLLM(facts=fixture.facts_for(fixture.MSA))
    first = invoke(engine, tmp_path, llm, max_documents_per_run=1)
    assert first.documents == 1 and first.progressed
    assert not (source.parent / "final-document.docx").exists()
    assert invoke(engine, tmp_path, llm, max_documents_per_run=1).documents == 1
    assert (source.parent / "final-document.docx").is_file()
    assert active_terms(engine, pile_id) == ["30"]


def test_stale_approved_evidence_returns_to_review_and_can_then_finish(
    engine: Engine, tmp_path: Path
) -> None:
    pile_id, source = fixture.seed(engine, tmp_path, "stale-choice")
    saver = InMemorySaver()
    llm = fixture.ScriptedLLM(
        facts=fixture.facts_for(fixture.MSA) + fixture.facts_for(fixture.REVISED)
    )
    assert invoke(engine, tmp_path, llm, saver)
    fixture._write(source.parent / "second.md", fixture.REVISED)
    fixture.rescan(engine, pile_id, source.parent)
    assert invoke(engine, tmp_path, llm, saver).paused
    with engine.begin() as conn:
        item = decisions.open_items(conn, pile_id)[0]
        chosen = next(
            o["value_group"] for o in item["payload"]["options"] if o["value_norm"] == "30"
        )
        decisions.settle(
            conn, str(item["id"]), status="approved", actor="Reviewer", chosen_value_group=chosen
        )
    fixture._write(source, fixture.REVISED)
    fixture.rescan(engine, pile_id, source.parent)
    outcome = runner.resume_after_approval(
        engine,
        saver,
        fixture.settings_for(fixture.dsn(engine), tmp_path),
        fixture.models(),
        llm,
        pile_id=pile_id,
        worker_id="regression",
    )
    assert outcome and outcome.paused
    with engine.begin() as conn:
        assert (
            conn.execute(
                text(
                    "SELECT count(*) FROM deliverable_commits WHERE CAST(:id AS uuid)=ANY(decision_ids)"
                ),
                {"id": item["id"]},
            ).scalar_one()
            == 0
        )
        assert decisions.get(conn, str(item["id"]))["status"] == "superseded"
        for fresh in decisions.open_items(conn, pile_id):
            assert fresh["payload"]["options"]
            decisions.settle(
                conn,
                str(fresh["id"]),
                status="approved",
                actor="Reviewer",
                chosen_value_group=fresh["payload"]["options"][0]["value_group"],
            )
    finished = runner.resume_after_approval(
        engine,
        saver,
        fixture.settings_for(fixture.dsn(engine), tmp_path),
        fixture.models(),
        llm,
        pile_id=pile_id,
        worker_id="regression",
    )
    assert finished and not finished.paused


def test_a_pending_item_with_changed_evidence_is_sent_back_and_asked_again(
    engine: Engine, tmp_path: Path
) -> None:
    """Approving an item whose sources changed used to 409 for ever: nothing refreshed
    it and classify would not raise a second item while it was pending."""
    pile_id, source = fixture.seed(engine, tmp_path, "send-back")
    saver = InMemorySaver()
    llm = fixture.ScriptedLLM(
        facts=fixture.facts_for(fixture.MSA) + fixture.facts_for(fixture.REVISED)
    )
    assert invoke(engine, tmp_path, llm, saver)
    fixture._write(source.parent / "second.md", fixture.REVISED)
    fixture.rescan(engine, pile_id, source.parent)
    assert invoke(engine, tmp_path, llm, saver).paused
    with engine.begin() as conn:
        item = decisions.open_items(conn, pile_id)[0]
    fixture._write(source, fixture.REVISED)  # the evidence changes under the open item
    fixture.rescan(engine, pile_id, source.parent)
    with engine.begin() as conn:
        assert not decisions.evidence_current(
            conn, item, item["payload"]["options"][0]["value_group"]
        )
        decisions.send_back_for_recheck(conn, item)
    with engine.begin() as conn:
        assert decisions.get(conn, str(item["id"]))["status"] == "superseded"
        assert piles.get(conn, pile_id)["status"] == "idle"
    rerun = invoke(engine, tmp_path, llm, saver)
    assert rerun
    with engine.begin() as conn:
        for fresh in decisions.open_items(conn, pile_id):
            assert str(fresh["id"]) != str(item["id"])
            assert decisions.evidence_current(
                conn, fresh, fresh["payload"]["options"][0]["value_group"]
            )


def closing_event(engine: Engine, pile_id: str) -> dict[str, Any]:
    with engine.connect() as conn:
        payload = conn.execute(
            text(
                "SELECT payload FROM stage_events WHERE pile_id=:p "
                "AND type IN ('run.finished','run.paused','run.incomplete') ORDER BY id DESC LIMIT 1"
            ),
            {"p": pile_id},
        ).scalar_one()
    return dict(payload)


def test_runs_never_stop_for_cost_and_report_what_they_spent(
    engine: Engine, tmp_path: Path
) -> None:
    """There is no spending limit. A wildly expensive price still runs to the end of the
    graph, and the run's closing event says what it cost."""
    pile_id, _ = fixture.seed(engine, tmp_path, "no-limit")
    llm = fixture.ScriptedLLM(facts=fixture.facts_for(fixture.MSA))
    rates = tmp_path / "prices.json"
    rates.write_text(json.dumps({llm.model: {"input": "100000", "output": "100000"}}))
    outcome = invoke(engine, tmp_path, llm, model_prices_path=rates)
    assert outcome and outcome.progressed
    with engine.connect() as conn:
        assert piles.get(conn, pile_id)["status"] in ("idle", "awaiting_review")
    closing = closing_event(engine, pile_id)
    assert Decimal(closing["cost_usd"]) > 0 and closing["calls"] > 0
    assert llm.model not in closing.get("unpriced_models", [])


def test_a_priced_model_too_cheap_to_register_is_not_called_unpriced(
    engine: Engine, tmp_path: Path
) -> None:
    """An embedding call can cost less than the cost column's smallest step and be
    stored as 0. Whether a model is priced comes from the price table, not that zero."""
    pile_id, _ = fixture.seed(engine, tmp_path, "tiny-price")
    llm = fixture.ScriptedLLM(facts=fixture.facts_for(fixture.MSA))
    rates = tmp_path / "prices.json"
    rates.write_text(json.dumps({llm.model: {"input": "0.000001", "output": "0.000001"}}))
    assert invoke(engine, tmp_path, llm, model_prices_path=rates)
    assert llm.model not in closing_event(engine, pile_id).get("unpriced_models", [])
    with engine.connect() as conn:
        unpriced = telemetry.totals_for_pile(conn, pile_id, priced={llm.model})["unpriced"]
    assert llm.model not in [row["model"] for row in unpriced]


def test_an_unpriced_model_still_runs_and_is_reported_as_unpriced(
    engine: Engine, tmp_path: Path
) -> None:
    """A model missing from the price table is a reporting gap, not a reason to stop."""
    pile_id, _ = fixture.seed(engine, tmp_path, "unpriced")
    llm = fixture.ScriptedLLM(facts=fixture.facts_for(fixture.MSA))
    rates = tmp_path / "prices.json"
    rates.write_text("{}")
    outcome = invoke(engine, tmp_path, llm, model_prices_path=rates)
    assert outcome and outcome.progressed
    closing = closing_event(engine, pile_id)
    assert closing["unpriced_calls"] > 0 and llm.model in closing["unpriced_models"]
    with engine.connect() as conn:
        unpriced = telemetry.totals_for_pile(conn, pile_id, priced=())["unpriced"]
    assert llm.model in [row["model"] for row in unpriced]


def test_dropped_unsupported_files_are_visible_in_the_final_coverage(
    engine: Engine, tmp_path: Path
) -> None:
    pile_id, source = fixture.seed(engine, tmp_path, "unsupported")
    llm = fixture.ScriptedLLM(facts=fixture.facts_for(fixture.MSA))
    assert invoke(engine, tmp_path, llm)
    fixture._write(source.parent / "legacy.rtf", "not a supported format")
    fixture.rescan(engine, pile_id, source.parent)
    assert invoke(engine, tmp_path, fixture.ScriptedLLM(facts=fixture.facts_for(fixture.MSA)))
    from documents_analyst import deliverable

    coverage = next(
        section.body
        for section in deliverable.read(source.parent / "final-document.docx")
        if section.agreement == "Analysis coverage"
    )
    assert "legacy.rtf [quarantined" in coverage
    assert "NOT ANALYSED" in coverage


def test_discovery_and_repair_continue_while_graph_work_is_blocked(
    engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading
    import time

    from documents_analyst.worker import main

    pile_id, source = fixture.seed(engine, tmp_path, "idle-repair")
    llm = fixture.ScriptedLLM(facts=fixture.facts_for(fixture.MSA))
    assert invoke(engine, tmp_path, llm)
    entered, release, stopped, wake = (threading.Event() for _ in range(4))

    def busy(*args: Any, **kwargs: Any) -> int:
        entered.set()
        release.wait(5)
        return 0

    monkeypatch.setattr(main, "drain_runs", busy)
    monkeypatch.setattr(main, "drain_resumes", lambda *args, **kwargs: 0)
    settings = fixture.settings_for(fixture.dsn(engine), tmp_path).model_copy(
        update={"scan_interval_s": 0.1, "settle_seconds": 0}
    )
    env = main.GraphEnv(InMemorySaver(), fixture.models(), llm)
    thread = threading.Thread(
        target=main.watch_loop,
        args=(engine, settings),
        kwargs={"graph_env": env, "stopped": stopped, "wake": wake},
    )
    thread.start()
    try:
        assert entered.wait(3)
        final = source.parent / "final-document.docx"
        final.unlink()
        folder = tmp_path / "arriving"
        folder.mkdir()
        (folder / "note.md").write_text(fixture.NOTES)
        end = time.monotonic() + 3
        found = False
        while time.monotonic() < end:
            with engine.connect() as conn:
                found = (
                    conn.execute(
                        text(
                            "SELECT count(*) FROM documents d JOIN piles p ON p.id=d.pile_id WHERE p.name='arriving'"
                        )
                    ).scalar_one()
                    == 1
                )
            if found and final.exists():
                break
            time.sleep(0.03)
        assert found and final.exists()
        assert not release.is_set(), "Discovery finished before the blocked graph was released"
    finally:
        stopped.set()
        wake.set()
        release.set()
        thread.join(5)
    assert not thread.is_alive()


def test_a_wake_during_a_drain_starts_the_next_drain_as_soon_as_it_ends(
    engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An approval's NOTIFY that lands mid-drain used to wait out the rest of the scan
    interval after the drain ended. It must not make the loop spin either."""
    import threading
    import time

    from documents_analyst.worker import main

    calls: list[float] = []
    first, release, stopped, wake = (threading.Event() for _ in range(4))

    def drain(*args: Any, **kwargs: Any) -> int:
        calls.append(time.monotonic())
        if len(calls) == 1:
            first.set()
            release.wait(5)
        return 0

    monkeypatch.setattr(main, "drain_runs", drain)
    monkeypatch.setattr(main, "drain_resumes", lambda *args, **kwargs: 0)
    settings = fixture.settings_for(fixture.dsn(engine), tmp_path).model_copy(
        update={"scan_interval_s": 60, "settle_seconds": 0}
    )
    env = main.GraphEnv(InMemorySaver(), fixture.models(), fixture.ScriptedLLM(facts=[]))
    thread = threading.Thread(
        target=main.watch_loop,
        args=(engine, settings),
        kwargs={"graph_env": env, "stopped": stopped, "wake": wake},
    )
    thread.start()
    try:
        assert first.wait(3)
        wake.set()  # an approval arrives while the first drain is busy
        time.sleep(0.3)
        release.set()
        end = time.monotonic() + 3
        while len(calls) < 2 and time.monotonic() < end:
            time.sleep(0.02)
        assert len(calls) == 2, "the missed wake was not honoured when the drain ended"
        time.sleep(0.5)
        assert len(calls) == 2, "the loop re-drained without being woken"
    finally:
        stopped.set()
        wake.set()
        release.set()
        thread.join(5)
    assert not thread.is_alive()


def test_a_byte_identical_copy_updates_inventory_without_extra_facts(
    engine: Engine, tmp_path: Path
) -> None:
    pile_id, source = fixture.seed(engine, tmp_path, "copy-inventory")
    llm = fixture.ScriptedLLM(facts=fixture.facts_for(fixture.MSA))
    assert invoke(engine, tmp_path, llm)
    fixture._write(source.parent / "copy.md", fixture.MSA)
    fixture.rescan(engine, pile_id, source.parent)
    assert invoke(engine, tmp_path, llm)
    from documents_analyst import deliverable

    coverage = next(
        section.body
        for section in deliverable.read(source.parent / "final-document.docx")
        if section.agreement == "Analysis coverage"
    )
    assert "copy.md" in coverage
    assert active_terms(engine, pile_id) == ["30"]


def test_a_surviving_format_alias_still_contributes_after_original_overwrite(
    engine: Engine, tmp_path: Path
) -> None:
    from docx import Document

    pile_id, source = fixture.seed(engine, tmp_path, "alias-survives")
    document = Document()
    for line in fixture.MSA.splitlines():
        document.add_paragraph(line)
    document.save(source.parent / "export.docx")
    import os
    import time

    os.utime(source.parent / "export.docx", (time.time() - 60, time.time() - 60))
    fixture.rescan(engine, pile_id, source.parent)
    llm = fixture.ScriptedLLM(
        facts=fixture.facts_for(fixture.MSA) + fixture.facts_for(fixture.REVISED)
    )
    assert invoke(engine, tmp_path, llm)
    fixture._write(source, fixture.REVISED)
    fixture.rescan(engine, pile_id, source.parent)
    assert invoke(engine, tmp_path, llm)
    assert sorted(active_terms(engine, pile_id)) == ["30", "45"]


def test_judging_uses_the_separately_injected_model(engine: Engine, tmp_path: Path) -> None:
    fixture.seed(engine, tmp_path, "separate-judge")

    class Recording(fixture.ScriptedLLM):
        def __init__(self, label: str) -> None:
            super().__init__(facts=fixture.facts_for(fixture.MSA))
            self.model = label
            self.calls: list[set[str]] = []

        def structured(self, **kwargs: Any) -> Any:
            self.calls.append(set(kwargs["schema"].get("properties", {})))
            return super().structured(**kwargs)

    extract, judge = Recording("extract-model"), Recording("judge-model")
    assert runner.run_pile(
        engine,
        InMemorySaver(),
        fixture.settings_for(fixture.dsn(engine), tmp_path),
        fixture.models(),
        extract,
        judge=judge,
        worker_id="regression",
    )
    assert extract.calls and all("party_indices" not in call for call in extract.calls)
    assert any("party_indices" in call for call in judge.calls)


def test_deleting_earlier_conflicting_source_uses_surviving_value_in_docx(
    engine: Engine, tmp_path: Path
) -> None:
    from documents_analyst import deliverable

    pile_id, source = fixture.seed(engine, tmp_path, "surviving-source")
    fixture._write(source.parent / "newer.md", fixture.REVISED)
    fixture.rescan(engine, pile_id, source.parent)
    llm = fixture.ScriptedLLM(
        facts=fixture.facts_for(fixture.MSA) + fixture.facts_for(fixture.REVISED)
    )
    assert invoke(engine, tmp_path, llm).paused
    source.unlink()
    fixture.rescan(engine, pile_id, source.parent)
    with engine.begin() as conn:
        epoch = lease.acquire(conn, pile_id, "test", 90)
        assert epoch is not None
        decisions.invalidate_review(conn, Fence(pile_id, epoch))
        lease.release(conn, pile_id, epoch)
    assert invoke(engine, tmp_path, llm)
    term = next(
        section
        for section in deliverable.read(source.parent / "final-document.docx")
        if section.heading == "Payment terms"
    )
    assert term.body.startswith("45 days")
    assert fixture.NEW_TERMS in term.body


def test_a_source_change_withdraws_only_the_items_whose_evidence_changed(
    engine: Engine, tmp_path: Path
) -> None:
    """Any added or copied file used to withdraw every open and approved item in the
    pile. An item whose cited facts are unchanged now survives, approval and all."""
    pile_id, source = fixture.seed(engine, tmp_path, "scoped-invalidation")
    fixture._write(source.parent / "newer.md", fixture.REVISED)
    fixture.rescan(engine, pile_id, source.parent)
    llm = fixture.ScriptedLLM(
        facts=fixture.facts_for(fixture.MSA) + fixture.facts_for(fixture.REVISED)
    )
    assert invoke(engine, tmp_path, llm).paused
    with engine.begin() as conn:
        item = decisions.open_items(conn, pile_id)[0]
        decisions.settle(
            conn,
            str(item["id"]),
            status="approved",
            actor="Reviewer",
            chosen_value_group=item["payload"]["options"][0]["value_group"],
        )
    with engine.begin() as conn:
        epoch = lease.acquire(conn, pile_id, "test", 90)
        assert epoch is not None
        assert decisions.invalidate_review(conn, Fence(pile_id, epoch)) == 0
        lease.release(conn, pile_id, epoch)
    with engine.begin() as conn:
        assert decisions.get(conn, str(item["id"]))["status"] == "approved"

    (source.parent / "newer.md").unlink()  # now its evidence really is gone
    fixture.rescan(engine, pile_id, source.parent)
    with engine.begin() as conn:
        epoch = lease.acquire(conn, pile_id, "test", 90)
        assert epoch is not None
        assert decisions.invalidate_review(conn, Fence(pile_id, epoch)) == 1
        lease.release(conn, pile_id, epoch)
    with engine.begin() as conn:
        assert decisions.get(conn, str(item["id"]))["status"] == "superseded"


def test_removing_one_copy_removes_it_from_inventory(engine: Engine, tmp_path: Path) -> None:
    from documents_analyst import deliverable

    pile_id, source = fixture.seed(engine, tmp_path, "copy-removal")
    copy = source.parent / "copy.md"
    fixture._write(copy, fixture.MSA)
    fixture.rescan(engine, pile_id, source.parent)
    llm = fixture.ScriptedLLM(facts=fixture.facts_for(fixture.MSA))
    assert invoke(engine, tmp_path, llm)
    copy.unlink()
    fixture.rescan(engine, pile_id, source.parent)
    assert invoke(engine, tmp_path, llm)
    coverage = next(
        section.body
        for section in deliverable.read(source.parent / "final-document.docx")
        if section.agreement == "Analysis coverage"
    )
    assert "copy.md" not in coverage
    assert "01_msa.md" in coverage
