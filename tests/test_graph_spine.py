"""The graph spine — Phase 4A. Checkpoints, resume, and the human-gate boundary.

Marked `db`, because every property here *is* a database property: a checkpoint
that survives the process, a claim that two workers cannot both win, an epoch
that fences a write. Faking any of them would test the fake.

The LLM is scripted rather than live. `ScriptedLLM` answers by looking at the
schema it was handed, so the party call and the extraction call can be answered
differently without depending on the order the graph happens to make them in —
an ordered queue of canned responses breaks the first time a node is reordered,
and breaks by returning the wrong answer rather than by failing.
"""

from __future__ import annotations

import os
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from documents_analyst.db import migrate
from documents_analyst.db.repo import chunks as chunk_repo
from documents_analyst.db.repo import documents as doc_repo
from documents_analyst.db.repo import events as event_repo
from documents_analyst.db.repo import piles as pile_repo
from documents_analyst.db.repo import telemetry as telemetry_repo
from documents_analyst.graph.build import build_graph
from documents_analyst.graph.checkpointer import CHECKPOINT_SCHEMA, checkpointer, psycopg_url
from documents_analyst.models import LoadedModels
from documents_analyst.settings import Settings
from documents_analyst.worker import lease, queue, runner
from documents_analyst.worker.ingest import ingest_pile
from documents_analyst.worker.lease import Fence, LostLease
from documents_analyst_core.parsing.sections import split_sections
from documents_analyst_core.portable.llm_client import LLMQuotaExhausted, Usage
from documents_analyst_core.portable.ner_gliner import Span

pytestmark = pytest.mark.db

SCRATCH = "analyst_graph_test"
FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"

QUOTE = "Payment is due Net 30 days from the date of invoice."

CONTRACT = """MASTER SERVICES AGREEMENT

This Agreement is entered into between Acme Corporation and Beta Logistics Inc.

1. Payment Terms

Payment is due Net 30 days from the date of invoice.

2. Term

The initial term is twelve (12) months from the Effective Date.
"""


# ── doubles ───────────────────────────────────────────────────────────────


class ScriptedLLM:
    """Answers by shape, not by turn.

    `party_indices` in the schema means the party pick; `facts` means
    extraction. Anything else is a call this test did not expect, and it says
    so rather than returning a plausible empty answer — a fake that shrugs
    turns "the node never ran" into "the node found nothing".
    """

    def __init__(
        self,
        *,
        facts: list[dict[str, Any]] | None = None,
        subjects: list[dict[str, Any]] | None = None,
    ) -> None:
        self.model = "scripted-1"
        self.facts = facts if facts is not None else []
        self.subjects = subjects if subjects is not None else []
        self.calls: list[str] = []

    def structured(
        self, *, system: str, user: str, schema: dict[str, Any], max_tokens: int
    ) -> tuple[dict[str, Any], Usage]:
        properties = (schema.get("properties") or {}).keys()
        usage = Usage(model=self.model, input_tokens=120, output_tokens=40)
        if "party_indices" in properties:
            self.calls.append("parties")
            return {"party_indices": [0, 1]}, usage
        if "subjects" in properties:
            # The open-subject pass added in 4B. Empty is the normal answer —
            # most sections state nothing the taxonomy misses.
            self.calls.append("open")
            return {"subjects": self.subjects if self.calls.count("open") == 1 else []}, usage
        if "facts" in properties:
            self.calls.append("extract")
            # Only the first section carries the facts; later sections return
            # nothing, which is what a real contract does too.
            payload = {"facts": self.facts if self.calls.count("extract") == 1 else []}
            return payload, usage
        raise AssertionError(f"unexpected structured call with schema keys {sorted(properties)}")

    def complete(self, *, system: str, user: str, max_tokens: int) -> tuple[str, Usage]:
        raise AssertionError("the spine makes no unstructured calls")


class FixedEmbeddings:
    """384 floats, deterministic, no model.

    The width is not arbitrary — `facts_and_claims.fact_key_embedding` is
    `vector(384)` and a mismatch is a database error, so this doubles as a check
    that the node hands over vectors of the declared width.

    `seen` is here for one assertion: that ingest stopped embedding chunks.
    """

    def __init__(self) -> None:
        self.seen: list[list[str]] = []
        self.last_usage = None

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        self.seen.append(list(texts))
        return [[float((len(t) + i) % 7) / 7.0] * 384 for i, t in enumerate(texts)]


class ScriptedNER:
    """The two companies in the preamble, at their real offsets."""

    enabled = True
    is_available = True

    def extract(self, text: str, labels: list[str], *, threshold: float = 0.5) -> list[Span]:
        found = []
        for name in ("Acme Corporation", "Beta Logistics Inc"):
            start = text.find(name)
            if start >= 0:
                found.append(
                    Span(
                        text=name,
                        label="organization",
                        start=start,
                        end=start + len(name),
                        score=0.9,
                    )
                )
        return found


