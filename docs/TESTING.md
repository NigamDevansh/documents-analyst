# Testing

How the suite is built, what runs where, and why the offline guarantee is the
design constraint rather than a nice-to-have. This covers the backend, the
shared core and the dashboard — the backend tests in `tests/` (the ones marked
`db` need `DATABASE_URL`).

Companion to [`CODE_ARCHITECTURE.md`](CODE_ARCHITECTURE.md),
[`CORE_INTERNALS.md`](CORE_INTERNALS.md),
[`BACKEND_INTERNALS.md`](BACKEND_INTERNALS.md) and
[`WORKER_RUNTIME.md`](WORKER_RUNTIME.md).

---

## 1. The one rule

> **The whole suite runs with no API keys and no network.**

Not a convenience — it is how the system proves itself: offline tests checking
kill/resume, concurrency and the final document. It also shapes real design decisions:
Gate 1 is a string comparison rather than a model call precisely so that "never
bluffs" can be a test.

```bash
make test        # everything, empty .env
```

That runs the dedicated `tests` compose service, which is given only
`DATABASE_URL` and `WATCH_ROOT` — not the shell's or `.env`'s API
keys. So a green run proves the suite is genuinely offline, rather than quietly
borrowing credentials the `worker` service would have inherited.

Three things make that possible:

| | How |
|---|---|
| Models | Baked into the image at build time (`scripts/prefetch_models.py`). No download on first run |
| LLMs | A `FakeLLM` returning canned structured output |
| The final document | Written locally with python-docx into a `tmp_path` pile folder — nothing to fake |

---

## 2. Layers

```
tests/
├── conftest.py              credential blanking (§7) and a loud warning when
│                            DATABASE_URL is unset and the db half skipped
├── review_helpers.py        seeds review options backed by real source facts
├── test_<module>.py         one file per area — pure functions through to
│                            whole graph runs against fixture piles
├── test_commit.py           the final document end to end (§5)
├── test_audit_regressions.py  user-level regressions: complete extraction,
│                            quarantine, stale evidence, cost reporting
├── test_amendment_chain.py  three-plus values: chain vs conflict (no DB)
├── test_price_warnings.py   missing / expiring / expired price warnings
├── test_brand.py            the API's ICON_SVG equals frontend/src/assets/icon.svg
└── test_llm_live.py         opt-in, real providers — `live`, skipped by default
```

`pytest --collect-only` currently finds about 700 tests: roughly 280 marked
`db`, 2 marked `models`, 6 marked `live`. The DB-backed modules each build their
own engine fixture; `test_audit_regressions.py` reuses `test_commit.py`'s.

### The dashboard's own tests

```bash
make ui-test          # vitest, no browser, no server
```

The vitest suite (`frontend/src/api/*.test.ts`) covers `cut` and `lineOf`
(offsets → highlighted pieces, including Unicode code points), `readHash`, and
which SSE events force a refetch plus the event cursor (`refresh.ts`).

The Playwright spec (`frontend/e2e/workspace.spec.ts`) starts the Vite dev
server on `127.0.0.1:5174` and answers every API call from a `page.route`
fixture — no backend. It covers late pile and citation responses, phone and
desktop layout, uploading files into the open pile (`POST
/piles/{id}/documents`), welcome-dialog focus, keyboard review, and setup
unlocking. A separate live-stack browser check is still needed to verify real
ingestion, provider calls, review, and downloads.

```bash
cd frontend
npx playwright install chromium
npm run test:e2e
# or reuse a local Chrome:
PLAYWRIGHT_CHROME_EXECUTABLE=/path/to/chrome npm run test:e2e
```

`cut` earns its tests because the failure it prevents is silent: highlighting by
searching for the quote text would light up the *first* "Net 30" in a contract
that says it four times, and look completely deliberate doing it.

### Markers

