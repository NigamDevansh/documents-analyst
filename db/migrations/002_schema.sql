-- 002 · The schema. Fourteen tables, in dependency order.
--
-- Raw SQL rather than Alembic on purpose: the schema IS the story of this
-- system, and a file a reviewer can read top to bottom beats a generated
-- revision chain. pgvector's HNSW DDL is also awkward through Alembic.
--
-- **This file was twenty-two files.** The schema was built forward one
-- migration at a time and eight of those migrations existed only to correct an
-- earlier one — a column added in 004 dropped in 018, a unique key from 010
-- replaced in 017, three CHECK constraints rewritten. That history is the
-- system's, not the schema's; what is here is what those files added up to,
-- verified by building the schema both ways and diffing every column,
-- constraint, index and comment.
--
-- Forward-only and checksummed still holds from here: `migrate.py` refuses a
-- file that changed after it was applied, and the next change to this schema is
-- a new numbered file, not an edit to this one.

-- ── piles ────────────────────────────────────────────────────────────────
-- A folder under WATCH_ROOT. There is no "create a pile" step; a directory
-- appearing is the whole of it.
CREATE TABLE piles (
    id                        uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    name                      text        NOT NULL UNIQUE,
    watch_path                text        NOT NULL,
    rulebook_path             text,
    status                    text        NOT NULL DEFAULT 'idle'
                              CHECK (status IN ('idle','scanning','running',
                                                'awaiting_review','parked','error')),

    -- The generated document. `deliverable_sections` is exactly what was last
    -- written, in order — {slot_id, agreement, heading, body, sha256} per section —
    -- so Resolve can repair a missing file and Reconcile can read the file back
    -- and prove it says what we recorded. `deliverable_sha256` hashes the ordered
    -- section hashes: one value that changes iff any section changed.
    deliverable_path          text,
    deliverable_sha256        text,
    deliverable_sections      jsonb       NOT NULL DEFAULT '[]',
    deliverable_version       integer     NOT NULL DEFAULT 0,

    -- The lease. One worker owns a pile at a time, and `lease_epoch` is what
    -- every mutating statement is fenced on: a worker that lost the pile writes
    -- nothing, rather than writing late.
    lease_owner_id            text,
    lease_epoch               bigint      NOT NULL DEFAULT 0,
    lease_expires_at          timestamptz,

    created_at                timestamptz NOT NULL DEFAULT now(),
    updated_at                timestamptz NOT NULL DEFAULT now()
);

COMMENT ON COLUMN piles.lease_epoch IS
    'Incremented on every acquire. Carried in the graph state and checked by '
    'every write, so a worker that lost the lease cannot land a stale update.';

-- ── agreements ───────────────────────────────────────────────────────────
-- One contract, which is usually several documents: an MSA, its SOW, the
-- invoice that references it. What makes them one contract is the party set.
CREATE TABLE agreements (
    id                 uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    pile_id            uuid        NOT NULL REFERENCES piles(id) ON DELETE CASCADE,
    title              text,
    parties_normalized text[]      NOT NULL,
    external_ref       text,
    family             text,
    created_at         timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT agreements_identity_key
        UNIQUE NULLS NOT DISTINCT (pile_id, parties_normalized, external_ref, family)
);

COMMENT ON COLUMN agreements.parties_normalized IS
    'Sorted, casefolded party names. The identity of an agreement, which is why '
    'party extraction returns verbatim spans rather than a model''s paraphrase.';

COMMENT ON COLUMN agreements.family IS
    'The kind of contract: nda, lease, services, supply, … NULL for a document '
    'that does not stand on its own (amendment, SOW), which joins the existing '
    'agreement for the same parties instead of forming a new one.';

COMMENT ON CONSTRAINT agreements_identity_key ON agreements IS
    'NULLS NOT DISTINCT: a NULL external_ref or family must collide with another '
    'NULL, or every document without one would open its own agreement.';

