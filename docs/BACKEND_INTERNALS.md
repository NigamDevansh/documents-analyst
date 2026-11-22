# Backend internals

Phase 4. How `backend/` is put together — the graph, the API, the data layer,
the MCP server.

Companion to [`CODE_ARCHITECTURE.md`](CODE_ARCHITECTURE.md) (foundations),
[`CORE_INTERNALS.md`](CORE_INTERNALS.md) (the shared library),
[`WORKER_RUNTIME.md`](WORKER_RUNTIME.md) (threads and lifecycle) and
[`REPO_STRUCTURE.md`](REPO_STRUCTURE.md) (layout).

---

## 1. Layout

```
backend/src/documents_analyst/
├── settings.py          the ONLY place os.environ is read
├── pricing.py           config/model_prices.json → prices(); price_warnings()
├── deliverable.py       writes and reads back final-document.docx
├── api/                 async — FastAPI
│   ├── main.py          app factory
│   ├── deps.py          connection + settings injection
│   ├── schemas.py       pydantic request/response
│   ├── events.py        SSE fan-out (one LISTEN, many subscribers)
│   └── routes/          piles · documents · decisions · deliverable · citations · events · cost
├── worker/              sync — see WORKER_RUNTIME.md
│   ├── main.py  scanner.py  ingest.py  lease.py  queue.py  runner.py  notify.py
├── graph/
│   ├── build.py         StateGraph wiring + Postgres checkpointer
│   ├── state.py         PileRunState
│   └── nodes/           ingest · extract · ground · match · classify ·
│                        audit · human_gate · commit · resolve · reconcile
├── rules/               loader.py (YAML → Rule) · evaluator.py (5 ops + judge)
├── register/            slots.py · render.py
├── db/                  tables.py · rows.py · repo/ · migrate.py
└── mcp/                 a separate process; HTTP only
    ├── api.py           the client for our own API — the architectural boundary
    ├── server.py        7 composed tools
    └── tools.py         `python -m documents_analyst.mcp.tools` prints the surface
```

---

## 2. The graph

### 2.1 Run scope — one run per pile, batching what is ingested

A run picks up **every ingested, graph-ready document** in the pile, not one.

The graph is `Resolve → Ingest → Extract → Match → Classify → Audit →
HumanGate → Commit → Reconcile → Audit (pass 2)`. `Ingest` and `Extract` loop
over the batch **one document at a time, in discovery order** (`ORDER BY
created_at`): file 1 is fully extracted before file 2 starts, and no two files
of one pile are ever in flight at once. Grounding (Gate 1) happens inside
`Extract`, per quote (§2.5). The cross-file stages — `Match`, `Classify`
(conflicts and amendments), `Audit` against the rulebook, `HumanGate`, and the
`Commit` that writes the final document — happen **once** for the whole batch.

It is not "finish file 1, including the final document, before file 2". A
conflict needs two files, so the document is written once per review round, not
once per file. A file added later is one more run of the same shape, and only
the sections it affects change. Separate *piles* do run in parallel — up to
`pile_concurrency` per worker, one thread lane each — but that never splits a
pile; see [`WORKER_RUNTIME.md`](WORKER_RUNTIME.md) §1a.

Why: the first build of a 4-document pile becomes one run with one review
queue. A single new document later is also one run, same shape. If it were
one run per document, that first build would mean four separate trips to the
human gate — a bad first experience for no benefit.

### 2.2 `thread_id` is the pile id

LangGraph keys checkpoints by `thread_id`. We use **`pile_id`**, not `run_id`.

That is what makes resume-after-approval work: the API knows only the pile, so a
worker can call `graph.invoke(None, config={"thread_id": pile_id})` and land back
in the interrupt. It also means one active run per pile by construction, which
the lease already guarantees anyway. (LangGraph's `thread_id` is a checkpoint
key, not an OS thread. The worker's pile lanes never share one, because two
lanes never hold the same pile; the one `PostgresSaver` they share locks per
cursor, so concurrent checkpoints for different piles are safe.)

`None` there is load-bearing. Passing the state again restarts the pile from
`Ingest` and re-extracts every document a reviewer has been looking at —
overwriting the very facts their decision refers to, and paying for it twice.

#### Where the checkpoints live

`PostgresSaver` owns four tables (`checkpoints`, `checkpoint_blobs`,
`checkpoint_writes`, `checkpoint_migrations`), creates them itself, and takes no
schema argument — it writes wherever `search_path` points. Left alone that is
`public`, and our fourteen tables quietly become eighteen.

So the checkpointer connects with `search_path=langgraph,public`
(`graph/checkpointer.py`, schema created in migration `004_indexes`). Their tables,
their migrations, in a room with their name on it; `\dt public.*` stays exactly
our schema. `public` remains on the path because LangGraph's own DDL references
things `001_extensions` installed there.

Nothing in `documents_analyst/` reads those tables. A checkpoint is the graph runtime's
internal representation, and querying it behind LangGraph's back is how a resume
starts disagreeing with itself about where it was.

### 2.3 The state — six fields, all control flow

```python
class PileRunState(TypedDict):
    pile_id: str
    run_id: str
    lease_epoch: int
    document_ids: list[str]
    audit_pass: int  # 1 = before the gate, 2 = after commit
    attempt: int
```