| Marker | Meaning | Default |
|---|---|---|
| *(none)* | Pure, fast, no DB | runs |
| `db` | Needs Postgres | runs |
| `models` | Loads GLiNER / NLI | **runs** (skips if no model cache) |
| `live` | Needs a real external API key | **skipped** |

```bash
pytest                      # everything; live tests skip themselves
pytest -m "not models"      # fast inner loop (make test-fast)
pytest -m live              # only with RUN_LIVE_LLM=1 and a key
```

The `live` marker covers provider capability checks only
(`RUN_LIVE_LLM=1 uv run pytest -m live`). Nothing about the final document needs
one: it is written and read back locally.

**Model tests are on by default** because the models ship inside the image, so
they are always available. Making them opt-in would leave the integration code —
exactly the part most likely to break on a different machine — untested unless
somebody remembered a flag.

There are two of them, in `tests/test_models.py` (DeBERTa NLI and GLiNER). On a
host without a populated model cache or the `ml` extra they skip with the
command that fixes it rather than failing.

---

## 3. Database

Each DB-backed module derives a server connection from `DATABASE_URL`, creates
its own scratch database (for example `analyst_api_test_<random>`), applies
migrations, and truncates that scratch database's tables between tests. Without
`DATABASE_URL` those modules skip, and `conftest.py` ends the run with a red
"database-backed tests did not run" banner so the skip is not mistaken for a
pass. The database name
in `DATABASE_URL` is not the database those fixtures truncate; application data
is left intact. The configured user needs permission to create databases.

Every scratch database name includes a random suffix. Concurrent test processes
on one Postgres server therefore create and drop different databases. The
application database remains untouched. The compose `tests` service has its own
opt-in profile, so `make full-up` starts application services without tests.
`make test` explicitly selects the test service.

```python
SCRATCH = "analyst_commit_test_" + uuid.uuid4().hex[:12]

@pytest.fixture(scope="module")
def engine():       # CREATE DATABASE SCRATCH, migrate.run(), drop it afterwards

@pytest.fixture
def clean(engine):  # TRUNCATE piles CASCADE; TRUNCATE stage_events, stage_runs
```

**Why truncate rather than transaction-rollback.** Rollback-per-test is faster
and usually the right default — but it only works when a test uses one
connection. Our concurrency tests deliberately use **two** (`FOR UPDATE SKIP
LOCKED`, lease contention, epoch fencing), and they are the tests that matter
most. One strategy that works everywhere beats two strategies with a footgun.

Testcontainers was rejected: docker-in-docker complexity when compose already
provides the database.

### The drift test

Migrations are raw SQL; `db/tables.py` is hand-written Core metadata. Nothing
generates one from the other, so they can silently diverge.

```
tests/test_schema_drift.py
    apply every migration to a scratch database
    reflect it with SQLAlchemy
    assert reflected tables/columns/types == tables.py
```

Turns a runtime surprise into a red test.

---

## 4. Faking the outside world

### `.docx` edge cases — real files

Source `.docx` files are read as XML, not through python-docx (see
`CORE_INTERNALS.md`), and two real Word files pin that down:

```
fixtures/docx_edge_cases/
├── tracked_changes_and_comments.docx   an insertion, a deletion and a comment
└── confidential_header.docx            text in a page header, outside the body
```

The tests assert both directions: a tracked insertion is in the canonical text
and a tracked deletion is not; header and comment text never enter it.

### LLMs — `FakeLLM`

Returns canned structured output and records what it was asked.

```python
fake = FakeLLM(responses=[{"facts": [...]}])
fake.calls[0].schema      # assert the taxonomy actually constrained it
fake.calls[0].user        # assert document text never entered an instruction
```

The recorded calls are what make two injection tests possible without a model:
that the extractor is given no tools, and that instructions are composed by us
rather than concatenated from document text.

### Time