def models(embeddings: FixedEmbeddings | None = None) -> LoadedModels:
    return LoadedModels(  # type: ignore[arg-type]
        embeddings=embeddings or FixedEmbeddings(), nli=object(), ner=ScriptedNER()
    )


def settings_for(url: str, watch_root: Path) -> Settings:
    return Settings(
        database_url=url,
        watch_root=watch_root,
        lease_ttl_s=90,
        settle_seconds=1,
        job_max_attempts=3,
        worker_id="test-worker",
    )


def fact_in(body: str, quote: str, field_key: str = "payment_terms_days") -> dict[str, Any]:
    """A candidate whose offsets are correct *relative to its section*.

    `extract_facts` renders `text[section.start:section.end]` for the model and
    passes `offset=section.start` to the gate, so offsets in a canned response
    are section-relative exactly as a real model's would be. Computed rather
    than hard-coded, so this stays honest when the fixture text changes.

    `field_key` must be one of the 28 taxonomy keys — `payment_terms_days`, not
    `payment_terms`. That constraint is the enum in `taxonomy/schema.py`, and a
    test that quietly used a key outside it would be testing a path production
    refuses.
    """
    section_start = body.index("1. Payment Terms")
    local = body.index(quote) - section_start
    return {
        "field_key": field_key,
        "value_text": "Net 30 days",
        "source_quote": quote,
        "start": local,
        "end": local + len(quote),
        "confidence": 0.9,
    }


# ── fixtures ──────────────────────────────────────────────────────────────


def dsn(engine: Engine) -> str:
    """The connection string WITH its password.

    `str(engine.url)` renders it as `***`. Handing that to psycopg produces an
    authentication failure that reads like a broken database rather than a
    masked string, which is a genuinely confusing half hour.
    """
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
    """One pile per test, because claiming is global.

    `claim_and_lease` asks "any pile with work", not "this pile". Left to share
    a database, a test that seeds one pile can be handed the pile the previous
    test left behind — and it passes, against the wrong data, until the day the
    order changes.

    `piles` cascades to documents, agreements, facts, chunks, decisions, events
    and stage_runs, so one TRUNCATE is the entire reset.
    """
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE piles CASCADE"))
        conn.execute(text("TRUNCATE stage_events, stage_runs"))
    yield


def seed(engine: Engine, tmp_path: Path, name: str, *, files: dict[str, str] | None = None) -> str:
    """A pile on disk, discovered and ingested exactly as the worker would.

    Goes through the real `ingest_pile` rather than inserting rows: the graph
    reads `status = 'ingested'` originals, and a hand-inserted document is a
    document that never proved it can get into that state.
    """
    pile_dir = tmp_path / name
    pile_dir.mkdir(parents=True, exist_ok=True)
    for filename, body in (files or {"01_msa.md": CONTRACT}).items():
        target = pile_dir / filename
        target.write_text(body)
        when = time.time() - 60
        os.utime(target, (when, when))

    with engine.begin() as conn:
        pile_id = pile_repo.ensure(conn, name=name, watch_path=pile_dir)
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
    return pile_id


def run(
    engine: Engine,
    tmp_path: Path,
    llm: Any,
    *,
    saver: Any = None,
    embeddings: FixedEmbeddings | None = None,
) -> Any:
    return runner.run_pile(
        engine,
        saver or InMemorySaver(),
        settings_for(dsn(engine), tmp_path),
        models(embeddings),
        llm,
        worker_id="test-worker",
    )


# ── the checkpointer's schema boundary ────────────────────────────────────


def test_checkpoint_tables_land_outside_our_schema(engine: Engine) -> None:
    """Our fourteen tables stay fourteen.

    LangGraph's `setup()` issues `CREATE TABLE IF NOT EXISTS` with no schema
    argument, so left alone it puts four more tables in `public` and the
    schema-drift test starts carrying an allow-list. This asserts the boundary
    rather than trusting the connection string to keep being right.
    """
    with checkpointer(dsn(engine)):
        pass

    with engine.connect() as conn:
        public = {
            r[0]
            for r in conn.execute(
                text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
            )
        }
        theirs = {
            r[0]
            for r in conn.execute(
                text("SELECT tablename FROM pg_tables WHERE schemaname = :s"),
                {"s": CHECKPOINT_SCHEMA},
            )
        }

    assert len(public) == 14, f"public gained tables: {sorted(public)}"
    assert "checkpoints" in theirs and "checkpoint_writes" in theirs
    assert not (public & theirs)


def test_the_connection_string_keeps_public_reachable() -> None:
    """`search_path=langgraph` alone would break LangGraph's own DDL.

    Its statements reference things installed by `001_extensions` in `public`,
    so dropping `public` off the path makes `setup()` fail on a fresh database
    — the one case nobody runs twice.
    """
    made = psycopg_url("postgresql+psycopg://u:p@h:5432/db")

    assert "postgresql+psycopg" not in made, "psycopg does not understand the dialect prefix"
    assert "search_path%3Dlanggraph%2Cpublic" in made