| Field | What it is | Where it comes from |
|---|---|---|
| `pile_id` | Which pile this run owns | The runner's claim — `SELECT … FROM piles … FOR UPDATE SKIP LOCKED` in `worker/runner.py` |
| `run_id` | Correlates everything: `stage_events`, `stage_runs`, log lines | Generated by the runner when the run starts |
| `lease_epoch` | The fencing token. **Every write carries it** — `… AND lease_epoch = :mine` | Returned by `lease.hold()` in `worker/lease.py` |
| `document_ids` | The batch this run is responsible for | Queue read at run start: documents in this pile with `status='ingested'` |
| `audit_pass` | Whether this is the pre-gate audit or the post-commit one. Nothing in the database records this, so it *must* live in state | Set to `1` by the graph, bumped to `2` by `Reconcile` |
| `attempt` | Bounded node retries | Incremented by our retry wrapper |

**That is the whole state.** No facts, no conflicts, no findings, no register
text. Six small values.

### 2.4 Why everything else is read from the database

Each node opens a short transaction and reads what it needs by `pile_id` /
`run_id`. Four reasons, in order of how much they matter:

1. **A resumed run must see current data.** A run pauses at the gate for two
   hours while a human reviews. If facts were carried in state, resuming would
   act on a two-hour-old snapshot — and a second document may have arrived since.
2. **The checkpoint stays inspectable.** Six fields is something you can read in
   `psql` when debugging. A serialised blob of facts is not.
3. **The UI needs the same data.** Decisions, facts and findings have to be
   queryable by the API. If they only existed inside a checkpoint, the dashboard
   could not show them.
4. **State schema changes would break old checkpoints.** Keeping state tiny and
   stable means a deploy mid-run does not strand a paused pile.

The cost is a few extra queries per node. Against LLM latency measured in
seconds, that is nothing.

### 2.5 Nodes — what each reads and writes

| Node | Reads | Writes |
|---|---|---|
| `Resolve` | `piles.deliverable_sections`, the file on disk | nothing, unless the file is missing, unreadable or differs — then rewrites it from the record (version unchanged) |
| `Ingest` | `documents.extracted_text` | `document_chunks`, `agreements`, `documents.agreement_id` |
| `Extract` | `documents`, taxonomy | `facts_and_claims` — only quotes that pass Gate 1, and only once every section and the open-subject pass answered. An incomplete document writes no facts and counts an attempt (`documents.attempts`); after `job_max_attempts` it is quarantined (`extraction failed after N attempts: …`) and the pile carries on |
| `Match` | `facts_and_claims` | `claim_slots`, `facts.slot_id`, `facts.value_group` |
| `Classify` | `claim_slots` + their grouped facts, the source paragraphs | `claim_slots.state`, `facts.role`, `pending_decisions` (§2.5b) |
| `Audit` | `facts_and_claims`, `rulebook.yaml` | `findings`, `pending_decisions` |
| `HumanGate` | — | **interrupt**; checkpoint is the pause |
| `Commit` | approved `pending_decisions`, `claim_slots`, `piles.deliverable_sections` | applies each approved choice to its slot, **then** renders, writes `final-document.docx` if any section changed, and records `piles.deliverable_*`, `deliverable_versions`, `deliverable_commits` — one transaction |
| `Reconcile` | `piles.deliverable_sections`, the file on disk | an `unverified_document` review item on a mismatch; bumps `audit_pass` to 2 |

#### Two corrections this table used to get wrong

**`Ground` is not a node.** It was listed between `Extract` and `Match`, with
`Extract` producing "candidate facts (in memory)". Nodes communicate only
through the state, and §2.4 forbids business data there — so a separate `Ground`
node would need a batch of *unverified* claims serialised into a Postgres
checkpoint one step before being verified. Gate 1 therefore stays where it
already was, inside `provenance/extract.ground_fact`, wrapped directly around
the model output. A candidate never exists as a value anything else can reach.

**Parties are resolved in `Ingest`, not `Match`.** They cannot be in `Match`:
`facts_and_claims.agreement_id` is `NOT NULL` and an agreement *is* a normalised
party set, so the agreement has to exist before the first fact is written —
and `Match` reads facts. `Ingest` finds the preamble, runs GLiNER over it, has
the model pick indices into those spans, and upserts the agreement.

**`Match` and `Classify` DO split, and it is worth being clear why when
`Extract`/`Ground` did not.** A split is possible exactly where the second node
can read what the first wrote *from the database*: `Match` writes `claim_slots`
and `facts.value_group`, and `Classify` reads them. `Extract`'s candidates had
nowhere to land short of a table of unverified claims. The rule is not "fewer
nodes" — it is that nothing crosses a node boundary except through Postgres.

**A slot is keyed by a taxonomy field OR a free subject.** `claim_slots.field_key`
is nullable beside `subject_free`, the two mutually exclusive by CHECK
(`002_schema.sql`). Without it a term the 28 keys cannot express — the answer key's data
residency clause — has a fact and no slot, so the register never renders it and
the classifier never compares it. Net 2 matches those by embedding, because one
contract's "data residency region" is another's "data storage location".