Lease TTLs are **injected**, not frozen. Tests call `lease.acquire(...,
ttl_s=0)`, so the lease is already expired and takeover happens in real time
without a clock library. Retry backoff works the same way: `sleep` is injected
into the LLM client.

---

## 5. The invariant suite

The behaviours the system promises, each a named test. All offline.

| Invariant | Test |
|---|---|
| Quote must be verbatim in source | `test_verbatim_gate_rejects_paraphrase` |
| Document text never enters an instruction | `test_instructions_are_composed_not_concatenated` |
| No two runs on one pile | `test_concurrent_runs_serialize` |
| A fenced worker cannot write | `test_stale_epoch_write_is_rejected` |
| Document commands are data | `test_the_injection_fixture_reaches_the_model_only_as_data` |
| Injection cannot suppress a deterministic rule | `test_a_check_rule_fires_despite_an_injected_instruction` |
| Files dropped while down are found | `test_files_dropped_while_down_are_found` |
| Partial writes are not ingested | `test_partial_write_is_not_ingested` |
| Same content, new path ⇒ not reprocessed | `test_same_content_new_path_is_not_reprocessed` |
| Same content, new format ⇒ not reprocessed | `test_same_content_different_format_is_not_reprocessed` |
| Replaced file supersedes, doesn't duplicate | `test_replaced_file_supersedes_not_duplicates` |
| "Could not look" never means "gone" | `test_an_unreadable_pile_folder_removes_nothing` |
| An emptied folder still marks its documents removed | `test_a_genuinely_emptied_pile_still_marks_its_documents_removed` |
| A namesake in another folder is not a version | `test_same_name_in_two_folders_is_two_contracts_not_a_version` |
| An alias is never handed to the graph | `test_an_alias_is_never_handed_to_the_graph` |
| A document is read outside any transaction | `test_hashing_and_parsing_happen_outside_the_transaction` |
| Deleting the original promotes its alias | `test_deleting_the_original_promotes_its_alias` |
| Checkpoints stay out of our fourteen tables | `test_checkpoint_tables_land_outside_our_schema` |
| A paused run resumes; it does not restart | `test_a_paused_run_resumes_into_the_gate_without_re_extracting` |
| A paused pile cannot be claimed by the timer | `test_a_paused_pile_is_not_claimed_until_an_explicit_resume` |
| A premature resume cannot bypass approval | `test_an_unapproved_resume_stays_at_the_human_gate` |
| A resumed checkpoint uses its fresh fencing epoch | `test_resume_replaces_the_checkpointed_lease_epoch` |
| Checkpoint resume survives a worker restart | `test_postgres_checkpoint_survives_a_worker_restart_without_reextracting` |
| Every graph model-work boundary renews its lease | `test_graph_renews_before_every_model_work_boundary` |
| An incomplete listing never guesses a version move | `test_an_incomplete_listing_never_guesses_a_missing_namesake_was_moved` |
| A file changed during a read is retried, not mixed | `test_a_file_changed_between_hash_and_parse_is_retried_not_mixed` |
| A paused run holds no lease | `test_a_pending_decision_pauses_the_run_and_releases_the_lease` |
| Same parties ⇒ one agreement | `test_two_documents_with_the_same_parties_share_one_agreement` |
| An extracted pile leaves the queue | `test_an_extracted_pile_leaves_the_queue` |
| A failed extraction stays in the queue | `test_a_document_whose_calls_all_failed_stays_in_the_queue` |
| A partial extraction publishes nothing and retries cleanly | `test_partial_extraction_retries_without_publishing_or_duplicating` |
| A section that always fails is quarantined after `job_max_attempts` | `test_a_section_that_always_fails_is_quarantined_after_the_attempt_limit` |
| A failed run is not recorded as ok | `test_a_run_whose_every_call_failed_is_not_recorded_as_ok` |
| An unverifiable amendment becomes a conflict | `test_an_amendment_claimed_without_verifiable_evidence_becomes_a_conflict` |
| An unreachable adjudicator defaults to conflict | `test_an_unreachable_adjudicator_defaults_to_conflict` |
| Agreeing sources cost nothing | `test_two_documents_agreeing_produce_no_review_item` |
| The same value written differently settles free | `test_the_same_value_written_differently_is_one_group` |
| A resolved slot is contested, never reverted | `test_a_resolved_slot_is_contested_not_reverted` |
| A conflict is raised once, not per run | `test_a_conflict_is_raised_once_not_once_per_run` |
| A dismissed finding is not asked again | `test_a_dismissed_finding_is_not_asked_again` |
| A rule that now passes clears its finding | `test_a_rule_that_now_passes_clears_its_finding` |
| A superseded document's facts stop counting | `test_a_rule_that_now_passes_clears_its_finding` |
| A rulebook is never executed while read | `test_a_rulebook_is_not_executed_while_being_read` |
| A judge finding without a verbatim quote is discarded | `test_a_judge_finding_whose_quote_is_invented_never_reaches_the_queue` |
| One clause, one review item | `test_a_judge_finding_and_a_conflict_about_one_sentence_share_a_group` |
| A term outside the taxonomy is still recorded | `test_a_term_outside_the_taxonomy_is_recorded_rather_than_dropped` |
| One subject, however it is worded | `test_two_documents_naming_one_subject_differently_share_a_slot` |
| The first write lands in the pile's own folder | `test_the_first_run_writes_the_final_document_into_the_pile_folder` |
| The output is never ingested as a source | `test_the_final_document_is_never_ingested_as_a_source` |
| A namesake in a subfolder is still a source | `test_a_subfolder_file_with_the_same_name_is_still_a_source` |
| One changed source ⇒ one changed section, the rest hash-identical | `test_a_changed_source_updates_only_the_section_it_affects` |
| A document that already agrees is left alone | `test_a_document_that_already_agrees_is_left_alone` |
| An approved rename lands exactly once | `test_an_approved_rename_reaches_the_document_exactly_once` |
| A rename is an edit of the same section | `test_a_rename_is_an_edit_of_the_same_section_not_a_removal` |
| Deleting a contract flags its terms and asks | `test_deleting_a_contract_flags_its_terms_and_asks_whether_to_remove_them` |
| An approved removal deletes the row | `test_an_approved_removal_deletes_the_row` |
| The removal marker is appended, never substituted | `test_the_marker_is_appended_to_the_value_never_substituted_for_it` |
| A missing or hand-edited document is restored from the record | `test_a_missing_or_damaged_document_is_restored_from_the_record` |
| A write that does not read back is reported, not claimed | `test_a_write_that_does_not_read_back_is_reported_not_claimed` |
| A crash mid-commit loses nothing, duplicates nothing | `test_a_crash_after_writing_the_file_loses_nothing_and_duplicates_nothing` |
| Resolve runs before a new commit | `test_resolve_runs_before_a_new_commit_is_opened` |
| Section hashes respect field boundaries | `test_section_hashes_respect_field_boundaries` |
| Writing the document needs no key and no network | `test_the_final_document_needs_no_key_and_no_network` |
| The run audits twice and then ends | `test_the_run_audits_a_second_time_and_then_ends` |
| Poison file quarantines, pile continues | `test_poison_file_quarantines_and_pile_continues` |
| Party names are never invented | `test_party_pick_is_constrained_to_gliner_spans` |
| Judge finding without a quote is discarded | `test_a_judge_finding_with_no_quote_is_discarded` |
| Model cannot invent a rule_id | `test_the_model_cannot_invent_a_rule` |
| An approval with no name is refused | `test_an_approval_without_a_name_is_refused_and_changes_nothing` |
| A second decision loses and is told so | `test_a_second_decision_on_the_same_item_loses_and_is_told_so` |
| The batch lane settles amendments only | `test_the_batch_lane_settles_amendments_and_leaves_everything_else` |
| A half-reviewed pile does not resume | `test_a_half_reviewed_pile_is_not_resumable` |
| Rejecting everything still finishes the run | `test_rejecting_every_item_still_makes_the_pile_resumable` |
| A failed resume leaves the pile reviewable | `test_a_resume_that_raises_leaves_the_pile_reviewable` |
| Upload writes a file, never a document row | `test_upload_writes_a_file_and_deliberately_no_document_row` |
| One upload ⇒ exactly one document | `test_upload_then_discovery_produces_exactly_one_document` |
| An upload filename cannot escape the pile | `test_an_uploaded_filename_cannot_escape_the_pile` |
| The stream cursor survives a run boundary | `test_the_replay_cursor_is_the_table_id_not_the_per_run_seq` |
| Every stage event notifies, from anywhere | `test_an_event_insert_notifies_without_anyone_remembering_to` |
| Replay and live delivery are one path | `test_the_stream_replays_then_delivers_live` |
| A stalled subscriber cannot grow memory | `test_a_stalled_subscriber_cannot_grow_the_brokers_memory` |
| An approval wakes the worker | `test_the_worker_listener_wakes_on_a_notification` |
| The document downloads from the pile folder | `test_the_document_downloads_from_the_pile_folder` |
| The document is reachable when the API is locked | `test_the_document_is_reachable_on_an_authenticated_instance` |
| Verification reads the file back | `test_verify_reads_the_file_back` |
| History names each section change and its decision | `test_history_lists_section_changes_with_their_decision` |
| The MCP tool returns the document verified | `test_the_final_document_comes_back_verified` |
| A token closes the routes, not `/health` | `test_with_a_token_set_the_routes_close_but_health_stays_open` |
| No route writes its own SQL | `test_no_route_writes_its_own_sql` |
| A malformed id is a sentence, not a 500 | `test_a_malformed_id_is_a_sentence_not_a_500` |
| The MCP server reaches the system only over HTTP | `test_the_server_reaches_the_system_only_over_http` |
| An agent cannot approve what it did not read | `test_approving_without_quoting_the_evidence_is_refused` |
| A near-miss quote is still a miss | `test_a_near_miss_is_still_a_miss` |
| A machine approval is always tagged as one | `test_the_actor_is_always_machine_tagged` |
| Rejecting needs no proof of reading | `test_rejecting_needs_no_confirmation` |
| An approved choice reaches the register | `test_an_approved_choice_reaches_the_register` |
| Commit resolves before it renders | `test_commit_applies_resolutions_before_it_renders` |
| A conflict cannot be approved without a winner | `test_approving_a_conflict_without_choosing_is_refused` |
| An agent can tell the options apart | `test_the_queue_shows_which_group_holds_which_value` |
| The stdio entrypoint speaks the protocol | `test_the_stdio_entrypoint_speaks_the_protocol` |
| The compose stdio service starts stdio | `test_the_compose_stdio_service_actually_starts_stdio` |
| A public MCP bind without a token refuses to start | `test_a_public_bind_without_an_inbound_token_refuses_to_start` |
| The download URL is one a caller can reach | `test_the_download_url_is_one_an_outside_caller_can_reach` |
| A choice off the list never erases the values | `test_resolving_to_a_missing_group_changes_nothing` |
| A settled slot does not re-open itself | `test_a_resolved_slot_does_not_reopen_itself_on_the_next_run` |
| New evidence re-opens it AND can be answered | `test_new_disagreeing_evidence_reopens_a_resolved_slot_and_asks_again` |
| Three values are never settled by comparing two | `test_a_three_way_disagreement_never_takes_the_amendment_fast_lane` |
| A verified chain settles on the latest value | `test_a_verified_chain_is_one_amendment_settling_on_the_latest_value` |
| One unproved step makes the slot a conflict | `test_one_unproved_step_makes_the_whole_slot_a_conflict` |
| An older value reappearing later is a conflict | `test_an_older_value_reappearing_later_is_a_conflict_without_asking_the_judge` |
| At most three documents stating the later value are tried | `test_at_most_three_documents_are_tried_and_the_newest_verdict_stands` |
| Evidence is checked in the paragraph shown, then the document | `test_evidence_is_checked_inside_the_paragraph_shown_before_the_whole_document` |
| Judging uses its own injected model | `test_judging_uses_the_separately_injected_model` |
| A source change withdraws only items whose evidence changed | `test_a_source_change_withdraws_only_the_items_whose_evidence_changed` |
| An approval on changed evidence is sent back and re-asked | `test_a_pending_item_with_changed_evidence_is_sent_back_and_asked_again` |
| Runs never stop for cost, and report what they spent | `test_runs_never_stop_for_cost_and_report_what_they_spent` |
| An unpriced model still runs and is reported as unpriced | `test_an_unpriced_model_still_runs_and_is_reported_as_unpriced` |
| Missing, expiring and expired prices are each named | `test_missing_expiring_and_expired_prices_are_each_named` |
| Discovery and repair continue while graph work is blocked | `test_discovery_and_repair_continue_while_graph_work_is_blocked` |
| A wake during a drain starts the next drain when it ends | `test_a_wake_during_a_drain_starts_the_next_drain_as_soon_as_it_ends` |
| OS files and rulebook lookalikes are not documents | `test_operating_system_files_and_rulebook_lookalikes_are_not_documents` |
| An open subject naming a field folds in only with the right kind of value | `test_a_name_match_with_the_wrong_kind_of_value_stays_an_open_subject` |
| An unsupported upload is refused before writing | `test_unsupported_upload_is_refused_before_writing` |
| A judge outage is not reported as compliant | `test_a_judge_outage_is_not_reported_as_compliant` |
| Answering a finding moves the finding | `test_answering_a_finding_moves_the_finding` |
| A conflict with no options cannot be approved | `test_a_conflict_with_no_options_cannot_be_approved_at_all` |
| The API serves the dashboard, same origin | `test_the_api_serves_the_dashboard_when_one_is_built` |
| A mistyped path is not answered with the app | `test_a_mistyped_api_path_is_not_answered_with_the_app` |
| A highlight marks the span, not the first match | `cut` — `frontend/src/api/spans.test.ts` |
| A verdict about the effective date is never an amendment | `test_an_amendment_verdict_on_the_effective_date_is_never_taken` |
| Each prompt's wording is pinned by its `VERSION` | `test_every_version_has_a_snapshot` (+ the `SNAPSHOTS` hashes) |
| The API and the dashboard share one mark | `test_the_api_and_the_dashboard_share_one_mark` |
| Citation offsets land on their quote over HTTP | `test_every_citation_offset_lands_on_its_own_quote` |

