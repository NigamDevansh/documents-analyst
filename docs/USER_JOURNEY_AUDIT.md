# User journey audit — 22 November 2026

## Second review follow-up

A second review of the fixes below, plus a live end-to-end run, found that some
of them introduced new failure modes. These changes supersede the corresponding
rows of the next section.

| Area | Change |
|---|---|
| Spending | The per-run dollar limit and its reservations are removed at the owner's request. They parked the demo pile out of the box: about $8 reserved against $5 on the default `gpt-4o`. Every call runs. Each run's cost is on its closing timeline event, and the pile total is on the Cost tab. Missing prices are reported as unpriced, never as free. |
| Price expiry | An expired `valid_until` no longer blocks calls. It is flagged in `/health`, the worker log and the Cost tab from 14 days before the date, and the last price stays in use. |
| Failing sections | An incomplete extraction now counts as an attempt. After `job_max_attempts` the file is quarantined with its reason, and the pile proceeds. Before, it was re-billed on every scan and never reached review. Retry re-queues such files. |
| 3+ values | Same-paragraph detection runs first again. Otherwise the values are ordered by arrival and every neighbouring pair is checked. A fully verified chain is one amendment settling on the latest value; any unproven step, or an older value reappearing later, is a conflict. |
| Stale review items | An approval on changed evidence withdraws the item and re-asks it, instead of returning 409 for ever. A source change withdraws only the items whose cited facts changed; an identical copy withdraws nothing. |
| Scheduling | A wake that arrives during a drain starts the next drain as soon as it ends. |
| Scanner | `Thumbs.db`, `desktop.ini` and macOS `Icon\r` are ignored, not quarantined. `rulebook.yml` and similar near-misses are skipped, with a log line naming the expected `rulebook.yaml`. |
| Open subjects | An open subject folds into a typed field only if its value parses as that type. "Monthly fee", "Subscription fee" and similar names fold into Recurring price, which removes the duplicate price rows in the final document. |
| Dashboard | The phone-only "Piles" button no longer breaks the desktop grid (a same-specificity `.btn` rule overrode its hide rule). The timeline names `run.sources_changed` and `run.coverage_updated` in plain words. |

Verified live: closing the tab mid-extraction, reopening it, restarting the API with
the tab open, reloading mid-review, closing the tab right after the last approval, and
hard-killing the worker mid-extraction. The worker continued each time. The timeline
replayed in full with no duplicates. Finished documents were not extracted again.

## Fix and verification follow-up

The original audit below is retained as the reproduction record. Runtime fixes
now address findings 1–8 and 10–16, plus all three accessibility findings.
Finding 9 is intentionally excluded at the user's request: the added browser
token/sign-in experiment was removed. The dashboard opens directly. The
pre-existing optional HTTP bearer guard remains for integrations; leave
`API_TOKEN` blank for dashboard use.

| Area | Result after fixes |
|---|---|
| Source history and duplicates | A→B→A restores the correct facts; aliases survive original replacement/removal without duplicating facts. Complete scans prune vanished copy paths; incomplete scans preserve them. |
| Extraction and disagreements | Failed sections keep the whole document queued, and publication waits for all pending documents. Three distinct values cannot settle from comparing just two. |
| Review integrity | API and fenced commit validate current source evidence. Stale choices reopen review; only applied decisions become committed. |
| Limits and providers | Document batches are bounded. Durable cost reservations cover each model/embedding attempt and survive crash/review resumes. Missing, invalid or expired prices block calls. Extraction and judge models are separate. Explicit retry is available after fixing configuration. |
| Dashboard | Pile and citation request identities prevent late-response mixups. Unicode highlights use code points. Mobile navigation/source/download layouts and keyboard/modal/reduced-motion behavior were fixed. |
| Uploads and scheduling | Unsupported uploads return 415; dropped unsupported files are quarantined and listed. Discovery/repair runs independently of long graph work. |
| Test isolation and CI | Unique scratch database names isolate concurrent suites. Tests have a separate Compose profile. A GitHub Actions workflow now runs frontend/browser and full container checks; remote CI has not been invoked. |
| Final DOCX | Verbatim evidence, paths/offsets, reviewer identity, and a hashed source inventory travel with the output. Unreadable, removed, duplicate and textless sources are visible. Staged output is verified before atomic publication. The open-subject count cap was removed; known taxonomy names and narrowly declared aliases consolidate through the same grounding gates. |

The inventory is a snapshot of completed analysis. A valid quote proves its
source text; it cannot prove that a model identified every material term.
Human completeness review remains necessary. Consolidation applies to fresh
extraction; previously persisted duplicate open subjects are not automatically
migrated or re-extracted.