### 2.5a An approval is not applied until a slot changes

`pending_decisions.chosen_value_group` records which value a reviewer picked.
For a long time nothing read it: the slot stayed `disputed`, `Slot.current`
stayed `None`, and the register kept rendering `⚠ sources disagree — awaiting
review` — while the commit that followed wrote that same warning back into the
document. The approval was real and changed nothing.

`Commit` now resolves before it renders:

```
approved_undecided → apply the choice → read the slots → render → compare → write
```

Applying means demoting the groups nobody chose to `role = 'superseded'` and
marking the slot `resolved` with the winner as `current_fact_id`. The demotion
is what makes the register change: `SLOT_ROWS` joins only live, non-superseded
facts, so the chosen group becomes the slot's only value and `Slot.current`
returns it. Marking the state alone would leave `current` pointing at group 0 —
publishing the value the reviewer *rejected* under a resolved heading, which is
worse than doing nothing.

The order is the property, and a test reads `Commit.run` to enforce both halves:
resolving after the read would render the previous values.

**A conflict needs an explicit choice; an amendment does not.** For an amendment
"the later document supersedes" is mechanical, and the classifier already worked
out which group that is to reach its verdict — it stores it as
`payload.recommended_value_group`. That is what keeps the batch lane honest:
approving twelve amendments at once agrees to a rule, not to twelve unseen
numbers. A conflict approved with no choice is refused by the API (`422`, with
the options listed), because there is no defensible default and picking a side
for the reviewer is the one thing this system exists not to do.

### 2.5b Classify: conflict or amendment

One value group settles the slot. Two or more go down a ladder, cheapest test
first, and the first tier that can answer does:

```
tier 0  every quote in one paragraph of one document?  → conflict, relationship same_passage
3+ groups                                               → _chain (below)
tier 1  both scalar (already known to differ)           → _amendment_across, detected_by rule
tier 2  local NLI: no contradiction                     → agree
tier 3  the judge                                       → _amendment_across, detected_by llm
```

**`_amendment_across`** asks whether the later value replaces the earlier one.
A value can be stated by several documents, so it shows the judge each document
stating the later value, newest first, up to `AMENDMENT_CANDIDATES = 3`, against
the earliest statement of the earlier value; the first verified amendment wins.
Each side is shown with the paragraph its quote sits in (bounded by blank lines,
capped at `CONTEXT_CHARS = 1000`), because the words that make it an amendment
usually sit just before the value. An `amendment` verdict holds only if its
`evidence_quote` is verbatim and found exactly once — first inside the paragraph
the judge was shown, then in the whole later document. Anything else, including
an `LLMError`, is a conflict: the safe failure asks a person.

**`_chain`** handles three or more values. Groups are put in arrival order and
every neighbouring pair must be a verified amendment; then the last value wins
and the item can take the fast lane. One failed step — or an older value
reappearing in a later document — makes it a conflict. A 3+ amendment verdict
not produced by `_chain` is downgraded to a conflict with no recommended group.

Adjudication uses `ctx.judge` (`LLM_MODEL_JUDGE`), a client separate from the
extraction client `ctx.llm` unless both resolve to the same model.

### 2.6 The gate: review everything, then commit once

> **Corrected.** This section described a loop that commits on *each* approval
> and returns to the gate. The graph does not do that, and building it would
> have been wasted work — the gate re-interrupts while **anything** is still
> pending (`human_gate.py`), so a resume in the middle of a review pauses again
> immediately. There is no `Commit → HumanGate` edge.

```
Audit(1) → HumanGate ──queue empty──► Commit → Reconcile → Audit(2) → END
               ▲                   │
               └── still pending? ──┘   (re-interrupt; the worker walks away)
```

A run pauses until the queue is **empty**, then commits everything at once. The
worker's resume sweep is keyed on exactly that condition, and rejecting every
item still resumes — a reviewer who declines everything has finished reviewing,
and the run has to reach the end or the pile waits for ever.

**Coalescing is therefore the normal case rather than a lucky one.** Five
decisions become one commit: one transaction, one version bump, one file write,
and one `deliverable_commits` row naming all five. Approving takes as long as it
takes, and the document changes once at the end of it.

**All items are visible at once.** One gate stop shows the whole queue, decided
in any order. Rejecting item A never affects item B — each is its own row.

### 2.7 The deliverable is a `.docx` in the pile's own folder — rendered by us

The final document is `final-document.docx`, written with python-docx into the
folder it was built from: `<WATCH_ROOT>/<pile>/final-document.docx`. The name is
`deliverable_filename` in `config/runtime.yaml`. It sits next to the sources, so
whoever dropped the contracts in finds the result beside them without opening
the dashboard.

Writing into a watched folder has one obvious hazard: the scanner would ingest
the system's own output as a new contract. `scan_pile(..., skip_root_names=...)`
in `worker/scanner.py` skips **exactly** that name at the pile **root** — a
same-named file in a subfolder is still somebody's source — and the temp file
the write goes through is a dotfile, which the scanner ignores anyway.

#### The unit is the section