-- ── documents ────────────────────────────────────────────────────────────
CREATE TABLE documents (
    id                     uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    pile_id                uuid        NOT NULL REFERENCES piles(id) ON DELETE CASCADE,
    agreement_id           uuid        REFERENCES agreements(id) ON DELETE SET NULL,

    sha256                 text        NOT NULL,
    -- Hash of the EXTRACTED TEXT, not the bytes. The same contract as .docx and
    -- as .md are different files and one document; without this the register
    -- reports every term twice and calls the copies corroboration.
    text_sha256            text,
    filename               text        NOT NULL,
    extracted_text         text,

    supersedes_document_id uuid        REFERENCES documents(id) ON DELETE SET NULL,
    alias_of_document_id   uuid        REFERENCES documents(id) ON DELETE SET NULL,
    paths                  jsonb       NOT NULL DEFAULT '{}',

    status                 text        NOT NULL DEFAULT 'queued'
                           CHECK (status IN ('queued','ingested','extracted',
                                             'superseded','removed','quarantined')),
    attempts               integer     NOT NULL DEFAULT 0,
    quarantine_error       text,
    extraction_ok          boolean     NOT NULL DEFAULT true,

    created_at             timestamptz NOT NULL DEFAULT now(),
    updated_at             timestamptz NOT NULL DEFAULT now(),

    UNIQUE (pile_id, sha256)
);

COMMENT ON COLUMN documents.extracted_text IS
    'WRITE-ONCE. Never patched in place. A replaced file gets a NEW row with '
    'supersedes_document_id, because shifting this string would silently '
    'invalidate every citation that points into it.';

COMMENT ON COLUMN documents.paths IS
    'path → {size, mtime_ns, inode}. Presence is NOT stored — it is derived per '
    'scan by intersecting these keys with what the filesystem walk found, so a '
    'stale flag can never outlive the disk.';

COMMENT ON COLUMN documents.extraction_ok IS
    'False when every section call failed for this document, so its fact set is '
    'known-incomplete rather than known-empty. Absence-based rules (assert: '
    'exists) are not evaluated against an agreement holding such a document.';

COMMENT ON COLUMN documents.alias_of_document_id IS
    'Same extracted text as another document in this pile. Kept as a real row so '
    'the register can say which file a term was read from, and excluded from the '
    'extraction queue so one contract is not read twice and reported as agreeing.';

-- ── contract_fields ──────────────────────────────────────────────────────
-- The closed taxonomy. Seeded in 003 and checked against `taxonomy/fields.py`
-- by a test: extraction is schema-constrained to these keys, so a model cannot
-- mint `late_fee` on one run and `late_payment_penalty` on the next and hide a
-- contradiction between them.
CREATE TABLE contract_fields (
    field_key   text PRIMARY KEY,
    field_group text NOT NULL,
    value_type  text NOT NULL
                CHECK (value_type IN ('money','percent','date','duration_days',
                                      'duration_months','duration_hours',
                                      'boolean','enum','text','party_list')),
    label       text NOT NULL,
    description text NOT NULL,
    multi       boolean NOT NULL DEFAULT false
);

COMMENT ON COLUMN contract_fields.description IS
    'Goes into the extraction prompt verbatim — the only instruction the model '
    'gets about what this field means. Written for the model, not for us.';

-- ── document_chunks ──────────────────────────────────────────────────────
-- Where each part of a document begins and ends. **Not embedded**: extraction
-- runs per section, and the hybrid retrieval query these rows were sized for
-- was never written, so the vector column and the tsvector both went. The
-- offsets are the payload.
CREATE TABLE document_chunks (
    id           uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id  uuid        NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    pile_id      uuid        NOT NULL REFERENCES piles(id) ON DELETE CASCADE,
    ordinal      integer     NOT NULL,

    section_path text,
    char_start   integer     NOT NULL,
    char_end     integer     NOT NULL,
    raw_text     text        NOT NULL,
    -- A short prefix ("MSA · 4.2 Payment Terms"), in its own column. Never part
    -- of raw_text and never counted in the offsets, or every citation inside
    -- the chunk would be shifted by its length.
    context      text        NOT NULL DEFAULT '',

    created_at   timestamptz NOT NULL DEFAULT now(),

    UNIQUE (document_id, ordinal),
    CHECK (char_end >= char_start)
);

COMMENT ON TABLE document_chunks IS
    'Where each part of a document begins and ends. Not embedded: the offsets '
    'are the payload. Citations still resolve against documents.extracted_text, '
    'never against these rows.';

