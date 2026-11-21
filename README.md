# Documents Analyst

Drop a folder of related documents in. Get back one **final document** —
`final-document.docx`, written into that same folder — stating every material
term, what each term says, which document it came from, and which documents
disagree. A human approves every choice before it lands.

The system reads the pile, extracts facts, grounds every fact against the
source text, matches them across documents, flags conflicts and amendments,
checks everything against the pile's `rulebook.yaml`, and stops at a human gate.
After review it writes the final document. When a new document arrives, only
the sections that document affects change, and the system can prove the rest
are byte-identical.

```
watched/acme/                     ← a folder is a pile
├── 01_msa.docx
├── 02_sow.pdf
├── 04_amendment.md
├── rulebook.yaml                 ← optional: the rules to check against
└── final-document.docx           ← written by the system after review
```

---

## Quick start

1. **Install Docker** (Desktop on macOS/Windows). Nothing else is needed on the host.
2. **Add your keys.** `cp .env.example .env`, then set `OPENAI_API_KEY` (embeddings,
   always needed) and the key for your `LLM_PROVIDER` (`GEMINI_API_KEY` by default, or
   set `LLM_PROVIDER=openai` and one OpenAI key covers both). `make up` creates `.env`
   for you if you skip this, and the dashboard lists whatever is still missing.
3. **Choose where your folders live.** By default it is `watched/` inside this repo:
   copy each folder of documents into it. To use a folder anywhere else on your
   machine, set `WATCH_DIR=/absolute/path/to/folder` in `.env` (no `~`). You never move
   the repo or pass individual files — the system watches that one folder, and **each
   subfolder of it is one pile**.
4. **Start it:** `make up`, then open **http://localhost:8000**. The first build takes
   5–10 minutes (it bakes the local models into the image).
5. **Review.** Type your name in the sidebar, open the **Review** tab, pick the winning
   value for each conflict and approve, or reject. Findings from `rulebook.yaml` appear
   there too.
6. **Collect the result.** When the queue is empty, `final-document.docx` is written into
   the pile's own folder (`<WATCH_DIR>/<pile>/final-document.docx`). Download it from the
   top right of the dashboard, or open it from the folder.

The dashboard shows this same guide, with your actual folder path, on first load and
whenever no folder has been found yet (the **?** in the sidebar reopens it).

## Run it

```bash
make up
```

That is the whole thing: it creates `.env` from the example, puts a demo pile in
`watched/` (only while `WATCH_DIR` is the default — never into a folder of yours), builds, migrates, and starts Postgres, the API and the worker. When
it returns, the dashboard is on **http://localhost:8000** with a folder of
documents already in it. `make down` stops it.

**It works before you have any keys.** The dashboard loads, the banner names
every missing credential and what each one costs you, and the worker discovers
and ingests documents while refusing to run the graph. Reading a folder needs no
model, and pretending to extract without one would be worse than stopping. Put
a key in `.env`, run `make up` again, and the same pile carries on from where it
stopped.

The first build takes 5–10 minutes, because the image bakes the two local models
so the test suite needs no network. Every build after that is cached.

For development with hot reload (Docker for Postgres only, the API on the host):

```bash
make db-up && make migrate-local && make api
```

See [DEVELOPMENT.md](DEVELOPMENT.md) for both modes and every command.

### Keys

Keep API keys and model choices in `.env`. Edit
[`config/runtime.yaml`](config/runtime.yaml) for paths, budgets, timings, the
final document's file name and other shared, non-secret behaviour.

You need **two things**: an LLM key and an embedding key. Until both are set, the
dashboard shows a setup screen that lists them and blocks everything else. It
unlocks by itself once they are set (save `.env`, run `make up` again).

| Need | Any one of | Notes |
|---|---|---|
| LLM key: extraction and judging | `ANTHROPIC_API_KEY` (Claude), `OPENAI_API_KEY`, `GEMINI_API_KEY` | With `LLM_PROVIDER` blank, the provider is picked from the keys: Claude, then Gemini, then OpenAI. Set `LLM_PROVIDER` to choose. Claude defaults to `claude-haiku-5-5`. |
| Embedding key: matching terms across documents | `OPENAI_API_KEY`, `GEMINI_API_KEY` | **Claude has no embedding model**, so a Claude user also adds an OpenAI or Gemini key. |