`deliverable.py` + `register/render.py` turn the slots into sections: one per
register row, carrying the agreement heading, the row heading and the body text.
In the file that is a `Heading 1` per agreement and a `Heading 2` + paragraph per
row — headings rather than a table, because a section is what the system tracks,
hashes and proves unchanged, and a table would make the whole register one unit.

Each section has a SHA-256 over exactly those three strings (JSON-encoded first,
so `("a b", "c")` and `("a", "b c")` cannot collide), and
`piles.deliverable_sections` records every section as last written —
`{slot_id, agreement, heading, body, sha256}` — with `piles.deliverable_sha256`
hashing the ordered section hashes. Rendering is deterministic: the same slots
always produce the same sections in the same order.

#### One transaction, fenced on the lease

`Commit` does everything below in one transaction, every write carrying
`lease_epoch`:

```
apply approved decisions → render sections → compare hashes with the record
  → changed?  write the file (temp + fsync + os.replace)
              bump piles.deliverable_version, record sections/sha/path
              one deliverable_versions row per CHANGED section
  → one deliverable_commits row (decision_ids, slot_ids, version, sha, epoch)
```

The write is a dotfile temp `.final-document.docx.tmp` in the same directory,
fsynced, then `os.replace`d over the real name, so a reader sees the old file or
the new one, never half of one.

`deliverable_commits` is how `decisions.approved_undecided` knows a decision has
already reached the document: "approved but not yet committed" is a
`NOT EXISTS` against it, so an approval can never be applied twice.

#### A focused update, provably

A new source that moves one term changes one section's hash. That section gets a
`deliverable_versions` row — `operation` `create`/`edit`/`delete`, before and
after text, before and after hash, a `reason` and the `decision_id` that
authorised it — and every other section gets nothing. Its hash in
`piles.deliverable_sections` is byte-identical to the one recorded before, and
that is the proof it was not touched. `stage.commit.applied` names the changed
rows and counts the unchanged ones.

`reason` is `initial` on a first write, `new_field` for a row that appeared
later, `source_update` when a source changed, `orphan_flag` when the removal
marker was added — or the decision kind (`conflict`, `amendment`,
`label_change`, `orphaned_claims`) when a human's approval caused the change.

A commit that renders exactly what is already recorded writes nothing: no file,
no version bump.

#### Crash safety, and the file being changed behind our back

The file is written inside the transaction, before it commits. Killed before the
commit, the record rolls back, the decisions are still approved-but-uncommitted,
and the next run renders the same sections — rendering is deterministic — and
records them once. Killed after, the record and the file agree.

`Resolve`, first in every run, covers the remaining gap: it reads the file back
and compares it to `piles.deliverable_sections`. Missing, unreadable or
different, it rewrites the file **from the record**, not from the current slots
— the record is what a human last approved, and the slots may hold changes
nobody has reviewed. The version does not move; `stage.resolve.done` says
`repaired: true`. A hand edit to the `.docx` is overwritten the same way: a row
whose text differs from the record is a claim no source and no reviewer
supports.

`Reconcile`, after `Commit`, does the same read-back and treats a mismatch as a
finding rather than a repair: an `unverified_document` review item, and the run
goes back to the gate instead of ending as though all were well. "Committed"
means the bytes on disk say what we recorded.

#### The dashboard shows the register, and links the file

The register pane renders `GET /piles/{id}/deliverable` — our slots, set as a
document — and the file is a plain download link (`DocumentDownload.tsx`). There
is nothing to request and nothing to wait for: the worker writes the file after
review, so it is either there or not yet.

Because the register is our render, **provenance works properly**. We own the
DOM, so *"clicking a claim in the report immediately highlights the source
sentence proving it"* is a plain click handler:

```
┌─ LEFT ────────────────────────┐   ┌─ RIGHT ────────────────┐
│  The register                 │   │  Source contract       │
│  our render of the slots      │   │  our render, offsets   │
│                               │   │                        │
│                               │   │  highlighted span ◄────┼── click a
│  click a value ───────────────┼───┼──► scrolls + highlights│   claim
└───────────────────────────────┘   └────────────────────────┘
```

The mapping is ours end to end: a claim carries `document_id` and
`char_start/char_end` into `documents.extracted_text`.

#### Renames, and why `label_override` exists

An approved `label_change` sets `claim_slots.label_override`, and the section is
re-rendered under the new heading. Sections are keyed by slot id, so a renamed
heading is an **edit of the same section**, not a removal plus an addition.

**`label_override` is what makes the rename durable, and it is easy to miss
why.** Register content is generated from the taxonomy, so without the column the
*next* commit would render the field under its taxonomy label again and silently
undo the rename. Every later render reads it.

The preamble at the top of the document says what the document is:

> This document is generated and kept up to date automatically from the source
> documents in this folder. Every value is maintained from source, and changes
> are applied only after human review.

**Seven decision kinds**: `conflict`, `amendment`, `finding`,
`orphaned_claims`, `unverified_document`, `unassigned_agreement`,
`label_change`.

#### The five ways the deliverable changes

Renaming is only one of them, and the gating rule is not "everything is gated".