-- ── facts_and_claims ─────────────────────────────────────────────────────
-- Every fact is grounded or it does not exist. `source_quote` must appear
-- verbatim at [char_start, char_end) in the source document's extracted_text —
-- that check is Gate 1, and it is the reason this system never bluffs.
CREATE TABLE facts_and_claims (
    id                 uuid          PRIMARY KEY DEFAULT gen_random_uuid(),
    agreement_id       uuid          NOT NULL REFERENCES agreements(id) ON DELETE CASCADE,

    field_key          text          REFERENCES contract_fields(field_key),
    subject_free       text,
    value              jsonb         NOT NULL,
    -- The comparable form. NULL for free text on purpose: normalising prose
    -- invents structure, and structure invented here is structure a conflict
    -- check will later treat as fact.
    value_norm         text,

    source_document_id uuid          NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    char_start         integer       NOT NULL,
    char_end           integer       NOT NULL,
    source_quote       text          NOT NULL CHECK (source_quote <> ''),

    -- Open subjects only. A taxonomy fact is matched by `field_key`, exactly and
    -- for free, and nothing has ever compared its vector.
    fact_key_embedding vector(384),
    -- Which embedder produced it. Two models can share a width and share no
    -- meaning, so a cosine between vectors from different models is noise that
    -- looks like a score. NULL means no vector.
    embedding_model    text,

    -- The FK is declared after claim_slots exists — the two tables point at
    -- each other, and this is the half that has to wait.
    slot_id            uuid,
    role               text          NOT NULL DEFAULT 'current'
                       CHECK (role IN ('current','corroborating','competing',
                                       'superseded','rejected')),
    value_group        integer,
    status             text          NOT NULL DEFAULT 'active'
                       CHECK (status IN ('active','superseded','orphaned','rejected')),

    created_at         timestamptz   NOT NULL DEFAULT now(),

    CHECK (char_end >= char_start),
    CHECK (field_key IS NOT NULL OR subject_free IS NOT NULL)
);

COMMENT ON COLUMN facts_and_claims.value_group IS
    'Facts asserting the SAME value share a group. Three documents saying '
    '30/45/60 days are three groups in one slot — one question for a human, '
    'asked once, instead of N pairwise conflicts.';

COMMENT ON COLUMN facts_and_claims.source_quote IS
    'Must equal extracted_text[char_start:char_end] exactly. Gate 1 refuses the '
    'fact otherwise — a claim whose quote cannot be located is not recorded.';

COMMENT ON COLUMN facts_and_claims.embedding_model IS
    'Which embedder produced fact_key_embedding. Two models can share a width '
    'and share no meaning, so a cosine between vectors from different models is '
    'noise that looks like a score. NULL means no vector.';

-- ── claim_slots ──────────────────────────────────────────────────────────
-- One question about one agreement: "what does this say about payment terms?"
-- Every fact answering it lands here, and every fact answering it the same way
-- shares a `value_group`.
CREATE TABLE claim_slots (
    id              uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    agreement_id    uuid        NOT NULL REFERENCES agreements(id) ON DELETE CASCADE,

    field_key       text        REFERENCES contract_fields(field_key),
    subject_free    text,

    state           text        NOT NULL DEFAULT 'settled'
                    CHECK (state IN ('settled','disputed','resolved','contested')),
    current_fact_id uuid        REFERENCES facts_and_claims(id) ON DELETE SET NULL,
    resolved_by     text,
    resolved_at     timestamptz,

    -- A rename a human made. Register content is generated from the taxonomy,
    -- so without this the next update re-emits the taxonomy label and silently
    -- undoes them.
    label_override  text,
    retired_at      timestamptz,

    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT claim_slots_identity_key
        UNIQUE NULLS NOT DISTINCT (agreement_id, field_key, subject_free),
    CONSTRAINT claim_slots_one_subject
        CHECK ((field_key IS NULL) <> (subject_free IS NULL))
);

COMMENT ON COLUMN claim_slots.state IS
    'settled = one agreed value · disputed = sources conflict, nobody decided · '
    'resolved = a human chose · contested = new conflict arrived on a resolved '
    'slot. A resolved slot is NEVER silently overridden or reverted to disputed.';