# ── a run, end to end ─────────────────────────────────────────────────────


def test_chunks_are_written_without_being_embedded(engine: Engine, tmp_path: Path) -> None:
    """Every chunk of every document used to go through the embedder here, and
    nothing ever read the vectors — extraction works per section and the hybrid
    retrieval query was never written. Free-ish when embeddings were a local
    model; a paid round trip per document now they are not.

    Asserted on what the node *sent*, not on the absent column, because the
    column can be dropped while the call stays and the cost with it.
    """
    seed(engine, tmp_path, "no-chunk-vectors")
    llm = ScriptedLLM(
        facts=[fact_in(CONTRACT, "Payment is due Net 30 days from the date of invoice.")]
    )
    embeddings = FixedEmbeddings()

    assert run(engine, tmp_path, llm, embeddings=embeddings) is not None

    for batch in embeddings.seen:
        for sent in batch:
            assert CONTRACT[:80] not in sent, "a chunk of the contract reached the embedder"
    assert all(len(sent) < 400 for batch in embeddings.seen for sent in batch), (
        "only fact keys and open subjects are embedded, and both are short"
    )


def test_a_run_chunks_groups_and_grounds(engine: Engine, tmp_path: Path) -> None:
    """The spine's whole job, asserted where it lands rather than in the log."""
    pile_id = seed(engine, tmp_path, "spine")
    llm = ScriptedLLM(
        facts=[fact_in(CONTRACT, "Payment is due Net 30 days from the date of invoice.")]
    )

    outcome = run(engine, tmp_path, llm)

    assert outcome is not None and outcome.documents == 1 and not outcome.paused
    with engine.connect() as conn:
        chunks = conn.execute(
            text("SELECT count(*) FROM document_chunks WHERE pile_id = :p"), {"p": pile_id}
        ).scalar_one()
        parties = conn.execute(
            text("SELECT parties_normalized FROM agreements WHERE pile_id = :p"), {"p": pile_id}
        ).scalar_one()
        facts = conn.execute(
            text(
                "SELECT field_key, source_quote FROM facts_and_claims f "
                "JOIN documents d ON d.id = f.source_document_id WHERE d.pile_id = :p"
            ),
            {"p": pile_id},
        ).all()

    assert chunks > 0
    assert parties == ["acme", "beta logistics"], parties
    assert [(f.field_key, f.source_quote) for f in facts] == [
        ("payment_terms_days", "Payment is due Net 30 days from the date of invoice.")
    ]


def test_a_fact_whose_quote_is_not_in_the_document_never_lands(
    engine: Engine, tmp_path: Path
) -> None:
    """Gate 1 on the real path.

    The claim is well-formed, correctly typed and fits the schema — it is just
    not in the contract. Nothing in the pipeline paraphrases it into one that
    is, and nothing writes it anyway with a lower confidence.
    """
    seed(engine, tmp_path, "bluff")
    invented = {
        "field_key": "payment_terms_days",
        "value_text": "Net 90 days",
        "source_quote": "Payment is due Net 90 days.",
        "start": 0,
        "end": 27,
        "confidence": 0.99,
    }

    outcome = run(engine, tmp_path, ScriptedLLM(facts=[invented]))

    assert outcome is not None
    with engine.connect() as conn:
        total = conn.execute(
            text(
                "SELECT count(*) FROM facts_and_claims f JOIN documents d "
                "ON d.id = f.source_document_id JOIN piles p ON p.id = d.pile_id "
                "WHERE p.name = 'bluff'"
            )
        ).scalar_one()
    assert total == 0, "a quote that is not in the document must be dropped, not stored"


def test_re_running_a_pile_does_not_double_anything(engine: Engine, tmp_path: Path) -> None:
    """Idempotence is what makes a crashed node safe to redo.

    A resume re-enters whatever node was running, so a node that appends rather
    than replaces silently doubles its output every time a worker dies — which
    shows up much later as a retrieval score favouring whichever document
    happened to crash.
    """
    pile_id = seed(engine, tmp_path, "twice")
    quote = "Payment is due Net 30 days from the date of invoice."

    def counts() -> tuple[int, int]:
        with engine.connect() as conn:
            chunks = conn.execute(
                text("SELECT count(*) FROM document_chunks WHERE pile_id = :p"), {"p": pile_id}
            ).scalar_one()
            facts = conn.execute(
                text(
                    "SELECT count(*) FROM facts_and_claims f JOIN documents d "
                    "ON d.id = f.source_document_id WHERE d.pile_id = :p"
                ),
                {"p": pile_id},
            ).scalar_one()
        return int(chunks), int(facts)

    run(engine, tmp_path, ScriptedLLM(facts=[fact_in(CONTRACT, quote)]))
    first = counts()
    run(engine, tmp_path, ScriptedLLM(facts=[fact_in(CONTRACT, quote)]))

    assert first == counts() and first[1] == 1