A single OpenAI key, or a single Gemini key, covers both. You never need a Gemini key
if you have an OpenAI one.

Writing the final document needs no key and no network. It is local file work.

## Using it

A folder is a pile. There is no creation step:

```bash
make seed        # copies a fixture pile → watched/acme
make scan-local  # one discovery pass
```

```
acme: +8 new, 1 aliased, 0 superseded, 0 restored, 1 quarantined, 0 removed
  01_msa.md: same text as 01_msa.docx
  08_corrupt.pdf: could not be read as PDF — Unexpected EOF
```

`01_msa.md` and `01_msa.docx` are recognised as **one** document, not two that
disagree about everything. `08_corrupt.pdf` is quarantined with the reason, and
the other seven documents land normally. More sample piles are in
[`doc piles/`](doc%20piles/README.md); copy one into `watched/` (or your `WATCH_DIR`).

```bash
make run         # discover, then run the graph over whatever is ready
```

```
acme: run 6d588b97 over 8 document(s) — finished
```

The graph runs in the container because the local models are baked into the
image rather than installed on your host. `make run-local` does the same on the
host if you fetched them yourself.

## Folders, files and threads

**One subfolder of the watched folder = one pile = one final document.**

```
<WATCH_DIR>/
├── folder1/            5 files  → folder1/final-document.docx
└── folder2/           10 files  → folder2/final-document.docx
```

Each pile is processed on its own: its own run, review queue, history and document.
Nothing from `folder1` is compared with `folder2`.

**Inside one pile, files are processed strictly one at a time, in order** (the order
they were found). The worker reads file 1 (hash, parse), then file 2, and so on; then
extracts facts from file 1 completely before starting file 2. No two files of the same pile are ever in flight at
once. Only after every file is extracted do the cross-file steps run, once, over the
whole pile: match the facts across files, find conflicts and amendments, check the
rulebook, stop for your review, then write the final document. That order is required,
not a choice: a conflict needs two files to exist, so the document is written once per
review rather than once per file. A file added later goes through the same steps, and
only the sections it touches change.

**Separate piles run in parallel, one thread each.** With `folder1` and `folder2` both
waiting, `folder2` does not wait for `folder1`'s extraction. The worker runs up to
`pile_concurrency` piles at once (`config/runtime.yaml`, default 3; set it to 1 for one
pile after another). A pile is claimed with `FOR UPDATE SKIP LOCKED` and a fenced lease,
so two threads — or two worker containers — never process the same pile. The local
models are shared by the threads and take turns; LLM calls run concurrently, so raise
`pile_concurrency` only as far as your provider's rate limit allows.

## The final document

After review, the commit step writes `watched/<pile>/final-document.docx`:

```
Final Document — acme
  (a preamble: generated from source, changed only after review, certifies nothing)

Acme Corporation ↔ Beta Logistics Inc          ← one heading per agreement
  Recurring price
    USD 12,000 (stated in 2 documents)
  Payment terms
    ⚠ sources disagree — awaiting review — 30 days (1 source(s)); 45 days (1 source(s))
  Expiry date
    2027-01-01 — ⚠ source document was removed
```

**One section per term.** Each section is the agreement it belongs to, the
term's heading and its text. Each one gets a SHA-256 over exactly those three
strings, and the database records every section as it was last written. That
record is what turns these promises into checks:

| Promise | How it is checked |
|---|---|
| **An update costs like an update** | A commit compares every section's hash with the recorded one. Only sections whose hash moved get a version row. Change one term in one contract and you get one changed section; every other hash is byte-identical to what was there before. |
| **What changed, when, and why** | `GET /piles/{id}/deliverable/history` returns one row per changed section per version, with the text before and after, both hashes, and the decision that authorised it (or `source_update` / `new_field` / `orphan_flag` when nobody had to decide). |
| **A decision lands exactly once** | The commit that carries a decision records it in the same transaction as the document write. An approval cannot be applied twice. |
| **A crash loses nothing** | The file is written to a temp file and renamed into place, inside the transaction that records it. If the process is killed in between, the record rolls back and the next run writes the same sections and records them once. |
| **The file is never the source of truth** | Every run starts by reading the file back, and so does every scan of an idle pile (a stat each pass; the file is only re-read when it changed). If it is missing, truncated or hand-edited, it is restored from the record within one scan interval, with no new version. |
| **A success message means it** | After every commit the file is read back off disk and compared section by section. A mismatch raises a review item instead of a success event. |