COMMENT ON COLUMN claim_slots.subject_free IS
    'A material term with no taxonomy key — "data residency region". Matched '
    'between documents by embedding rather than by key, because one contract''s '
    '"data residency region" is another''s "data storage location". Mutually '
    'exclusive with field_key.';

COMMENT ON COLUMN claim_slots.retired_at IS
    'Set when an approved orphaned_claims removal has landed in the register. '
    'Retired slots are excluded from the register render and from structural '
    'verification; their facts and citations are kept.';

COMMENT ON CONSTRAINT claim_slots_one_subject ON claim_slots IS
    'A slot is a taxonomy field or a free subject, never both and never neither.';

-- The other half of the cycle: facts point at the slot that claimed them.
ALTER TABLE facts_and_claims
    ADD CONSTRAINT facts_slot_fk
    FOREIGN KEY (slot_id) REFERENCES claim_slots(id) ON DELETE SET NULL;

-- ── pending_decisions ────────────────────────────────────────────────────
-- The review queue. Everything a person has to answer, in one table, so
-- "what is waiting on me" is one query.
CREATE TABLE pending_decisions (
    id                 uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    pile_id            uuid        NOT NULL REFERENCES piles(id) ON DELETE CASCADE,

    kind               text        NOT NULL
                       CHECK (kind IN ('conflict','amendment','finding',
                                       'orphaned_claims','unverified_document',
                                       'unassigned_agreement','label_change')),
    ref_id             uuid,
    payload            jsonb       NOT NULL DEFAULT '{}',

    -- How the competing values relate, which is narrower than `kind`: the queue
    -- lane comes from `kind`, the sentence shown to a person comes from here.
    relationship       text        CHECK (relationship IN ('conflict','amendment',
                                                           'same_passage','unknown')),
    detected_by        text        CHECK (detected_by IN ('rule','nli','llm')),
    evidence_quote     text,
    group_key          text,
    recommendation     text,

    status             text        NOT NULL DEFAULT 'pending'
                       CHECK (status IN ('pending','approved','rejected','superseded')),
    chosen_value_group integer,
    decided_by         text,
    decided_at         timestamptz,
    decided_note       text,

    created_at         timestamptz NOT NULL DEFAULT now()
);

COMMENT ON COLUMN pending_decisions.relationship IS
    'How the competing values relate. conflict: two documents disagree. '
    'amendment: a later document replaces an earlier one, evidence verified. '
    'same_passage: one paragraph of one document states both — a rule and its '
    'exception, or one clause covering several parties. unknown: not established.';

COMMENT ON COLUMN pending_decisions.status IS
    'DEFAULT-DENY: an item nobody decided on stays pending and is never treated '
    'as approved. That is a data structure, not an if-statement.';

COMMENT ON COLUMN pending_decisions.group_key IS
    'Items sharing a group_key describe the same clause and render as one review '
    'card carrying every label and every piece of evidence.';

COMMENT ON COLUMN pending_decisions.decided_by IS
    'The reviewer''s name. The only authority this system has for editing a '
    'register, which is why the API refuses a decision without one.';

-- ── findings ─────────────────────────────────────────────────────────────
CREATE TABLE findings (
    id           uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    agreement_id uuid        NOT NULL REFERENCES agreements(id) ON DELETE CASCADE,

    rule_id      text        NOT NULL,
    severity     text        NOT NULL CHECK (severity IN ('low','medium','high')),
    evaluated_by text        NOT NULL CHECK (evaluated_by IN ('check','judge','scan')),
    message      text        NOT NULL,
    citations    jsonb       NOT NULL DEFAULT '[]',
    status       text        NOT NULL DEFAULT 'open'
                 CHECK (status IN ('open','acknowledged','dismissed')),

    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now(),

    UNIQUE (agreement_id, rule_id)
);

COMMENT ON COLUMN findings.evaluated_by IS
    'check: a rulebook rule. judge: the LLM tier, evidence verified. '
    'scan: the document itself carries text aimed at this system — reported, '
    'never obeyed, and never used to change a fact.';