### The two hardest to write

**`test_a_crash_after_writing_the_file_loses_nothing_and_duplicates_nothing`** —
simulate the dangerous window: let `Commit` write `final-document.docx`, then
make recording it raise, as a kill between the two would. Assert the file got
ahead of the record and the record rolled back to version 0. Then run again and
assert exactly one version, each section recorded once, and a file that reads
back as recorded. This is the test for the failure that would record a document
that does not exist, or record one twice.

**`test_stale_epoch_write_is_rejected`** — two real connections. Worker A takes
the lease at epoch 7, worker B takes over at epoch 8, then A attempts a write
carrying epoch 7 and must get zero rows. Needs genuine concurrency, which is why
the DB strategy is truncate rather than rollback.

---

## 6. Fixture integrity, checked before the suite runs

`scripts/verify_fixtures.py` asserts every `source_quote` in `ANSWER_KEY.json` is
a verbatim substring of its document. **Currently 107/107 exact.**
`scripts/build_fixture_docs.py --check` (also `make fixtures-check`) confirms
the `.docx` fixtures still match their `.md` sources.

Both run in CI before pytest. If the answer key drifts from the fixtures, every
downstream assertion is checking against a lie — better to fail immediately and
say so.

---

## 7. Live provider tests

Offline provider tests cover automatic LLM selection from keys (Anthropic,
Gemini, OpenAI), independent OpenAI/Gemini embedding selection, explicit
provider overrides, and the two setup requirements returned by `/health`.
Gemini embedding tests check batching, vector normalisation, response shape,
dimensions and estimated usage. These use stub SDK responses and do not prove
that a particular key or hosted model is currently usable.