def test_an_alias_is_not_extracted_twice(engine: Engine, tmp_path: Path) -> None:
    """The Phase 3 alias, followed all the way to the LLM bill.

    `.md` and `.docx` of one contract are one document. If the batch carried
    both, the model would be asked about the same text twice and the register
    would show every term twice — so the check is on the number of extraction
    calls, not just on rows.
    """
    pile_dir = tmp_path / "aliasrun"
    pile_dir.mkdir()
    for name in ("01_msa.md", "01_msa.docx"):
        target = pile_dir / name
        target.write_bytes((FIXTURES / "conflict_pile" / name).read_bytes())
        when = time.time() - 60
        os.utime(target, (when, when))

    with engine.begin() as conn:
        pile_id = pile_repo.ensure(conn, name="aliasrun", watch_path=pile_dir)
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

    llm = ScriptedLLM()
    outcome = run(engine, tmp_path, llm)

    assert outcome is not None and outcome.documents == 1, "the alias is not work"
    with engine.connect() as conn:
        chunked = conn.execute(
            text("SELECT count(DISTINCT document_id) FROM document_chunks WHERE pile_id = :p"),
            {"p": pile_id},
        ).scalar_one()
    assert chunked == 1


# ── the human gate ────────────────────────────────────────────────────────


def pending(engine: Engine, pile_id: str, kind: str = "conflict") -> str:
    """One item waiting on a person.

    Written directly because Classify and Audit produce these in 4B. The gate
    is what 4A is for, and it is testable the moment something can be waiting.
    """
    with engine.begin() as conn:
        return str(
            conn.execute(
                text(
                    "INSERT INTO pending_decisions (pile_id, kind, recommendation) "
                    "VALUES (:p, :k, 'pick one') RETURNING id"
                ),
                {"p": pile_id, "k": kind},
            ).scalar_one()
        )


def test_a_run_with_nothing_to_decide_finishes(engine: Engine, tmp_path: Path) -> None:
    """The gate is not a speed bump. With an empty queue it falls through, and
    the pile ends `idle` rather than sitting in `awaiting_review` waiting for a
    decision nobody was ever asked to make."""
    pile_id = seed(engine, tmp_path, "clear")

    outcome = run(engine, tmp_path, ScriptedLLM())

    assert outcome is not None and not outcome.paused
    with engine.connect() as conn:
        assert pile_repo.get(conn, pile_id)["status"] == "idle"


def test_a_pending_decision_pauses_the_run_and_releases_the_lease(
    engine: Engine, tmp_path: Path
) -> None:
    """The boundary Phase 4A exists to prove.

    Paused is not blocked: the run holds no thread and no lease, so new
    documents can still be ingested into this pile and another worker can pick
    it up. A gate that kept its lease would freeze the pile for as long as the
    reviewer took to make coffee.
    """
    pile_id = seed(engine, tmp_path, "gated")
    pending(engine, pile_id)

    outcome = run(engine, tmp_path, ScriptedLLM())

    assert outcome is not None and outcome.paused
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT status, lease_expires_at > now() AS held FROM piles WHERE id = :p"),
            {"p": pile_id},
        ).one()
    assert row.status == "awaiting_review"
    assert not row.held, "a paused run must not hold the pile"


def test_a_paused_run_resumes_into_the_gate_without_re_extracting(
    engine: Engine, tmp_path: Path
) -> None:
    """Resume means resume, not restart.

    The checkpoint is the whole mechanism: passing the state again would send
    the run back through Ingest and Extract, re-spending the extraction budget
    on documents a human has been reviewing for an hour — and overwriting the
    facts their decision refers to.

    Asserted on the LLM's call log, because "did it re-extract" is a question
    about what was *sent*, and row counts would look identical either way.
    """
    pile_id = seed(engine, tmp_path, "resumed")
    item = pending(engine, pile_id)
    saver = InMemorySaver()

    first = ScriptedLLM(
        facts=[fact_in(CONTRACT, "Payment is due Net 30 days from the date of invoice.")]
    )
    paused = run(engine, tmp_path, first, saver=saver)
    assert paused is not None and paused.paused
    assert first.calls.count("extract") > 0

    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE pending_decisions SET status = 'approved', decided_by = 'reviewer', "
                "decided_at = now() WHERE id = :id"
            ),
            {"id": item},
        )

    second = ScriptedLLM()
    resumed = runner.resume_after_approval(
        engine,
        saver,
        settings_for(dsn(engine), tmp_path),
        models(),
        second,
        pile_id=pile_id,
        worker_id="worker-B",
    )

    assert resumed is not None and not resumed.paused
    assert second.calls == [], "a resume re-entered the gate; it did not re-run extraction"
    with engine.connect() as conn:
        assert pile_repo.get(conn, pile_id)["status"] == "idle"