-- ── deliverable_versions ─────────────────────────────────────────────────
-- What changed in the document, why, and on whose decision. Append-only, one
-- row per changed SECTION per version. Sections a commit did not change get no
-- row — their hash in `piles.deliverable_sections` is unchanged, which is the
-- proof they were not touched.
CREATE TABLE deliverable_versions (
    id              uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    pile_id         uuid        NOT NULL REFERENCES piles(id) ON DELETE CASCADE,
    version         integer     NOT NULL,

    changed_slot_id uuid        REFERENCES claim_slots(id) ON DELETE SET NULL,
    section_label   text,
    operation       text        CHECK (operation IN ('create','edit','delete')),
    before_text     text,
    after_text      text,
    before_sha256   text,
    after_sha256    text,
    reason          text        NOT NULL,
    decision_id     uuid        REFERENCES pending_decisions(id) ON DELETE SET NULL,

    created_at      timestamptz NOT NULL DEFAULT now()
);

COMMENT ON COLUMN deliverable_versions.operation IS
    'create: a section appeared (first write, or a newly discovered field). '
    'edit: a section''s text changed. delete: an approved orphan removal.';

-- ── deliverable_commits ──────────────────────────────────────────────────
-- Which decisions reached the document, and in which version. Written in the
-- same transaction as the version bump, so "approved but not yet in the
-- document" is a NOT EXISTS against this table and nothing else.
CREATE TABLE deliverable_commits (
    id                 uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    pile_id            uuid        NOT NULL REFERENCES piles(id) ON DELETE CASCADE,

    decision_ids       uuid[]      NOT NULL DEFAULT '{}',
    slot_ids           uuid[]      NOT NULL DEFAULT '{}',
    version            integer     NOT NULL,
    document_sha256    text        NOT NULL,
    lease_epoch        bigint      NOT NULL,

    created_at         timestamptz NOT NULL DEFAULT now()
);

-- ── stage_events ─────────────────────────────────────────────────────────
-- The run, as a durable ordered table rather than a log. "Everything after 42"
-- is a query, which is what makes SSE reconnect a replay instead of a gap.
CREATE TABLE stage_events (
    id         bigserial   PRIMARY KEY,
    pile_id    uuid        NOT NULL REFERENCES piles(id) ON DELETE CASCADE,
    run_id     uuid        NOT NULL,
    seq        bigint      NOT NULL,
    type       text        NOT NULL,
    payload    jsonb       NOT NULL DEFAULT '{}',
    created_at timestamptz NOT NULL DEFAULT now(),

    UNIQUE (run_id, seq)
);

COMMENT ON COLUMN stage_events.id IS
    'The SSE cursor. Monotonic across the whole table, unlike seq, which restarts '
    'per run — a browser reconnecting sends this and gets exactly what it missed.';

-- ── stage_runs ───────────────────────────────────────────────────────────
-- What each stage spent and how long it took. Failed attempts included: a retry
-- that succeeded on the third try still spent the first two.
CREATE TABLE stage_runs (
    id             uuid          PRIMARY KEY DEFAULT gen_random_uuid(),
    pile_id        uuid          NOT NULL REFERENCES piles(id) ON DELETE CASCADE,
    run_id         uuid          NOT NULL,
    node           text          NOT NULL,

    model          text,
    tokens_in      integer       NOT NULL DEFAULT 0,
    tokens_out     integer       NOT NULL DEFAULT 0,
    attempts       integer       NOT NULL DEFAULT 1,
    ok             boolean       NOT NULL DEFAULT true,
    -- Wall time for the whole exchange, retries included: a call that took nine
    -- seconds across three tries reports nine, because three is what makes a
    -- stage slow.
    latency_ms     integer,

    llm_cost_usd   numeric(12,6) NOT NULL DEFAULT 0,

    created_at     timestamptz   NOT NULL DEFAULT now()
);

COMMENT ON COLUMN stage_runs.ok IS
    'FALSE attempts are recorded too. A retry that succeeded on attempt three '
    'still cost tokens, and a cost report that hides that is lying.';

COMMENT ON COLUMN stage_runs.llm_cost_usd IS
    'Zero when the model has no entry in config/model_prices.json. Reported as '
    'unpriced, never as free — we do not ship rates we cannot verify.';
