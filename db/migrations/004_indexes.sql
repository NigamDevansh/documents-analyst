-- 004 · Indexes, each one for a query that exists.
--
-- Named after the question rather than the columns, so a plan that uses one
-- says why it was created. Every partial index here has its predicate matched
-- by real code — a partial index whose WHERE clause no query repeats is never
-- chosen, and looks like coverage.
--
-- Two indexes were dropped when this file was consolidated: `chunks_tsv_idx`
-- and `chunks_embedding_idx`, the keyword and vector halves of a hybrid
-- retrieval query that was described in the architecture and never written, and
-- `stage_events_pile_idx`, which was byte-identical to `events_pile_idx` —
-- created twice, three migrations apart, and invisible until the schema was
-- read in one piece.

-- ── discovery and ingest ─────────────────────────────────────────────────
-- The lease sweep: which leases have expired.
CREATE INDEX piles_lease_idx ON piles (lease_expires_at)
    WHERE lease_owner_id IS NOT NULL;

-- Same extracted text, different file. The alias check runs per document.
CREATE INDEX documents_text_sha_idx ON documents (pile_id, text_sha256);

CREATE INDEX documents_agreement_idx ON documents (agreement_id);

-- The extraction queue, exactly as `CLAIMABLE_SQL` asks for it — an alias is a
-- real row with real text and must never be extracted a second time.
CREATE INDEX documents_claimable_idx ON documents (pile_id, created_at)
    WHERE status = 'ingested' AND alias_of_document_id IS NULL;

-- Documents whose extraction failed, which the audit stage needs: absence
-- caused by a failed model call is not absence in the contract.
CREATE INDEX documents_extraction_failed_idx ON documents (agreement_id)
    WHERE extraction_ok = false;

CREATE INDEX chunks_document_idx ON document_chunks (document_id, ordinal);

-- ── facts and slots ──────────────────────────────────────────────────────
CREATE INDEX facts_slot_idx   ON facts_and_claims (slot_id, role);
CREATE INDEX facts_source_idx ON facts_and_claims (source_document_id);

CREATE INDEX facts_field_idx  ON facts_and_claims (agreement_id, field_key)
    WHERE status = 'active';

-- Open subjects carrying a vector: what `match` compares, and what it re-embeds
-- when the model that produced it is not the configured one.
CREATE INDEX facts_embedding_model_idx ON facts_and_claims (agreement_id)
    WHERE subject_free IS NOT NULL AND fact_key_embedding IS NOT NULL;

-- HNSW rather than IVFFlat: no training step, and it behaves well on a corpus
-- that grows one document at a time, which is exactly this workload.
CREATE INDEX facts_key_embedding_idx
    ON facts_and_claims USING hnsw (fact_key_embedding vector_cosine_ops);

CREATE INDEX agreements_parties_idx ON agreements (pile_id, parties_normalized);

-- A retired slot is not rendered and not compared; every read of live slots
-- carries this predicate.
CREATE INDEX claim_slots_live_idx ON claim_slots (agreement_id)
    WHERE retired_at IS NULL;

-- ── review and audit ─────────────────────────────────────────────────────
-- "What is waiting on me", the query behind every review screen.
CREATE INDEX decisions_open_idx ON pending_decisions (pile_id, created_at)
    WHERE status = 'pending';

-- Items about the same sentence, batched into one question.
CREATE INDEX decisions_group_idx ON pending_decisions (pile_id, group_key)
    WHERE group_key IS NOT NULL;

CREATE INDEX findings_open_idx ON findings (agreement_id)
    WHERE status = 'open';

-- ── document commits ─────────────────────────────────────────────────────
-- "Has this decision reached the document?" — looked up by pile, then by
-- membership in `decision_ids`.
CREATE INDEX deliverable_commits_pile_idx ON deliverable_commits (pile_id);
CREATE INDEX deliverable_versions_pile_idx ON deliverable_versions (pile_id, version DESC);

-- ── events and cost ──────────────────────────────────────────────────────
-- The SSE cursor read: everything for this pile, newest first.
CREATE INDEX events_pile_idx   ON stage_events (pile_id, id DESC);
-- Replaying one run in order.
CREATE INDEX events_replay_idx ON stage_events (run_id, seq);

CREATE INDEX runs_cost_idx     ON stage_runs (pile_id, run_id, node);
CREATE INDEX stage_runs_run_idx ON stage_runs (run_id, node);

-- ── the checkpointer's own schema ────────────────────────────────────────
--
-- `PostgresSaver.setup()` issues CREATE TABLE IF NOT EXISTS for four tables and
-- takes no schema argument — it writes wherever `search_path` points. Pointing
-- it at its own schema keeps "Task 1 owns fourteen tables in `public`" literally
-- true rather than approximately true, and keeps the drift test free of an
-- allow-list. It also makes the boundary visible in `psql`: `\dt public.*` is
-- ours, `\dt langgraph.*` is the library's.
--
-- We create the empty room; we do not furnish it.
CREATE SCHEMA IF NOT EXISTS langgraph;

COMMENT ON SCHEMA langgraph IS
    'Owned by langgraph-checkpoint-postgres, created by its own setup(). '
    'Not described in tables.py and not covered by the schema-drift test — '
    'the library owns its shape, including its own migration table.';

-- ── notifications ────────────────────────────────────────────────────────
--
-- On triggers rather than in the routes that write the rows, for two reasons:
-- a caller cannot forget one, and a row written by hand in `psql` — which is
-- exactly what happens while debugging a stuck pile — wakes the system the same
-- way the API does.
--
-- **Both are latency hints and never sources of truth.** The API replays from
-- `stage_events` on reconnect and the worker sweeps for resumable piles every
-- tick, so a lost notification costs a scan interval, not correctness.

-- Identity only, never the payload: `pg_notify` raises above 8000 bytes, and
-- that error would abort the transaction that wrote the event.
CREATE OR REPLACE FUNCTION notify_stage_event() RETURNS trigger AS $$
BEGIN
    PERFORM pg_notify('analyst_events', json_build_object(
        'id',      NEW.id,
        'pile_id', NEW.pile_id,
        'run_id',  NEW.run_id,
        'seq',     NEW.seq,
        'type',    left(NEW.type, 200)
    )::text);
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

COMMENT ON FUNCTION notify_stage_event() IS
    'Fans a stage event out to the API''s single LISTEN connection. Identity '
    'only — never payload, which is unbounded and would abort the writing '
    'transaction at 8000 bytes.';

CREATE TRIGGER stage_events_notify
    AFTER INSERT ON stage_events
    FOR EACH ROW EXECUTE FUNCTION notify_stage_event();

-- A decision leaving `pending` is the one event that makes a paused pile
-- resumable, so it wakes the worker rather than waiting out the scan interval.
CREATE OR REPLACE FUNCTION notify_decision_settled() RETURNS trigger AS $$
BEGIN
    IF NEW.status IS DISTINCT FROM OLD.status AND OLD.status = 'pending' THEN
        PERFORM pg_notify('analyst_work', NEW.pile_id::text);
    END IF;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER pending_decisions_notify
    AFTER UPDATE OF status ON pending_decisions
    FOR EACH ROW EXECUTE FUNCTION notify_decision_settled();
