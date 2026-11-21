# Code Architecture

How the code is organised, and why. This is the *engineering* companion to
the [`README`](../README.md), which covers the *system* — the flows, gates and
schema. Nothing here changes that; it decides how it gets built.

Written phase by phase. Phase 1 below.

---

## Phase 1 — Foundations

Four decisions that shape every file. Each is recorded with what it forces downstream.

| Decision | Choice |
|---|---|
| Concurrency | **Async API, sync worker** |
| Data access | **SQLAlchemy Core** — no ORM |
| LLM structured output | **Raw provider SDKs** behind a thin protocol |
| Code sharing | **A `portable/` zone with zero internal imports — copyable by design** |

---

### 1.1 Concurrency — async API, sync worker

**Why not async everywhere.** The worker's real work is CPU-bound: parsing,
NLI, GLiNER. A single blocking `.encode()` inside an
event loop stalls everything else on that loop. Wrapping every call in
`asyncio.to_thread` works, but the day someone forgets one, the failure is
silent and looks like "the app got slow".

**The insight that settles it:** *our unit of concurrency is the process, not
the coroutine.* One pile runs under one lease. Inside a pile the pipeline is
inherently sequential — you cannot classify before you extract — and the files
of one pile are ingested and extracted one at a time, in discovery order.
Parallelism across piles comes from two places, both plain threads and
processes rather than an event loop: each worker runs up to `pile_concurrency`
piles at once on a small thread pool, one lane per pile (default 3; 1 restores
one pile after another), and you can run more worker processes. Lanes and
processes are kept apart by the same `FOR UPDATE SKIP LOCKED` claim and fenced
lease, so a lane is just a worker that happens to share an address space. What
the lanes share — the checkpointer, the LLM client, the embeddings client and
the two local models — is made safe once, in `models.shared_across_threads` and
a thread-local `last_usage`; see [`WORKER_RUNTIME.md`](WORKER_RUNTIME.md) §1a.
Async would buy the worker nothing and cost it real complexity.

The API is the opposite: many clients polling, SSE connections held open,
Postgres round-trips. All I/O. Async is plainly correct there.

psycopg3 speaks both, so this is one driver with two engine configurations —
not a split stack.

```
backend/api/      async   →  create_async_engine("postgresql+psycopg://…")
backend/worker/   sync    →  create_engine("postgresql+psycopg://…")
packages/core/    sync    →  no engine; callers pass a connection
```

**`packages/core` is sync throughout.** It is consumed by the worker directly,
and its `portable/` files are written to be copied anywhere (§1.4). Keeping
it sync means no async test fixtures, no coloured-function problem, and one
obvious way to call anything.

#### The consequence that isn't obvious

If core is sync and the API is async, then *the API cannot call core freely* —
doing so blocks the event loop. Tracing that through produces a rule the
architecture already implied but never stated:

> **The API never does anything slow or stateful.
> The worker owns that work.**

Concretely:

| Operation | Where it runs | Why |
|---|---|---|
| LLM calls, NLI, GLiNER, embeddings | worker only | Slow, CPU-bound or billed; must sit under the pile's lease |
| Writing `final-document.docx` | worker only, inside `Commit`'s transaction | The file and its record must change together, fenced on the lease |
| Reading the document back (`/deliverable/verify`) | API, via `asyncio.to_thread` | python-docx parsing is sync but fast; the UI and MCP want it promptly |
| Serving the `.docx` | API, `FileResponse` | A file read, resolved by pile name under the API's own `watch_root` |
| Everything else the API does | async, DB only | Reads, decision writes, SSE replay from `stage_events` |

No route writes the document. That keeps the "only the worker changes the
deliverable" edge in the topology diagram literally true, rather than
aspirational.

---

### 1.2 Data access — SQLAlchemy Core, no ORM

Our queries are not CRUD. The ones that matter are:

- claiming graph work: `SELECT … WHERE status='ingested' FOR UPDATE SKIP LOCKED`
- epoch-fenced writes: `UPDATE … WHERE lease_epoch = :mine`, then check rowcount
- lease acquire: `INSERT … ON CONFLICT DO UPDATE … WHERE expires_at < now()`

These are two separate comparisons and they should not be blurred:

**SQLAlchemy over raw SQL** — parameter binding, composable query fragments, and
pgvector's integration giving a real `Vector` type and `.cosine_distance()`
instead of hand-written `<=>` casts. *This applies to Core and ORM equally.*

**Core over ORM** — specific to the three queries above:

| Query | Why the ORM hurts |
|---|---|
| `FOR UPDATE SKIP LOCKED` | Supported, but fights the session's identity map |
| Epoch-fenced `UPDATE` | We need **rowcount** to detect a lost lease — the session abstracts it away |
| `ON CONFLICT DO UPDATE … WHERE` | No clean ORM path |

The ORM's real strengths — declarative classes defining the schema, lazy-loaded
relationships, unit-of-work flushing — buy us nothing here. We do not use
SQLAlchemy to create tables at all (migrations are raw SQL), and our reads are
explicit joins rather than object graphs.

Core keeps the SQL visible and composable, and `text()` is there for the two or
three genuinely gnarly statements.

**Shape:**

```
backend/db/tables.py     Core Table objects on one shared MetaData
backend/db/rows.py       frozen dataclasses returned by repos
backend/db/repo/*.py     one module per aggregate; functions take a connection
```

Repos take a connection rather than opening one, so the caller controls the
transaction boundary. The API runs one transaction per request.

**Worker transaction rule: wrap writes, not slow work.** A graph node does its
LLM calls and model inference *outside* any transaction, then opens a short one
to write its results and its checkpoint together. Holding a transaction open
across a five-minute extraction would keep row locks for the whole run — and a
frozen worker would then block the very takeover the lease exists to allow.
Paired with `idle_in_transaction_session_timeout = 60s` on the database, a
stalled session cannot wedge another worker.

Two consequences worth stating, because a resumed run depends on both:

- **Node writes are idempotent** — upsert on a natural key, never a blind
  insert. A node interrupted halfway will be run again from the start.
- **A node's data and its checkpoint commit together**, so a resume can never
  skip past a node whose output did not land.

#### The risk this creates, and how it is closed

Migrations are raw SQL (decided in the system plan — the schema is the story and
should be readable top to bottom). But `tables.py` is hand-written Core
metadata. **Those two can drift**, and drift would be discovered at runtime.

Closed with a test:

```
tests/test_schema_drift.py
    apply every migration to a scratch database
    reflect it with SQLAlchemy
    assert reflected tables/columns/types == tables.py metadata
```

Cheap, runs offline, and turns a class of silent bug into a red test.

---

### 1.3 LLM layer — raw SDKs behind a thin protocol

Schema-constraining is a **correctness feature** here, not ergonomics: it is
what stops the model inventing a `field_key` outside the taxonomy, and what
forces the party-picker to choose from GLiNER's spans by index. We want the
exact payload visible in our code.

We also need per-call token counts for `stage_runs`, which is a core
requirement — the cost report is built from them. Wrapper libraries tend to obscure exactly that.

The surface is small enough that a library would not save much:

```python
class LLMClient(Protocol):
    def complete(self, *, system: str, user: str, max_tokens: int) -> tuple[str, Usage]: ...

    def structured(
        self, *, system: str, user: str, schema: dict, max_tokens: int
    ) -> tuple[dict, Usage]: ...
```

Three provider implementations. The provider difference is confined to one
method each:

| Feature | Gemini | Anthropic | OpenAI |
|---|---|---|---|
| Structured output | `response_json_schema` | tool with `input_schema`, forced via `tool_choice` | `response_format={"type":"json_schema", …, "strict": true}` |
| Usage | normalised from Gemini usage metadata | `response.usage.input_tokens / output_tokens` | `response.usage.prompt_tokens / completion_tokens` |