def test_a_paused_pile_is_not_claimed_until_an_explicit_resume(
    engine: Engine, tmp_path: Path
) -> None:
    """A pause is a human wait, not work for the next timer tick.

    The checkpoint contains an `ingested` document, so this must assert the
    actual queue predicate rather than merely inspect the pile status. If this
    pile were re-claimed normally, a new graph would start at Ingest and spend
    the extraction budget a second time before anyone had decided anything.
    """
    pile_id = seed(engine, tmp_path, "not-claimable-while-paused")
    pending(engine, pile_id)

    paused = run(engine, tmp_path, ScriptedLLM(), saver=InMemorySaver())
    assert paused is not None and paused.paused

    with engine.begin() as conn:
        assert queue.claim_and_lease(conn, "next-tick", ttl_s=90) is None


def test_an_unapproved_resume_stays_at_the_human_gate(engine: Engine, tmp_path: Path) -> None:
    """Possessing a resume token is not an approval decision."""
    pile_id = seed(engine, tmp_path, "unapproved-resume")
    pending(engine, pile_id)
    saver = InMemorySaver()
    assert run(engine, tmp_path, ScriptedLLM(), saver=saver).paused

    attempted = runner.resume_after_approval(
        engine,
        saver,
        settings_for(dsn(engine), tmp_path),
        models(),
        ScriptedLLM(),
        pile_id=pile_id,
        worker_id="premature-worker",
    )

    assert attempted is not None and attempted.paused
    with engine.connect() as conn:
        assert pile_repo.get(conn, pile_id)["status"] == "awaiting_review"


def test_resume_replaces_the_checkpointed_lease_epoch(engine: Engine, tmp_path: Path) -> None:
    """The next fenced node must use the new owner's epoch, not the pauser's."""
    pile_id = seed(engine, tmp_path, "fresh-resume-fence")
    item = pending(engine, pile_id)
    saver = InMemorySaver()

    assert run(engine, tmp_path, ScriptedLLM(), saver=saver).paused
    with engine.connect() as conn:
        before = int(pile_repo.get(conn, pile_id)["lease_epoch"])
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE pending_decisions SET status = 'approved', decided_by = 'reviewer', "
                "decided_at = now() WHERE id = :id"
            ),
            {"id": item},
        )

    resumed = runner.resume_after_approval(
        engine,
        saver,
        settings_for(dsn(engine), tmp_path),
        models(),
        ScriptedLLM(),
        pile_id=pile_id,
        worker_id="worker-B",
    )
    assert resumed is not None and not resumed.paused
    snapshot = build_graph(saver).get_state({"configurable": {"thread_id": pile_id}})
    assert snapshot.values["lease_epoch"] == before + 1


def test_postgres_checkpoint_survives_a_worker_restart_without_reextracting(
    engine: Engine, tmp_path: Path
) -> None:
    """The production saver, not an in-memory stand-in, proves the 4A claim."""
    pile_id = seed(engine, tmp_path, "postgres-resume")
    item = pending(engine, pile_id)
    first = ScriptedLLM()

    # Closing this context simulates the worker process that paused disappearing.
    with checkpointer(dsn(engine)) as first_saver:
        paused = run(engine, tmp_path, first, saver=first_saver)
        assert paused is not None and paused.paused

    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE pending_decisions SET status = 'approved', decided_by = 'reviewer', "
                "decided_at = now() WHERE id = :id"
            ),
            {"id": item},
        )

    second = ScriptedLLM()
    with checkpointer(dsn(engine)) as second_saver:
        resumed = runner.resume_after_approval(
            engine,
            second_saver,
            settings_for(dsn(engine), tmp_path),
            models(),
            second,
            pile_id=pile_id,
            worker_id="worker-after-restart",
        )

    assert resumed is not None and not resumed.paused
    assert first.calls.count("extract") > 0
    assert second.calls == [], "the reopened saver must resume at the interrupt"