**Flagging is ungated; choosing and deleting are gated.** A row whose source
document changed is re-rendered without asking, because the document states what
the sources say. A row whose source file was deleted keeps its value and gains a
marker, and a human is asked whether to remove it. Picking between disagreeing
sources, renaming a row and deleting a row each need an approved decision.

The scanner skips exactly `final-document.docx` at the root of each pile, so the
system never reads its own output back in as a source.

```bash
make demo-crash  # damage final-document.docx, restart the worker, watch it restored
```

## The dashboard

```bash
make ui-build    # → frontend/dist, which the API serves at http://localhost:8000
make ui          # or the dev server on :5173, hot reload, API proxied
```

Four things on one screen, scoped to a pile, plus a download link for the final
document:

| | |
|---|---|
| **Register** | Every term as a document, not a table. A disputed term shows *all* its values and no answer: the register never picks a side |
| **Source** | Click any citation and the source opens beside it with the exact span highlighted, at the offsets the grounding check verified |
| **Review** | Approve or reject, choosing which value wins. The API's rules are the UI's rules: Approve stays disabled on a conflict until you pick one |
| **Timeline** | Scrub through every stage the run went through, live while a run is going |

No CSS framework and no router. `#pile=<id>&tab=review` makes a pile a
shareable link. It follows your system's light or dark setting, with a toggle,
and it is served from the same origin as the API.

## Driving it over HTTP

Everything above is reachable without a UI. `/docs` is generated from the
routes, and `make demo-api` walks the whole workflow with `curl`.

| | What it is | When it is required |
|---|---|---|
| `X-Actor` | A name, like a git author | On every decision; it becomes `decided_by` |
| `Authorization` | One shared `API_TOKEN` | Only if you set one; blank by default |

An approval with no `X-Actor` is refused rather than recorded as `"api"`,
because that row is the only authority the system has for changing your
document.

```bash
curl -X POST localhost:8000/decisions/$ID/approve \
  -H 'X-Actor: your name' -H 'content-type: application/json' \
  -d '{"note": "the amendment governs", "chosen_value_group": 1}'

curl -O -J localhost:8000/piles/$PILE/deliverable/document   # the .docx
curl localhost:8000/piles/$PILE/deliverable/verify           # read back and compared
curl -N localhost:8000/piles/$PILE/events                    # the live stream
```

Approving does not resume the run by itself. A worker resumes a pile when its
queue is **empty**: the gate re-interrupts while anything is still pending, and a
reviewer who rejects everything has still finished reviewing.

The event stream replays before it follows. Send `Last-Event-ID` and you get
exactly what you missed. Uploading is `POST /piles/{id}/documents`, which writes
the file into the pile's folder and lets the scanner do the rest, so there is one
ingestion path.

## Driving it from an agent

```bash
claude mcp add documents-analyst -- docker compose run --rm -T mcp
```

Seven tools; `make mcp-tools` prints them. Five are read-only, including
`get_final_document`, which returns the document's version, its hash, whether it
read back correctly, and a `document://` resource to fetch it through. The two
that write (`approve_item`, `reject_item`) are annotated as such.

**An agent may only approve what it has read.** `approve_item` requires a
verbatim quote from the item's evidence, checked by exact substring. Looping
over decision ids gets you nothing:

```
approve_item(id, confirm="looks fine")
  → refused: the `confirm` text is not a verbatim quote from this item's evidence
```

Whatever an agent approves is recorded as `mcp:<name>`, never as a person. The
server is a separate process that reaches the system **only** over the HTTP API.
The compose service has no database credentials, and a test fails the build if
anything in that package imports one.

---

## The domain

The register has room for **28 fields** in six groups, and the model may fill
only those. A `field_key` outside the list is refused
([`taxonomy/schema.py`](packages/core/src/documents_analyst_core/taxonomy/schema.py)),
so the model can no more invent a field than it can invent a value.