Both normalise into one `Usage` dataclass, so `telemetry` and the cost widget
never branch on provider.

**Retries are ours.** Bounded, and *every attempt* is recorded — including the
failures. A retry that silently succeeds on attempt three still cost tokens, and
a cost report that hides that is lying.

---

### 1.4 The `portable/` zone — copyable by design

A handful of core files need nothing of ours: text extraction, the LLM client,
the GLiNER wrapper and `verify_quote`. Every one of them is written with **zero
internal imports**: third-party SDKs and the standard library only, errors
declared in the same file, telemetry taken as a callback rather than importing a
protocol.

```
packages/core/src/documents_analyst_core/portable/
    document_text.py   llm_client.py   ner_gliner.py   verify.py
```

That makes them liftable verbatim into another project — and, just as useful
here, testable in complete isolation, with no fixture or setting of ours
present. `FakeLLM` lives inside `llm_client.py` for the same reason: a copy must
be able to test its own LLM path.

The constraint is visible at a glance: `portable/` is the only code in the repo
importing nothing of ours.

Full layout and reasoning in [`REPO_STRUCTURE.md`](REPO_STRUCTURE.md).

---

### 1.5 The resulting process map

```
┌─ api process ────────────── async ─┐   ┌─ worker process ───────── sync ─┐
│  FastAPI + SSE                     │   │  scanner · lease · graph        │
│  async engine (psycopg)            │   │  sync engine (psycopg)          │
│  DB, plus reading the .docx back   │   │  writes final-document.docx     │
└────────────────┬───────────────────┘   └────────────────┬────────────────┘
                 │                                        │
                 └──────────  Postgres  ──────────────────┘
                        LISTEN/NOTIFY · stage_events

┌─ mcp process ───────── async ─┐
│  7 composed tools             │
│  HTTP client of our own API   │
└───────────────────────────────┘

packages/core  —  sync, no engine, no framework.
                  Imported directly only by the worker.
                  Its portable/ subset imports nothing of ours.
```

Nothing in `packages/core` imports FastAPI, LangGraph or SQLAlchemy — it is a
library of capabilities, not application wiring. And nothing in
`documents_analyst_core/portable/` imports anything of ours at all, which is precisely
what makes those files copyable.

---

## Conventions that follow from the above

**Typing.** `mypy` non-strict on `packages/core` only. It is the shared surface,
so a wrong type there propagates into three consumers; application wiring is
less load-bearing.

**Models.** Pydantic at boundaries (API request/response, config, LLM output
validation). Frozen dataclasses internally. Validating every internal object
costs time and couples internals to the API layer for no benefit.

**Errors.** A typed exception hierarchy (`errors.py`, and `LLMError` with
`LLMTransportError`, `LLMSchemaViolation` and `LLMQuotaExhausted` declared
inline in `portable/llm_client.py`). Nodes catch `LLMError` specifically;
catching broadly would swallow real bugs into a "the provider is down" state.

---

## Observability — logs and tables have different jobs

**Logs are the narrative. Tables are what you query.** They are not alternatives
and neither replaces the other.

Logging is the **standard library's** `logging`, to stdout only — no file, no
rotation, no volume mount. Docker already stores it, so `docker compose logs
worker` is the debug view and it behaves identically in CI.

This paragraph used to credit `structlog`, and claimed every record carries
`run_id` and `pile_id`. Neither was true: nothing ever imported structlog, and
the log lines are plain messages with no correlation fields on them. **The
durable per-run narrative is `stage_events`**, which is queryable, survives the
process, and does not depend on anyone keeping a log format consistent. That is
the table to read when following one run; the logs are for stack traces.

Logs carry what you cannot usefully put in a table: full stack traces, every
retry attempt, the reason a finding was discarded, the raw response that failed
to parse.

Two things stay in Postgres because a log file genuinely cannot serve them:

| Table | Why not a log |
|---|---|
| `stage_events` | It is the **SSE replay source**. A reconnecting browser asks "events for run X since seq 42" — that is a query, not a grep |
| `stage_runs` | Cost is a **first-class output**. `SELECT node, SUM(llm_cost_usd) GROUP BY node` beats parsing and summing a file, and logs rotate away while the write-up still needs the numbers |

Two workers appending to one file also interleave; Postgres handles concurrent
writes correctly.

**`stage_runs` stays a real table** (rather than a view over `stage_events`) for
typed columns and simpler aggregation. That accepts a real redundancy — the same
numbers exist in two places — so one discipline closes it:

> A single helper writes the usage event **and** the `stage_runs` row in one
> transaction. No other code path records cost. If they can only be written
> together, they cannot disagree.

The log never repeats those numbers. It references `run_id` so you can correlate,
and that is all.

---

## Phase 2 — Dependencies and configuration

### 2.1 Workspace

`uv` workspace, Python pinned to **3.12** (`>= 3.12, < 3.13` — some ML wheels still
lag on 3.13, and the host here is 3.9 so everything runs in Docker anyway).

```toml
# pyproject.toml  (repo root — workspace only, not a package)
[tool.uv.workspace]
members = ["packages/core", "backend"]

[tool.uv.sources]
documents-analyst-core = { workspace = true }
# All Linux, both architectures. This was scoped to x86_64 on the assumption
# that /whl/cpu carried no aarch64 wheel — measured false in Phase 2C: it has
# since torch 2.3.0, and our floor is >=2.3. The narrow marker meant linux/arm64
# fell through to PyPI and pulled the CUDA build: 2.9GB of nvidia-* plus 652MB
# of triton, in a CPU-only container. macOS is unaffected either way.
torch = [{ index = "pytorch-cpu", marker = "sys_platform == 'linux'" }]

[[tool.uv.index]]
name = "pytorch-cpu"
url = "https://download.pytorch.org/whl/cpu"
explicit = true          # only torch resolves here; everything else uses PyPI

[dependency-groups]
dev = ["pytest", "pytest-asyncio", "respx", "pytest-cov", "ruff", "mypy", "types-PyYAML"]
```

The explicit `pytorch-cpu` index is what keeps CUDA out. Default PyPI torch
drags ~2GB of GPU libraries we can never use on this stack.

**Version policy:** floors in `pyproject.toml`, exact pins in `uv.lock`. The
lockfile is committed and is the reproducibility guarantee — `uv sync --frozen`
in the Dockerfile means the reviewer builds exactly what we built.

> Honest note: the floors below are minimums I'm confident in, not a claim about
> what's newest. `uv lock` resolves the actual versions, and if anything has
> shipped a breaking change we find out at lock time and adjust.

### 2.2 `packages/core` — no framework, no engine