def test_a_graph_node_stops_when_its_lease_is_lost(
    engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A node renews at a work boundary and does not write after takeover."""
    from documents_analyst.graph import context as graph_context

    seed(engine, tmp_path, "graph-lost-lease")
    monkeypatch.setattr(graph_context.lease, "heartbeat", lambda *args, **kwargs: False)

    with pytest.raises(LostLease):
        run(engine, tmp_path, ScriptedLLM())


def test_graph_renews_before_every_model_work_boundary(
    engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No slow LLM or embedding call inherits an almost-expired lease."""
    from documents_analyst.graph import context as graph_context

    seed(engine, tmp_path, "renew-at-model-boundaries")
    real_renew = graph_context.RunContext.renew_lease
    calls = 0

    def counted_renew(self, fence):  # noqa: ANN001, ANN202
        nonlocal calls
        calls += 1
        return real_renew(self, fence)

    monkeypatch.setattr(graph_context.RunContext, "renew_lease", counted_renew)
    outcome = run(engine, tmp_path, ScriptedLLM())

    assert outcome is not None
    # Ingest: document, party LLM, chunk embedding. Extract: document, each
    # taxonomy LLM, each open-subject LLM, both embedding batches. Commit: once,
    # before it writes the final document. This is intentionally exact so
    # omitting any boundary makes the regression red.
    sections = len(
        [
            section
            for section in split_sections(CONTRACT)
            if CONTRACT[section.start : section.end].strip()
        ]
    )
    assert calls == 6 + 2 * sections


def test_a_resume_is_refused_while_another_worker_holds_the_pile(
    engine: Engine, tmp_path: Path
) -> None:
    """Two people clicking approve at once must not start two resumes."""
    pile_id = seed(engine, tmp_path, "contended")
    pending(engine, pile_id)
    saver = InMemorySaver()
    run(engine, tmp_path, ScriptedLLM(), saver=saver)

    with engine.begin() as conn:
        assert lease.acquire(conn, pile_id, "worker-A", ttl_s=90) is not None

    refused = runner.resume_after_approval(
        engine,
        saver,
        settings_for(dsn(engine), tmp_path),
        models(),
        ScriptedLLM(),
        pile_id=pile_id,
        worker_id="worker-B",
    )

    assert refused is None


# ── claiming ──────────────────────────────────────────────────────────────


def test_a_leased_pile_is_not_claimed_again(engine: Engine, tmp_path: Path) -> None:
    """Two workers, one pile, one winner."""
    seed(engine, tmp_path, "claimed")

    with engine.begin() as conn:
        first = queue.claim_and_lease(conn, "worker-A", ttl_s=90)
    with engine.begin() as conn:
        second = queue.claim_and_lease(conn, "worker-B", ttl_s=90)

    assert first is not None, "there was work"
    assert second is None, "the pile was already leased"


def test_claiming_leases_and_marks_running_in_one_statement(engine: Engine, tmp_path: Path) -> None:
    """The whole point of `claim_and_lease`, and it is not a race fix.

    Claim-then-lease in two statements is already safe — `lease.ACQUIRE` has
    its own expiry predicate, so exactly one of two contending UPDATEs wins
    either way. What two statements cost is a claimed-but-unowned outcome and a
    `status` that needs a third write, leaving the navigator showing `idle`
    while a worker extracts.

    So this asserts what one statement buys: the epoch, the owner and the
    status all move together. A version that leases without marking passes the
    test above and fails this one.
    """
    pile_id = seed(engine, tmp_path, "running")
    with engine.connect() as conn:
        before = int(pile_repo.get(conn, pile_id)["lease_epoch"])

    with engine.begin() as conn:
        claimed = queue.claim_and_lease(conn, "worker-A", ttl_s=90)

    assert claimed == (pile_id, before + 1)
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT status, lease_owner_id FROM piles WHERE id = :p"), {"p": pile_id}
        ).one()
    assert row.status == "running", "a claimed pile must not still read idle"
    assert row.lease_owner_id == "worker-A"


# ── the visible staged graph ──────────────────────────────────────────────


def test_a_run_leaves_a_replayable_timeline(engine: Engine, tmp_path: Path) -> None:
    """The dashboard's source of truth after a reconnect.

    Sequence numbers are allocated inside the INSERT, so they are gap-free and
    strictly increasing even when two writers append to the same run.
    """
    seed(engine, tmp_path, "timeline")
    outcome = run(engine, tmp_path, ScriptedLLM())
    assert outcome is not None

    with engine.connect() as conn:
        events = event_repo.since(conn, outcome.run_id)

    types = [e["type"] for e in events]
    assert types[0] == "run.started"
    assert "stage.ingest.done" in types
    assert "stage.extract.done" in types
    assert types[-1] == "run.finished"
    assert [e["seq"] for e in events] == list(range(1, len(events) + 1))


def test_replay_can_resume_from_a_sequence(engine: Engine, tmp_path: Path) -> None:
    """ "Everything after seq 3" is the query a reconnecting browser makes."""
    pile_id = seed(engine, tmp_path, "replay")
    outcome = run(engine, tmp_path, ScriptedLLM())
    assert outcome is not None

    with engine.connect() as conn:
        everything = event_repo.since(conn, outcome.run_id)
        tail = event_repo.since(conn, outcome.run_id, after_seq=3)
        pile_view = event_repo.latest_for_pile(conn, pile_id)

    assert len(tail) == len(everything) - 3
    assert all(e["seq"] > 3 for e in tail)
    assert pile_view, "the pile timeline spans runs and must be answerable too"


def test_two_writers_cannot_reuse_a_sequence_number(engine: Engine, tmp_path: Path) -> None:
    """The reason `seq` is allocated in the statement rather than by the caller.

    A caller counting its own events gets it right until a node is retried, and
    then two events share a number — after which the reconnecting browser
    silently skips one.
    """
    pile_id = seed(engine, tmp_path, "seqrace")
    run_id = str(uuid.uuid4())

    with engine.begin() as conn:
        seqs = [
            event_repo.append(conn, pile_id=pile_id, run_id=run_id, type=f"t{i}") for i in range(5)
        ]

    assert seqs == [1, 2, 3, 4, 5]