| Group | Fields |
|---|---|
| **Identity** | parties · title · type · effective and expiry dates · governing law |
| **Commercial** | total value · recurring price · billing frequency · payment terms · late-fee amount and rate |
| **Risk** | liability cap (amount and type) · indemnity · warranty period · insurance required |
| **Lifecycle** | term length · auto-renew · renewal notice · termination notice · termination for convenience |
| **Service** | uptime % · support response time · service credits |
| **Data** | DPA present · subprocessors allowed · confidentiality period |

A material term the taxonomy has no room for is kept as an open subject, matched
across documents by meaning, and surfaced as a **taxonomy gap**, not dropped.

## Formats

`.pdf`, `.docx`, `.html`/`.htm`, `.txt`, `.md`. Anything else is quarantined by
name with the reason, and the pile carries on: one unreadable file never stops
the other nine. Scanned or image-only PDFs are not OCR'd and yield no facts
rather than wrong ones.

## How it avoids bluffing

Every value carries a citation, and every citation is checked by exact string
comparison against the extracted text at recorded offsets. A claim whose quote
is **not in the document at all is dropped**, never paraphrased into one that
is. When a quote is verbatim but its offsets are wrong, it is kept only if it
occurs **exactly once**. Two occurrences is a refusal, because choosing between
them would be a guess. That check
([`verify.py`](packages/core/src/documents_analyst_core/portable/verify.py)) has no
model, no fuzzy matching and no threshold, which is what makes it an offline
test rather than a promise.

A document that contains instructions aimed at the system is reported as a
finding and never obeyed. The `injection_pile` fixture proves that injected text
produces no facts.

## Tests

```bash
make test        # full suite in Docker, no keys needed
make test-local  # same, from your virtualenv (needs Postgres: make db-up)
make ui-test     # the dashboard's own tests
```

638 backend tests pass with no API keys and no network, including the
Postgres-backed ones, plus 13 dashboard tests. They cover:
- a run killed and resumed;
- two workers racing for one pile;
- a document that tries to give orders;
- the final document's focused update, repair, read-back verification, and a
  crash between writing the file and recording it.

[`fixtures/ANSWER_KEY.json`](fixtures/ANSWER_KEY.json) pins what a correct run
produces. `make fixtures-check` proves every answer-key quote is a
character-exact substring of its source.

## What it deliberately does not do

- **The final document is generated, not authored.** A hand edit to
  `final-document.docx` is overwritten on the next run, because a value no source
  supports is worse than a reverted edit. Renaming a row goes through review and
  persists.
- **It covers the document text it writes, nothing more.** The `.docx` carries a
  title, author and comment in its properties. No other metadata is managed.
- **It certifies nothing.** Findings are findings. Nothing here is a legal
  opinion or a compliance certificate.
- **It is not a collaborative editor or a document archive.** Your source files
  are read and never modified. The only file written into a pile is the final
  document.

## Documentation

| Doc | Covers |
|---|---|
| [`docs/CODE_ARCHITECTURE.md`](docs/CODE_ARCHITECTURE.md) | Foundations, dependencies, models |
| [`docs/REPO_STRUCTURE.md`](docs/REPO_STRUCTURE.md) | Layout, and the `portable/` zone |
| [`docs/CORE_INTERNALS.md`](docs/CORE_INTERNALS.md) | The shared library |
| [`docs/WORKER_RUNTIME.md`](docs/WORKER_RUNTIME.md) | Threads, leases, crash behaviour |
| [`docs/BACKEND_INTERNALS.md`](docs/BACKEND_INTERNALS.md) | Graph, final document, API, data layer, MCP |
| [`docs/TESTING.md`](docs/TESTING.md) | Suite, fixtures, CI |
| [`docs/BUILD_AND_RUNTIME.md`](docs/BUILD_AND_RUNTIME.md) | Docker, compose, first run |
| [`docs/THREADING.html`](docs/THREADING.html) | Threads and leases, by example |
| [`docs/UI_AND_LINKING.html`](docs/UI_AND_LINKING.html) | How a citation click reaches the source span |