| Mutation | Op | Gated? | Why |
|---|---|---|---|
| **New field discovered** | `create` | **No** | A field with one Gate-1-verified value is *reporting what a source says*, with its citation. Gating each would mean twenty approvals before a reviewer sees anything on a pile's first build |
| **Source deleted** → annotate the row | `edit` | **No** | Flagging is honesty, not a claim — the same reason a disputed slot is written before you decide |
| **Conflict resolved / amendment** | `edit` | **Yes** | Picking a winner is a judgement |
| **Rename** (`label_change`) | `edit` | **Yes** | Human-initiated by definition |
| **Remove an orphaned row** | `delete` | **Yes** | Dropping a grounded claim someone may already have acted on |

The line is consistent throughout: **flagging is ungated, choosing and deleting
are gated.** A register that waited for permission before saying "these two
contracts disagree" would keep showing one number with no warning, which is the
failure mode the whole design exists to avoid.

The removal marker is **appended** to the value, never substituted for it
(`render.ORPHANED`): the value was verified against a real document, and a
reader who saw it yesterday may have acted on it. Removing the row is a separate
`orphaned_claims` decision, raised once per slot.

### 2.8 The post-commit re-audit, and why it terminates

Approving the price amendment (10k → 12k) makes R003 fail: the 120,000 cap no
longer covers 12 × 12,000 = 144,000. That violation is *created by the approval*,
so an audit that only ran before the gate would never see it.

```
Audit(pass 1) → HumanGate → Commit → Reconcile → Audit(pass 2) → END
```

`Reconcile` runs once, after the commit. Once the file reads back as recorded,
it sets `audit_pass = 2` and routes back to `Audit`. On pass 2, `Audit` writes any new findings as review
items and then **ends the run** — it does not route to `HumanGate → Commit`
again.

That is what stops the cycle. Findings raised on pass 2 are waiting in the queue
when the human next looks, and they are handled by the *next* run. Without that
rule, a fix that creates a finding that creates a fix could loop forever.

---

## 3. The API

Async throughout. Every route reads the database; the deliverable routes also
read the final document off disk (§3.4).

### 3.0 It runs the worker's queries, not copies of them

Every route reads through `documents_analyst.db.repo.*` — the same functions the graph
nodes call, fences and all. Those are synchronous and this process is async,
which usually ends with a second async copy of every query, and that is exactly
the arrangement where a fenced write and its unfenced twin drift apart with only
one of them under test.

`AsyncConnection.run_sync` removes the choice: a sync repo function runs on the
async connection unchanged.

```python
rows = await run(conn, pile_repo.summaries)
```

`test_no_route_writes_its_own_sql` tokenises every module under `api/` and fails
on any that builds SQL, so this stays a property rather than a habit. The one
`NOTIFY` a handler sends lives in `pile_repo.notify_work`.

### 3.1 Routes

| Route | Purpose |
|---|---|
| `GET /piles` · `GET /piles/{id}` | Navigator, status dot, per-pile scoping |
| `POST /piles/{id}/documents` | Upload → write the file, `NOTIFY`. **No row** — see below |
| `GET /piles/{id}/documents` | The document list, without every document's text |
| `GET /piles/{id}/deliverable` | The register, with slot states and citations, plus `document: {filename, written, sha256, download}` |
| `GET /piles/{id}/deliverable/document` | The `.docx` itself (§3.4) |
| `GET /piles/{id}/deliverable/verify` | Read the file back and compare it to the record |
| `GET /piles/{id}/deliverable/history` | The `deliverable_versions` rows: what changed, when, on whose decision |
| `GET /documents/{id}/text` | `extracted_text` for the source pane |
| `GET /piles/{id}/decisions` | The review queue |
| `POST /decisions/{id}/approve` · `/reject` | One item. An approval whose evidence no longer matches the live facts (`evidence_current`) is withdrawn and re-asked (`send_back_for_recheck`: `superseded`, `run.sources_changed`, `NOTIFY`) and answered `409` — returned as a `JSONResponse`, not raised, so the withdrawal is not rolled back |
| `POST /piles/{id}/decisions/approve-batch` | The amendment fast lane |
| `GET /piles/{id}/citations` | The claim → source map: field, value, citation, `document_id`, `char_start/end`. Powers click-a-claim-to-highlight; no longer a separate toggle |
| `GET /piles/{id}/findings` | Standing rulebook verdicts |
| `GET /piles/{id}/events` | SSE — live pipeline |
| `POST /piles/{id}/retry` | Re-queue an `error` (or legacy `parked`) pile, or an idle one with files quarantined by failed extraction; attempts reset. `409` otherwise. Pile summaries carry a `retryable` count |
| `GET /piles/{id}/cost` | LLM spend, tokens and latency by stage, plus an `unpriced` list (models with no price; their calls count 0, never "free") |
| `GET /piles/{id}/taxonomy-gaps` | Recurring `subject_free` concepts |

#### Upload writes a file and deliberately no document row

> **Corrected in Phase 5.** This table used to read "write file, insert row,
> `NOTIFY`", and the middle step is the problem.

Inserting the row means the API also decides the sha256, whether that hash is
already in the pile, whether the *text* hash makes this an alias of a document
we have already read, whether the parse failed and it should be quarantined with
a reason, and whether it supersedes something at the same path. All of that
exists, tested, in `worker/ingest.py`. A second implementation of those
decisions inside a request handler only has to disagree once to put a duplicate
contract in somebody's register.