The setup screen polls `/health` every four seconds and unlocks when both
requirements are met. Browser checks should cover no keys, one OpenAI key,
one Gemini key, Claude alone, Claude plus an embedding key, and a pinned
provider whose key is absent. The frontend unit suite tests utility functions
only; the Playwright spec (`frontend/e2e/workspace.spec.ts`) covers the gate
unlocking once missing credentials become ready, against synthetic `/health`
responses, not real keys.

`tests/test_llm_live.py` is marked `live` and skipped unless asked for
(`RUN_LIVE_LLM=1 uv run pytest -m live`). It checks what is only knowable by
calling the providers: that the configured models exist, accept our schemas
and return what `structured()` expects.

These exist because **a provider's written spec can be wrong**, and finding out
during a demo is the expensive alternative. Nothing else in the suite needs a
key: the final document is written and verified locally.

The credential-isolation fixture is scoped per test. It clears credentials for
all offline tests and preserves them only for explicitly opted-in `live` tests.
Live checks use the configured extraction model for the selected provider and
the configured catalogue default for other providers.

---

## 8. CI

`.github/workflows/ci.yml` runs on pushes and pull requests:

```
Dashboard checks (Node 24, frontend/)
  1. npm ci · npm test (vitest) · npm run build
  2. playwright install chromium · npm run test:e2e   synthetic HTTP fixtures
Offline application suite
  3. docker compose --profile full run --build tests   the real image; db +
                                                       migrate come up first
     inside it: ruff check · ruff format --check · mypy
                build_fixture_docs.py --check · verify_fixtures.py
                pytest -m "not live"
```