# ── cost ──────────────────────────────────────────────────────────────────


def test_a_run_records_what_it_spent(engine: Engine, tmp_path: Path) -> None:
    """`stage_runs` had columns and no writer through six phases.

    Cost is a scored deliverable, and a cost report assembled at write-up time
    from a log file that has already rotated is not a report.
    """
    pile_id = seed(engine, tmp_path, "cost")
    llm = ScriptedLLM(
        facts=[fact_in(CONTRACT, "Payment is due Net 30 days from the date of invoice.")]
    )

    outcome = run(engine, tmp_path, llm)
    assert outcome is not None

    with engine.connect() as conn:
        totals = telemetry_repo.totals_for_pile(conn, pile_id)

    assert totals["calls"] > 0, "the extraction calls were not recorded"
    assert any(row["node"] for row in totals["by_node"]), "every row must name a node"


def test_re_chunking_into_fewer_pieces_leaves_no_orphans(engine: Engine, tmp_path: Path) -> None:
    """The case the unique constraint cannot catch.

    `ON CONFLICT (document_id, ordinal) DO NOTHING` makes an identical re-run a
    no-op, so it looks like it provides idempotence on its own. It does not:
    re-window the same text into a shorter list — a different `target_tokens`,
    say — and the tail of the previous run survives as orphaned high-ordinal
    rows with real embeddings, citing a windowing that no longer exists.
    Retrieval keeps finding them, and nothing anywhere reports a problem.
    """
    pile_id = seed(engine, tmp_path, "rechunk")
    with engine.connect() as conn:
        document_id = str(
            conn.execute(
                text("SELECT id FROM documents WHERE pile_id = :p LIMIT 1"), {"p": pile_id}
            ).scalar_one()
        )
    with engine.begin() as conn:
        epoch = lease.acquire(conn, pile_id, "chunker", ttl_s=90)
    assert epoch is not None
    fence = Fence(pile_id=pile_id, epoch=epoch)

    def rows(count: int) -> list[dict[str, Any]]:
        return [
            {
                "ordinal": i,
                "section_path": None,
                "char_start": i,
                "char_end": i + 1,
                "raw_text": f"piece {i}",
                "context": "",
                "embedding": [0.0] * 384,
            }
            for i in range(count)
        ]

    with engine.begin() as conn:
        chunk_repo.replace_for_document(conn, fence, document_id, rows(5))
    with engine.begin() as conn:
        chunk_repo.replace_for_document(conn, fence, document_id, rows(2))

    with engine.connect() as conn:
        assert chunk_repo.count_for_document(conn, document_id) == 2


# ── agreement identity ────────────────────────────────────────────────────


def test_two_documents_with_the_same_parties_share_one_agreement(
    engine: Engine, tmp_path: Path
) -> None:
    """The key that decides what gets compared, and it failed silently.

    `002` declared `UNIQUE (pile_id, parties_normalized, external_ref)` and
    `external_ref` is NULL for almost every agreement — so under the default
    NULLS DISTINCT the constraint never fires, `ON CONFLICT` matches nothing,
    and every document gets its own agreement. Two documents produced sixteen.

    Nothing errors. Conflicts are only ever looked for *within* an agreement,
    so one agreement per document means no two documents are ever compared and
    a pile containing a real contradiction reports a clean bill of health.
    """
    pile_id = seed(
        engine,
        tmp_path,
        "shared",
        files={"01_msa.md": CONTRACT, "02_amendment.md": CONTRACT.replace("Net 30", "Net 45")},
    )

    outcome = run(engine, tmp_path, ScriptedLLM())

    assert outcome is not None and outcome.documents == 2
    with engine.connect() as conn:
        agreements = conn.execute(
            text("SELECT parties_normalized FROM agreements WHERE pile_id = :p"), {"p": pile_id}
        ).all()
        linked = conn.execute(
            text(
                "SELECT count(DISTINCT agreement_id) FROM documents "
                "WHERE pile_id = :p AND agreement_id IS NOT NULL"
            ),
            {"p": pile_id},
        ).scalar_one()

    assert len(agreements) == 1, f"same parties, one agreement — got {len(agreements)}"
    assert linked == 1, "and both documents point at it"


def test_a_run_whose_every_call_failed_is_not_recorded_as_ok(
    engine: Engine, tmp_path: Path
) -> None:
    """A quota-exhausted extraction must not read as a successful one.

    `extract_facts` turns an `LLMError` into a `Rejection` so one bad section
    cannot lose a whole document — which means, from the node, a rate-limited
    run looks exactly like a contract that stated nothing. `stage_runs.ok`
    exists to tell those apart, and a cost report showing successful
    extractions of nothing is worse than no report at all.
    """
    seed(engine, tmp_path, "quota")

    class Exhausted(ScriptedLLM):
        def structured(self, *, system, user, schema, max_tokens):  # noqa: ANN001, ANN003, ANN204
            properties = (schema.get("properties") or {}).keys()
            if "party_indices" in properties:
                return super().structured(
                    system=system, user=user, schema=schema, max_tokens=max_tokens
                )
            raise LLMQuotaExhausted("429 RESOURCE_EXHAUSTED")

    outcome = run(engine, tmp_path, Exhausted())
    assert outcome is not None

    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT node, ok FROM stage_runs WHERE run_id = :r"), {"r": outcome.run_id}
        ).all()

    assert rows, "the failed attempt is still recorded — it still cost wall time"
    assert all(not r.ok for r in rows if r.node == "extract")