The follow-up exercised a fresh synthetic agreement and amendment with the real
OpenAI provider, approved its 45-day payment term through Chrome, resumed the
worker, downloaded the DOCX, checked every returned citation against source
offsets, and rendered the downloaded output for visual inspection. No decisions
in the original three piles were approved or rejected. Their versions remain
acme v2, acme-copy v1, and meridian-health v1.

Current verification evidence and cleanup status are recorded in the requested
checks table below. The historical findings' file/line references describe the
original checkout and may have moved.

## Original audit scope

Reviewed checkout `161bef6`, the running Docker stack, dashboard behavior in
Chrome, source ingestion, graph/review/commit behavior, provider setup,
documentation, and the automated suites. The application starts and its
existing tests pass, but the findings below affect data integrity and reliable
review. This paragraph and the findings below describe the state before fixes.

P1 means fix before relying on the affected workflow; P2 means a significant
functional limitation; P3 covers accessibility improvements. Reproductions
distinguish actual browser/database observations from findings established by
code inspection. The original passing tests did not cover the failures listed
here; regressions were added during the follow-up.

## P1 findings

### 1. Reverting a source file retains the newer source's terms

**Locations:** `backend/src/documents_analyst/worker/ingest.py:154`,
`backend/src/documents_analyst/db/repo/documents.py:71`.

Ingest a contract with 30-day payment terms, replace it with 45 days, then
restore the original 30-day file. The already-seen SHA branch restores only
documents marked `removed`, so it leaves the original `superseded` and returns.
The newer document still has a path that exists and remains active.

Reproduced through actual ingestion and graph processing in an isolated schema:
the file on disk says 30 days, but active facts still say 45. Text deduplication
also considers superseded originals, so a newly added format export can alias
to an inactive document. Reactivation must reconcile the document, its facts,
and the superseding version's surviving paths together.

### 2. One successful section hides failed extraction sections

**Location:** `backend/src/documents_analyst/graph/nodes/extract.py:114` and `:173`.

Completion is based on whether any section answered successfully. If one
section succeeds and another exhausts its retries, the document can be marked
`extracted`; the failed section is no longer queued. Terms disappear, and
absence rules can evaluate incompletely read text as if extraction were done.

Reproduced with two sections: the first returned an empty successful result,
the second raised an LLM error, and the backend completion predicate was true.
Track successful and failed sections separately and retry unfinished work.

### 3. A three-way disagreement can be settled after comparing two values

**Locations:** `backend/src/documents_analyst/graph/nodes/classify.py:190` and `:79`.

The prose adjudicator compares only the first two value groups. If it decides
they agree, the entire slot becomes settled even when a third value disagrees.
The existing multi-value safeguard covers amendment results only.

Reproduced using two clauses allowing assignment with consent and a third
prohibiting assignment entirely. Only the consent pair was examined; the
verdict was `agree`. Require agreement across every group, or retain a conflict
when any group remains unchecked.

### 4. An unapplied stale approval is recorded as committed

**Locations:** `backend/src/documents_analyst/graph/nodes/commit.py:379` and `:122`.

Change a source while a pile waits for review, then approve an option from the
saved review payload whose facts have become superseded. API choice validation
accepts the saved option. Commit cannot find a live winner and skips applying
it, but subsequently records all approvals as committed.

Reproduced against the real graph and repositories in an isolated schema:
zero live facts supported the choice; validation accepted it; the run finished;
the slot stayed disputed; the decision was recorded as committed. Revalidate
choices against current evidence and record only decisions actually applied.
Reopen stale choices for review.

### 5. Switching piles can show and download another pile's document

**Location:** `frontend/src/App.tsx:159`.

`loadPile` updates shared state without checking that its requested pile is
still selected. Previous register, queue, citations, and costs also remain
visible while the next request is pending or fails.

Reproduced in Chrome by delaying acme's responses, selecting acme-copy, and
letting the delayed responses finish. The heading and URL identified acme-copy,
but the document showed v2 and linked to acme's download endpoint. The same
unguarded setters affect review items, making decisions under the wrong pile
heading possible. Cancel obsolete requests or guard each response with the
current pile/request identity and clear previous-pile state on selection.

### 6. Configured budget and document limits are unenforced

**Locations:** `config/runtime.yaml:62`,
`backend/src/documents_analyst/worker/queue.py:100`.

`max_llm_cost_usd_per_run` and `max_documents_per_run` are declared in Settings
but never consumed by runtime code. Queue selection takes all eligible
documents, and model calls have no per-run spending cutoff. Users cannot rely
on the configured limits to bound processing or charges. Confirmed by tracing
all references and the queue/call paths; no charge-generating stress test was
performed. Enforce limits and explicitly handle models without configured rates.