`frontend/` is mounted read-only into the test container so `test_brand.py`
can compare the two icon copies (the image itself carries only the built
dashboard; under `make test`, which does not add that mount, the test skips).

**CI builds the actual image** rather than installing dependencies directly.
Slower on a cold cache, but it verifies the claim that matters — a stranger
clones this and `make full-up` works. A CI that tests a setup no human uses
proves nothing about that.

The workflow needs no API secrets; its offline tests use scripted
providers and the local models baked into the image.

The workflow's commands are exercised locally; a GitHub-hosted run still needs
the changes pushed to a repository with Actions enabled. No API secrets are
required. Browser regressions cover delayed responses, mobile navigation,
Unicode highlights, uploads, keyboard review, modal focus, and setup unlocking.

### `make test` runs in its own service, with no `.env`

Found in Phase 5, and it is the same shape as the credential leak `conftest.py`
fixes. `make test` used to run inside the `worker` service, which loads `.env` —
and compose exports those values into the container as **real environment
variables**, which beat the `_env_file=` a test passes explicitly.

So a developer who had pinned `LLM_MODEL_EXTRACT` in their own `.env` watched
three tests fail that assert the *default* model, while CI — which has no
`.env` — passed. The suite was reading one machine's configuration and reporting
it as behaviour, in whichever direction that machine happened to be configured.

There is now a `tests` service with **no `env_file`**, and `make test` runs in
it. Whether the suite sees a configuration must be a property of how it is run,
not of whose laptop is running it.

---

## See also

[`BUILD_AND_RUNTIME.md`](BUILD_AND_RUNTIME.md) — how CI's image build, the model
prefetch stage and the compose topology actually work.