| Package | Used by | Why this one |
|---|---|---|
| `httpx>=0.27` | `backend/.../mcp/api.py` (the MCP server's client for our own API) | Declared in core, though only the backend imports it; the MCP tests drive it over `httpx.ASGITransport`, so they need no socket |
| `pdfplumber>=0.11` | `portable/document_text.py` | Good text ordering on contracts. **PyMuPDF rejected — AGPL**, wrong licence to ship |
| `jsonschema>=4.22` | `portable/llm_client.py` | Structured LLM output is checked by us, not taken on the vendor's word |
| `google-genai>=1.0` | `portable/llm_client.py` | Default provider — `response_json_schema` |
| `anthropic>=0.34` | `portable/llm_client.py` | Tool-use forced schema |
| `openai>=1.40` | `portable/llm_client.py`, `embeddings/` | `json_schema` strict mode; and `text-embedding-3-small`, which is why this one is needed whatever `LLM_PROVIDER` says |

Six entries were removed from this table in the dead-code sweep, and the reason
is worth keeping: **a dependency table is a claim about what the code does.**
`httpx-sse` and `mcp` named modules that were deleted stubs; `structlog` was
never imported by anything; `python-docx` writes the final document, which is
the backend's job, so `backend` declares it; `pydantic` and `pyyaml` were core declaring the *worker's*
dependencies, which is how `backend` came to use `pyyaml` without declaring it.
Each row above now names a file you can open.

**Optional extra `[ml]`** — the heavy tier:

| Package | Why |
|---|---|
| `torch` (CPU index) | Runtime for the two transformer models |
| `transformers>=4.44` | NLI cross-encoder |
| `gliner>=0.2.13` | Party candidates |

The extra exists so `pip install documents-analyst-core` works for someone who only
wants parsing, Gate 1 and the LLM client. Our image installs `core[ml]`.

### 2.3 `backend`

| Package | Why |
|---|---|
| `documents-analyst-core` | workspace member |
| `fastapi>=0.115` | async API |
| `uvicorn[standard]>=0.30` | ASGI server |
| `sqlalchemy>=2.0.30` | Core layer; async engine for API, sync for worker |
| `psycopg[binary,pool]>=3.2` | **One driver.** LangGraph's checkpointer uses psycopg3 — asyncpg would mean two drivers in one process |
| `pgvector>=0.3` | `Vector` type + `.cosine_distance()` |
| `langgraph>=0.2.28` | State machine with interrupts |
| `langgraph-checkpoint-postgres>=2.0` | Durable checkpoints, same driver |
| `sse-starlette>=2.1` | Our SSE endpoint, with correct disconnect handling |
| `mcp>=1.2` | our 7-tool server |
| `python-docx>=1.1` | Writes `final-document.docx` (`deliverable.py`). Source `.docx` files are read without it — see `CORE_INTERNALS.md` |

**Not used, deliberately:** no LangChain. We call provider SDKs directly and
write our own splitter, because generic text splitters discard character
offsets and Gate 1 depends on them.

### 2.4 Docker — one image, two entrypoints

Confirmed decision. The API process carries torch it never imports, but it is
the same image so there is no extra storage, and `make full-up` stays a
single build from a fresh clone.

```
stage 1  deps      uv sync --frozen --no-install-project --extra ml
stage 2  models    branches from deps; prefetches NLI, GLiNER
stage 3  builder   branches from deps; installs workspace source into the venv
stage 4  runtime   python:3.12-slim + builder venv + models cache, non-root
```

`models` must branch from `deps`, not `builder`: then ordinary application
source edits cannot invalidate the expensive model-prefetch layer.

Stage 2 is not optional. Without it, "offline tests, no live keys" still needs
a network on first run to download models — which quietly breaks the
offline promise. First build runs ~5–10 minutes; every build after that is cached.

### 2.5 Models — where each one runs, and what happens when it can't

| Model | Size | Runtime | Used for |
|---|---|---|---|
| `text-embedding-3-small` | — | **OpenAI API**, asked for at 384 dimensions | Fact-key embeddings — matching open subjects across documents. The only model here that is not local, and the only one that bills per call |
| `MoritzLaurer/DeBERTa-v3-base-mnli-fever-anli` | ~184M | transformers + torch | Tier 2 of the conflict classifier — prose pairs only |
| `knowledgator/gliner-pii-base-v1.0` | ~300M | gliner + torch | Party candidates (preamble only) |
| `LLM_MODEL_EXTRACT` | remote | provider SDK | Fact extraction per section — **the bulk of spend** |
| `LLM_MODEL_JUDGE` | remote | provider SDK | Adjudication, judge rules, party pick, doc-type routing, chunk context, prose summary |

**The `xsmall` checkpoint this table used to name does not exist.** Caught in
Phase 2C when `prefetch_models.py` failed against the live Hub, not by reading.
Both real `deberta-v3-xsmall` NLI variants that MoritzLaurer publishes are
**binary** — `entailment` / `not_entailment` — which collapses neutral and
contradiction into one label. That is precisely the distinction tier 2 exists
to make, so no xsmall model can do this job. `DeBERTa-v3-base-mnli-fever-anli`
is 3-way, widely used, and verified here: *"Payment is due within 30 days"* vs
*"within 45 days"* scores contradiction at 1.00. The cost is size — ~184M
rather than the ~70M this table used to claim.

#### Portability

Everyone builds locally, so each machine produces an image for its own
architecture — no multi-arch publishing needed. `uv.lock` is multi-platform by
default, so one lockfile serves arm64 and x86_64. The only real trap is the
torch index, handled by the marker in §2.1.

#### Degradation — say what broke, keep going where honest

A model that will not load must produce a clear message naming the model, the
env var that controls it, and the capability lost. It must never fail silently
or pretend to work.

| Model unavailable | Behaviour |
|---|---|
| embeddings | **Hard fail.** Open-subject matching is load-bearing and there is no honest fallback. Checked at startup as a missing key, not a network probe |
| NLI | Warn, behave as `NLI_ENABLED=false` — every candidate pair escalates to the LLM. Slower and pricier, still correct |
| GLiNER | Party extraction yields **no candidates** — not "LLM-only". `pick_parties` returns indices into a candidate list, so with no candidates it returns nothing; asking the model to name parties freely would break the Phase 2B guarantee that a party name is always a verbatim span from the document |

### 2.6 Tooling

- **ruff** — lint *and* format. One tool replacing black + isort + flake8.
- **mypy** — non-strict, `packages/core` only. It is the shared surface, so a
  wrong type there propagates into three consumers. Strict typing on
  application wiring generates busywork rather than catching bugs.
- **GitHub Actions** — spin up pgvector, run migrations, run the offline suite.
  A green CI run demonstrates the system proves itself, instead of asking a
  reader to take it on trust.

### 2.7 Configuration

`config/runtime.yaml` carries every tracked, non-secret runtime default.
`.env.example` stays intentionally small: credentials and per-deployment model
choices only.

Genuinely required: `LLM_PROVIDER` (a closed
`Literal["gemini","anthropic","openai"]`, so a typo is rejected at startup
rather than silently taking the wrong provider path), that provider's key, and
`OPENAI_API_KEY` for embeddings whichever provider is chosen.

**Gemini is the default provider.** With both model settings blank it uses
`gemini-3.6-flash` for extraction and judging. That id was checked against the
provider's own model listing — it is offered and supports `generateContent`,
which is what the portable interface's `structured()` needs. `LLM_MODEL_EXTRACT`
and `LLM_MODEL_JUDGE` override it for a pinned deployment. OpenAI and Anthropic
remain supported explicit fallbacks with their own defaults.

Loaded once via `pydantic-settings` into a frozen application `Settings` object.
Application behaviour reads those injected settings; the portable model-cache
helper and model-prefetch script deliberately read only cache-location
environment variables so they can also run outside this application. Every
application knob remains typed, defaulted and discoverable in one file.

Model ids (`LLM_MODEL_EXTRACT`, `LLM_MODEL_JUDGE`) are left blank in the example
so the tested Gemini defaults apply; set them only when pinning a deployment.

**The test suite requires none of it.** `docker compose run --rm worker pytest`
runs with an empty `.env`.

---

## The rest of this design series *(all written)*

These numbers are the **documentation** phases, not the build steps — a
different sequence that happens to share digits. All six are complete.

| Doc phase | Covers |
|---|---|
| 3 | [`CORE_INTERNALS.md`](CORE_INTERNALS.md) — module interfaces, errors, config |
| 4 | [`BACKEND_INTERNALS.md`](BACKEND_INTERNALS.md) — API, worker, graph, repos |
| 5 | [`TESTING.md`](TESTING.md) — DB isolation, fixtures, markers, CI |
| 6 | [`BUILD_AND_RUNTIME.md`](BUILD_AND_RUNTIME.md) — Dockerfile, prefetch, compose |

Also: [`WORKER_RUNTIME.md`](WORKER_RUNTIME.md) (threads and lifecycle) and
[`REPO_STRUCTURE.md`](REPO_STRUCTURE.md) (layout). Do not read the table above
as an implementation order.