## P2 findings

### 7. The judge-model setting does not change the model used

**Locations:** `backend/src/documents_analyst/worker/main.py:95`,
`backend/src/documents_analyst/graph/nodes/audit.py:216`.

The worker creates one LLM client with `resolved_model_extract`. Classification,
audit, and party selection use that same client. `resolved_model_judge` affects
reported setup configuration but has no corresponding runtime client.
Confirmed by tracing client construction and consumers. Inject separate
extraction and judging clients and test their actual model identities.

### 8. Unsupported uploads are accepted and then silently ignored

**Locations:** `backend/src/documents_analyst/api/routes/documents.py:54`,
`backend/src/documents_analyst/worker/scanner.py:132`.

The upload route accepts arbitrary extensions with HTTP 202, while the scanner
skips unsupported extensions without recording a quarantine. A user uploading
`.doc`, `.xlsx`, or `.rtf` receives apparent success and no subsequent result.
This also contradicts the README's promise that unsupported formats are
quarantined with a reason. Confirmed by following upload and discovery code.
Reject unsupported uploads immediately and surface unsupported dropped files.

### 9. Setting API_TOKEN makes the browser workspace unusable

**Locations:** `frontend/src/api/client.ts:31`,
`frontend/src/hooks/useEvents.ts:24`, `frontend/src/components/DocumentDownload.tsx:48`.

The dashboard has no token-entry mechanism, adds no Authorization header, uses
unauthenticated EventSource, and downloads through a plain link. The API's
optional token protection consequently closes every functional browser path.
Reproduced the browser's response to an injected 401: no token input or recovery
flow is offered. The running deployment has no API_TOKEN, so this was not a
protected live deployment test. Provide a complete browser authentication
flow, including streaming and downloads.

### 10. Citation offsets break after non-BMP Unicode characters

**Location:** `frontend/src/api/spans.ts:28`.

Backend offsets index Python Unicode code points; JavaScript `slice` indexes
UTF-16 code units. Emoji or other characters outside the BMP before a citation
shift the highlight. Reproduced using the actual transpiled `cut` function:
`😀 Net 30 days`, with Python offsets 2–13, highlights ` Net 30 day` instead of
`Net 30 days`. Translate offsets consistently and add a Unicode regression case.

### 11. Opting into live provider tests still blanks every key

**Location:** `tests/conftest.py:24`.

The session-wide credential-clearing fixture runs even with `RUN_LIVE_LLM=1`.
Its docstring claims an exception for live tests, but the implementation has
none. Reproduced with placeholder keys and the opt-in flag: all six tests skip
with “no key configured”. The normal offline suite remains appropriately
keyless, but the documented opt-in command cannot validate provider behavior.
Scope credential isolation so explicit live tests can use their supplied keys.
Live tests also hard-code model IDs instead of checking configured model choices.

### 12. The mobile workspace is clipped

**Location:** `frontend/src/styles/app.css:47`.

The shell always reserves 250 pixels for its sidebar and keeps a horizontal
register/source split. Chrome at 390×844 leaves a 140-pixel main workspace and
an approximately 51-pixel source pane. The download control extends to x=439,
past the viewport, while overflow is hidden. Reproduced with a browser viewport
and screenshot. Add a collapsible sidebar and a stacked or selectable source view.

### 13. Thirty-second discovery and recovery are not maintained under load

**Location:** `backend/src/documents_analyst/worker/main.py:447`.

Each pass discovers files, drains review resumes, then drains new graph runs.
Parallel graph draining waits for its work before another discovery pass.
A long extraction in another pile can therefore delay new-file discovery and
idle deliverable repair for minutes. This is established by scheduler code;
the live recovery check below was performed while the stack was idle.
Separate discovery/repair scheduling from long graph work, or qualify the
timing promise and display the delay.

### 14. Rapid citation clicks can pair the wrong document and highlight

**Location:** `frontend/src/App.tsx:239`.

`showCitation` stores the newest citation immediately, then accepts any pending
document-text response without checking which citation/document remains
selected. Click sources A and B rapidly and a slow A response can overwrite B's
document while the citation offsets still belong to B. Confirmed by code
inspection; the ordinary single-citation browser check passed. Guard or cancel
obsolete source requests and render only matching citation/document identities.

### 15. CI is documented but no workflow exists in the checkout

**Location:** `docs/TESTING.md`, section 8.