So the API does the part only it can do — take the bytes, write them into the
pile's folder, say "look now" — and discovery does the rest, exactly as it does
for a file dragged in with Finder. **There is one ingestion path.** The cost is
honest and small: `202 Accepted` with no document id, because the document does
not exist yet.

#### Who is asking, and may they

Two separate questions, answered separately.

| | What it is | Required |
|---|---|---|
| `X-Actor` | A label the caller chooses, like a git author | On every **decision**, because `decided_by` is stored |
| `API_TOKEN` | One shared secret, no user model | Only if the deployment sets it |

`decided_by` is the only authority `Commit` has for editing the final
document, so a nameless approval is refused rather than filled in with `"api"` —
a placeholder in an audit trail is worse than a gap, because it looks like an
answer. Upload does not ask for a name: no column records who dropped a file in,
and demanding a header we discard teaches people the header does not matter.

`API_TOKEN` is blank by default, because a self-hosted tool that demands a credential
before it shows you anything gets closed. When it is set, every route needs
`Authorization: Bearer …` except `/` and `/health` — the landing page's job is
naming what is misconfigured, and a health check behind a credential cannot be
used by whatever restarts the container. `/health` also carries a `watch`
block — `path`, `deliverable_filename`, `pile_concurrency`, `scan_interval_s` —
which is what the dashboard's setup guide reads, and `pricing.warnings` from
`pricing.price_warnings()`: models with no price, or a price past or within 14
days of its `valid_until`. Prices never block a call. `path` is `watch_dir_host` when
Compose set it from `WATCH_DIR`, so the guide shows the folder as the person sees
it rather than `/data/watched` inside the container; otherwise `watch_root`.
(Phase 7 will fetch the stream rather
than use `EventSource`, which cannot send a header; a token in a query string
would land in every access log.)

### 3.2 SSE — one listener, many subscribers

Not one Postgres connection per browser. One background task holds a single
`LISTEN`, and fans out to per-client `asyncio.Queue`s.

```
worker → INSERT stage_events → trigger → NOTIFY
                                            ↓
                    api: one LISTEN task → wake → each browser re-reads
```

**The notification is a hint, not the data.** It carries ids and a type and
never `payload`, which is unbounded — pushing `pg_notify` past 8000 bytes raises,
and that error would abort the transaction *writing the event*, breaking the
pipeline from the observability layer. A woken stream re-reads `stage_events`
from its cursor, which is what a reconnecting one does anyway.

Three things fall out of that:

- **Replay and live delivery are one code path.** A reconnect is not a special
  case; it is a stream whose cursor started further back.
- **A lost, duplicated or coalesced notification costs a query, not an event.**
  Which matters, because notifications *are* lost — the LISTEN connection drops
  and everything published meanwhile is gone. On reconnect the broker wakes
  every subscriber rather than working out what it missed.
- **A slow client cannot grow memory.** Its queue holds a single "look again"
  token, so a subscriber behind by four hundred events and one behind by one are
  indistinguishable. Backpressure with no bookkeeping.

The `NOTIFY` is issued by a trigger rather than by our code because
`stage_events` is written from eleven places, and the one that forgets is
silent: the row lands, the timeline is right, and only the live stream is
missing an event.

### 3.3 Reconnect replays, it does not skip

The client sends `Last-Event-ID`. The route reads the missed rows from
`stage_events`, sends them, *then* waits for new ones — subscribing **before**
the first read, so an event published between the query and the subscription is
not in neither.

This is exactly why `stage_events` is a table and not a log file — a browser
that reconnects mid-run asks "everything after 42", which is a query.

**The cursor is `stage_events.id`, not `seq`**, and an earlier draft of this
section said `seq`. `seq` is allocated per run and restarts at 1, so a browser
that saw seq 12 of one run and reconnected during the next would ask for
"everything after 12" and be handed nothing until the new run passed twelve
events — silently losing the eleven that started it. `id` is one BIGSERIAL over
the whole table, which is what a stream position has to be. `seq` stays in the
payload: it is an ordering *within* a run, and it is what makes one run's
timeline readable.

### 3.4 The API reads the document; it never writes it

No route writes `final-document.docx`. Writing belongs to the worker, under the
pile's lease, in the same transaction that records it (§2.7). The API only reads:

- `GET /piles/{id}/deliverable/document` streams the file. `409` before the
  first commit — there is nothing to download yet — and `410` if the record says
  a document exists but the file is not on disk (the next run's `Resolve`
  restores it).
- `GET /piles/{id}/deliverable/verify` runs the same read-back `Reconcile` runs,
  on demand, via `asyncio.to_thread` because python-docx parsing is synchronous.

**The file is found by pile name, never by the stored path.**
`deliverable.path_for` joins the pile's name to *this process's own*
`watch_root` and checks the result is still inside it. Two reasons: the worker
in a container writes `/data/watched/acme/…` while an API on the host reads
`./watched/acme/…` (or `<WATCH_DIR>/acme/…`) — the same file under two paths — and a stored path is data,
which should not decide a filesystem read.

---

## 4. Data layer