def test_an_extracted_pile_leaves_the_queue(engine: Engine, tmp_path: Path) -> None:
    """Otherwise the worker re-runs the same pile for ever.

    `CLAIMABLE` asks for a pile holding `ingested` originals. A document that
    stays `ingested` after being extracted keeps its pile claimable, so the next
    tick re-claims it, re-runs the whole graph and spends the extraction budget
    again on facts it already has. Observed live before this was added: one
    `--once` pass ran the same two documents three times.
    """
    pile_id = seed(engine, tmp_path, "retire")

    first = run(engine, tmp_path, ScriptedLLM())
    assert first is not None and first.documents == 1

    with engine.connect() as conn:
        assert doc_repo.counts(conn, pile_id) == {"extracted": 1}
        assert queue.claim_pile(conn, "worker-A") is None, "nothing left to claim"

    assert run(engine, tmp_path, ScriptedLLM()) is None, "a second pass finds no work"


def test_a_document_whose_calls_all_failed_stays_in_the_queue(
    engine: Engine, tmp_path: Path
) -> None:
    """The other half, and it is the one that matters more.

    Retiring a document after every call failed would take it off the queue
    having learned nothing from it: the pile goes quiet, reports finished, and
    holds a contract nobody ever read. A rate limit must cost a retry, not a
    document.
    """
    pile_id = seed(engine, tmp_path, "retry")

    class Exhausted(ScriptedLLM):
        def structured(self, *, system, user, schema, max_tokens):  # noqa: ANN001, ANN003, ANN204
            if "party_indices" in (schema.get("properties") or {}):
                return super().structured(
                    system=system, user=user, schema=schema, max_tokens=max_tokens
                )
            raise LLMQuotaExhausted("429 RESOURCE_EXHAUSTED")

    assert run(engine, tmp_path, Exhausted()) is not None
    with engine.connect() as conn:
        assert doc_repo.counts(conn, pile_id) == {"ingested": 1}, "still work to do"

    recovered = run(engine, tmp_path, ScriptedLLM(facts=[fact_in(CONTRACT, QUOTE)]))
    assert recovered is not None and recovered.documents == 1
    with engine.connect() as conn:
        assert doc_repo.counts(conn, pile_id) == {"extracted": 1}


def test_a_run_that_retires_nothing_stops_the_drain(engine: Engine, tmp_path: Path) -> None:
    """A provider outage must cost a tick, not a spin.

    `drain_runs` loops until there is no work. A run whose every LLM call
    failed completes normally, writes nothing, and leaves the pile exactly as
    claimable as it found it — so the loop claims it again and does the same
    nothing, up to its circuit breaker. Seen live during a Gemini quota
    outage: one `--once` pass re-ran the same two documents three times.
    """
    seed(engine, tmp_path, "outage")

    class Exhausted(ScriptedLLM):
        def structured(self, *, system, user, schema, max_tokens):  # noqa: ANN001, ANN003, ANN204
            if "party_indices" in (schema.get("properties") or {}):
                return super().structured(
                    system=system, user=user, schema=schema, max_tokens=max_tokens
                )
            raise LLMQuotaExhausted("429 RESOURCE_EXHAUSTED")

    stalled = run(engine, tmp_path, Exhausted())
    assert stalled is not None
    assert not stalled.progressed, "nothing left the queue"

    healthy = run(engine, tmp_path, ScriptedLLM(facts=[fact_in(CONTRACT, QUOTE)]))
    assert healthy is not None and healthy.progressed


def test_the_drain_stops_instead_of_spinning_on_a_stalled_pile(
    engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Asserted on the loop, not just on the flag it reads."""
    from documents_analyst.worker import main as worker_main

    seed(engine, tmp_path, "spin")
    saver = InMemorySaver()

    class Exhausted(ScriptedLLM):
        def structured(self, *, system, user, schema, max_tokens):  # noqa: ANN001, ANN003, ANN204
            if "party_indices" in (schema.get("properties") or {}):
                return super().structured(
                    system=system, user=user, schema=schema, max_tokens=max_tokens
                )
            raise LLMQuotaExhausted("429 RESOURCE_EXHAUSTED")

    env = worker_main.GraphEnv(checkpointer=saver, models=models(), llm=Exhausted())
    ran = worker_main.drain_runs(engine, settings_for(dsn(engine), tmp_path), env, limit=8)

    assert ran == 1, "one attempt, then wait for the next tick"