The documentation describes GitHub Actions running checks on pushes and pull
requests, but the checkout contains no tracked `.github` workflow. This audit
ran checks manually; an automatic CI guarantee cannot be established from the
repository. Supply and verify the workflow before relying on that guarantee.
External repository-level automation was not inspected.

### 16. Full-stack startup launches tests; concurrent suites can collide

**Locations:** `docker-compose.yml:173`, `tests/test_api.py:71`.

The `tests` service shares the `full` profile, so `make full-up` starts a test
run as part of application startup. The fixtures create and truncate scratch
databases, leaving the application database intact. However, those scratch
names are fixed (`analyst_api_test`, and other module-specific names), so
concurrent suites on one Postgres server can drop/truncate each other's test
databases. Confirmed by compose and fixture inspection; this audit ran one
suite at a time. Give tests their own profile and unique scratch names or a
separate server per suite.

## P3 accessibility findings

- `frontend/src/components/SetupGuide.tsx:128`: the welcome dialog declares
  itself modal but does not trap focus, make background controls inert, or
  implement Escape dismissal. Its initial focus is set, but keyboard focus can
  leave the dialog.
- `frontend/src/components/ReviewQueue.tsx:143`: custom `role="radio"` buttons
  lack expected arrow-key group navigation and roving focus.
- `frontend/src/components/SourcePane.tsx:55`,
  `frontend/src/styles/tokens.css:107`: reduced-motion CSS changes tokenized
  durations, but does not govern explicit Framer Motion animations or the
  hard-coded smooth citation scroll. Connect these to the user's motion setting.

These were identified by reviewing the UI against the
[Web Interface Guidelines](https://raw.githubusercontent.com/vercel-labs/web-interface-guidelines/main/command.md).

## Requested remaining checks

| Item | Result |
|---|---|
| Background rebuild | Complete. `make up` rebuilt and restarted the fixed stack. API, worker and database are healthy; migrations exited 0. |
| Delete and recover final document | Repeated on the rebuilt stack after saving a backup. acme-copy's DOCX returned after 2.28 seconds; all 97 sections verified and version remained 1. DOCX archive bytes can differ on regeneration; verification uses section hashes. |
| Dashboard with OpenAI configured | Real OpenAI extraction, synthetic amendment approval, worker resume, DOCX download and exact citation checks passed through Chrome with no page errors. The final fresh document has three sections, with no duplicate payment row. Its rendered page was inspected. All three original piles also opened, highlighted sources and downloaded verified DOCX files without changing their versions. |
| Full suite | Final rebuilt Docker image: 690 passed, six skipped, 15 dependency warnings. The six skips are opt-in live-provider tests. Frontend sources were mounted read-only, so the source-consistency test ran in this same suite. |
| Frontend and other checks | 14 frontend unit tests and six Playwright Chrome regressions pass; production build passes; Ruff and formatting pass; mypy passes over 113 source files. Fixture DOCX consistency passes; all 107 answer-key quotes match exactly. |
| README Keys section | Reviewed and retained the capability table. Corrected contradictory Quick start and no-key behavior, and documented independent embedding selection. The removed Gemini pricing/data-terms paragraph is absent; no unverified replacement pricing was added. |
| docs/*.md | Updated all seven existing Markdown docs for relevant setup/provider/embedding behavior, plus README and DEVELOPMENT. Added reliability, budget/retry, source-inventory, browser-regression and CI details where relevant. |
| Test container | Removed `documents-analyst-testdb` after the suite and isolated database reproductions. Live API, worker and database remain running with acme v2, acme-copy v1 and meridian-health v1. |

## Reproduction artifacts and limits

Temporary local artifacts are outside the repository:

- `/tmp/documents-analyst-review-repros.py`: three deterministic no-database reproductions.
- `/tmp/documents-analyst-db-review-repros.py`: actual graph/repository source-reversion
  and stale-approval reproductions; requires a separate disposable test database
  container to be recreated. The audit's scratch schema was cleaned up.
- `/tmp/documents-analyst-browser/flows.cjs`: Chrome workflow and delayed-response checks.
- `/tmp/documents-analyst-browser/mobile.png`: narrow-viewport evidence.

No original pending human decisions were approved or rejected. Follow-up paid
provider checks were limited to small synthetic inputs: four OpenAI/Gemini live
tests passed and two Anthropic tests skipped because no key was configured.
The fresh synthetic end-to-end workflow used OpenAI. A complete Gemini-only or
Claude deployment, sustained production-load timing, Windows setup and a
screen-reader session were not exercised. Busy-worker discovery/repair was
checked by a controlled concurrency regression. The runtime findings are
addressed as described in the follow-up, with the user-requested exclusion for
added browser authentication.