```
db/tables.py     Core Table objects on one shared MetaData
db/rows.py       frozen dataclasses returned by repos
db/repo/*.py     one module per aggregate; every function takes a connection
db/migrate.py    ~40-line runner over db/migrations/*.sql
```

Repos take a connection rather than opening one, so the **caller owns the
transaction boundary** — a request for the API, a short write window for a graph
node.

Two engines, one driver:

```python
# api      → create_async_engine("postgresql+psycopg://…")
# worker   → create_engine("postgresql+psycopg://…")
```

`psycopg3` because LangGraph's checkpointer uses it. Adding asyncpg would mean
two drivers and two pools in one process.

**The drift test.** Migrations are raw SQL, `tables.py` is hand-written — nothing
generates one from the other. `tests/test_schema_drift.py` applies every
migration to a scratch database, reflects it, and asserts it matches the
metadata. A silent runtime bug becomes a red test.

### 4.1 Schema notes that prevent silent failures

Listed here because each one prevents a failure nobody would see until a
register was wrong.

| Table | Column | The failure it prevents |
|---|---|---|
| `piles` | **`deliverable_sections`**, `deliverable_sha256`, `deliverable_version`, `deliverable_path` | Without the recorded sections there is nothing to compare a fresh render against, so neither "only this section changed" nor "the file on disk says what we wrote" could be checked — only asserted |
| `claim_slots` | **`label_override`** | Register content is generated from the taxonomy, so a rename that only reached the document would be undone by the next commit |
| `deliverable_commits` | `decision_ids` | "Approved but not yet in the document" is a `NOT EXISTS` against it; without it an approval could be applied twice |

`pending_decisions.kind` has **seven** values, `label_change` and
`unverified_document` among them.

**14 tables** in all (13 in `002_schema` plus `schema_migrations`), in four
migration files: extensions, the schema, the taxonomy seed and the indexes.

---

## 5. MCP server

A **separate process**, and an HTTP client of our own API.

That is deliberate. If it imported the handlers directly, "machine callers go
through the same gates as the browser" would be a convention. As an HTTP client
it is structural — there is no other path available to it, and
`test_the_server_reaches_the_system_only_over_http` walks the package's imports
and fails on any route into the database. The compose service depends on `api`
rather than `db`, and has no credentials for one.

Both transports, and **the transport is in the command, not the environment**:

| Service | Profile | Transport | Reachable from |
|---|---|---|---|
| `mcp` | `mcp` | stdio | the process that started it |
| `mcp-http` | `mcp-http` | streamable-http | `127.0.0.1:8765` only |

`docker compose run --rm -T mcp` is what `claude mcp add` runs. It used to
inherit `MCP_TRANSPORT=streamable-http` from the service environment, so the
documented command started an HTTP listener while the client waited for JSON-RPC
on a stdout that never spoke — a hang, not an error, which is the hardest kind
of wrong to find. Passing `--transport stdio` in the command makes the service
say what it is.

#### The HTTP transport needs its own credential

This process holds `API_TOKEN` and presents it on every downstream call, so an
unauthenticated port here is a **confused deputy**: anyone who reaches it can
approve register changes using our credential, and the API's guard never sees
them. Locking the API and leaving this open is worse than leaving both open,
because it looks protected.

So `MCP_TOKEN` is what the server *demands* of a caller, deliberately separate
from `API_TOKEN`, which is what it *presents* downstream — one string for both
would hand every client the API's credential in the act of checking it. `MCP_HOST`
defaults to loopback, the compose service publishes to `127.0.0.1` only, and the
server **refuses to start** on a non-loopback bind with an `API_TOKEN` and no
`MCP_TOKEN`. An open API behind an open port is allowed: the port grants nothing
the API does not already grant to anyone.

### 5.1 The seven tools

| Tool | | Composes |
|---|---|---|
| `list_piles` | read | `GET /piles` |
| `get_register` | read | the register with each term's state and competing values |
| `get_review_queue` | read | the queue **with each item's evidence** |
| `get_citations` | read | claim → the exact source span proving it |
| `approve_item` | write | re-read the item, check the confirmation, then `POST …/approve` |
| `reject_item` | write | re-read, then `POST …/reject` |
| `get_final_document` | read | `GET …/deliverable/verify`: version, SHA-256, verified, problems, plus how to fetch the file |

`read_only_hint` is set on the five reads, so a host can offer them freely and
confirm the rest. They are **composed, not mirrored**: `get_review_queue`
returns the evidence inline precisely because `approve_item` demands it back,
and a queue that made an agent fetch it separately is a queue it approves
without reading.

It also returns `options` — each competing value with the `value_group` an
approval names, and the documents that say it. Without them an agent can see
that two values disagree and cannot tell which number is group 0, so choosing
between them is a coin flip that writes into somebody's register. The numbers
come straight from the decision's payload, which the classifier wrote from the
facts, so what an agent reads and what an approval selects cannot drift.

### 5.2 Approving requires quoting the evidence

`approve_item(decision_id, confirm)` re-reads the item and requires `confirm` to
appear **verbatim** in its `evidence_quote` — or, for a rulebook finding, which
has no quote because the violation is an *absent* clause, its recommendation.
Whitespace is normalised, because a quote copied out of a tool result may have
been re-wrapped; nothing else is. It is the Gate 1 rule one level up: exact
substring, no fuzzy matching, no threshold, no model in the loop.

An agent looping over ids cannot pass it. One that read the conflict can.

**Its honest limit:** the refusal returns the real evidence, so a caller can
fail once and retry with the right text. That is the mechanism working rather
than a hole — the point is that the evidence is in the caller's context before
an approval lands, and after that retry it is. Closing the "loophole" would mean
refusing to say why, which makes the tool unusable to an honest caller and
barely inconveniences a dishonest one.

`reject_item` requires no confirmation. Rejecting commits nothing, and putting
the same friction on the safe direction only teaches callers to approve.

### 5.3 A machine's name is always a machine's

`MCP_ACTOR` lands in `pending_decisions.decided_by`, and the server forces an
`mcp:` prefix if it is missing. An operator setting `MCP_ACTOR=devansh` means
"the agent I run"; taken literally it would put approvals in the audit trail
that are indistinguishable from ones a person made in the UI, and telling those
apart is the entire reason that column is not just "someone".

The actor is set in the client, not taken as a tool argument, so no call can
dress a machine decision up as a person's.

#### `get_final_document` reads the document; it never produces one

The document is written by the pipeline after review, not on request, so the
tool only reads: it returns the version, the document SHA-256, `verified` and
any `problems` from reading the file back, or `ready: false` before the first
commit. Nothing about it is a write, and it is annotated read-only.

The file itself comes back two ways. A `document://{pile_id}` **resource URI**,
read over the MCP session — the only path that works when `API_TOKEN` is set,
because the caller holds `MCP_TOKEN` and not the API's credential. The tool
advertises the URI in its result, since a client cannot read one nobody
mentioned. And a plain `download_url`
(`GET /piles/{id}/deliverable/document`), built from `PUBLIC_API_BASE_URL`
rather than the address this process dials — in compose it reaches the API at
`http://api:8000`, a name that resolves only inside Docker's network, and
handing that to a host-side agent advertises a download it cannot follow.

---

## 6. Settings

One frozen `Settings` in `settings.py`, loaded once via `pydantic-settings`.
**Nothing else in the codebase reads `os.environ`.**

With `LLM_PROVIDER` blank, `_pick_providers` chooses Anthropic when its key is
present, otherwise Gemini when its key is present, otherwise OpenAI.
`EMBEDDING_PROVIDER` is independent:
blank chooses OpenAI when its key is present, otherwise Gemini when its key is
present. With neither key, OpenAI is the default and the requirement stays unmet.
Explicit provider settings require the corresponding key. Claude needs an OpenAI or
Gemini embedding key in addition to its own key.

`key_checklist()` returns the two capability requirements used by
`GET /health`: acceptable keys and presence flags, `met`, `using`, `chosen_by`,
and model names. `setup.ready` is true only when both are met. The frontend's
`SetupGate` blocks the workspace and polls health every four seconds until it
is ready; the API continues serving reads and the worker continues ingesting.
Changes to `.env` require `make up` to reload the container environment.

`models.load_models` injects `embedding_api_key`, `embedding_provider` and
`resolved_embedding_model` into the core loader. OpenAI and Gemini both return
384-dimensional vectors; Gemini normalises vectors and estimates token usage.
The worker does not start graph work while either key requirement is missing.

Core takes injected values, never a `Settings` object — that is what keeps
`portable/` copyable and core testable with no `.env` present.

```python
llm = build_client(
    provider=settings.llm_provider,
    api_key=settings.llm_api_key,
    model=settings.resolved_model_extract,
    max_retries=settings.llm_max_retries,
)
```

---

## See also

[`TESTING.md`](TESTING.md) · [`BUILD_AND_RUNTIME.md`](BUILD_AND_RUNTIME.md)


## Reliability changes after the user journey audit

Extraction now reports section failures explicitly. Taxonomy and open-subject
results are persisted together only when both passes complete; an unfinished
document counts an attempt and is quarantined after `job_max_attempts` (§2.5).
Classification and publication wait for all extraction batches. Three or more
value groups settle only as a verified amendment chain (§2.5b).

Approvals are checked against live source identities (`evidence_current`) at the
API and inside the fenced commit transaction. A source change runs
`invalidate_review`, which withdraws (`superseded`, author kept) only the
uncommitted conflict/amendment items whose evidence changed; an identical copy
(`aliased`) triggers nothing, and an added, superseded, restored or removed file
sends an `awaiting_review` pile back to `idle` so the change joins the same
review. Stale approvals are never recorded as applied.

There is no spending limit. Each run's closing event (`run.finished`,
`run.paused`, `run.incomplete`) carries `cost_usd`, `calls` and, when a model has
no price, `unpriced_calls` / `unpriced_models` (`telemetry.totals_for_run`).

DOCX sections include verbatim source passages, relative paths and offsets. A
hashed coverage section lists sources and unavailable content. The staged file
is verified before its atomic rename; section hashes then support verification,
history and repair. Source inventory changes are graph work even when no new
original needs extraction. See `run.sources_changed` / `run.coverage_updated`.
