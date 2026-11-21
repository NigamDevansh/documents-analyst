# Build and runtime

This document records the runtime as built. The Dockerfile has deps, models,
builder, frontend and runtime stages.

For the supported local-development and full-Docker commands, see
[`DEVELOPMENT.md`](../DEVELOPMENT.md). Docker Compose now starts only Postgres
by default; the application services use the explicit `full` profile.

Companion to [`CODE_ARCHITECTURE.md`](CODE_ARCHITECTURE.md),
[`REPO_STRUCTURE.md`](REPO_STRUCTURE.md),
[`WORKER_RUNTIME.md`](WORKER_RUNTIME.md) and [`TESTING.md`](TESTING.md).

---

## 1. The target

```bash
git clone …
cp .env.example .env
# Edit config/runtime.yaml for shared non-secret behaviour.
docker compose --profile full up --build
```

Open one URL. That is the whole setup — one command from a fresh clone — and
everything below is in service of it.

---

## 2. Dockerfile — five stages

```dockerfile
# ── 1 · deps ──────────────────────────────────────────────
FROM python:3.12-slim AS deps                 # NO application code, ever
  install uv
  COPY pyproject.toml uv.lock packages/core/pyproject.toml backend/pyproject.toml
  uv sync --frozen --no-install-project --extra ml

# ── 2 · models (built, Phase 2C) ───────────────────────────
FROM deps AS models                           # NOT `FROM builder` — see below
  ENV HF_HOME / HUGGINGFACE_HUB_CACHE → /opt/models
  COPY scripts/prefetch_models.py + documents_analyst_core/model_cache.py   # two files only
  RUN python scripts/prefetch_models.py --dir /opt/models

# ── 3 · builder ───────────────────────────────────────────
FROM deps AS builder                          # source lands here and nowhere earlier
  COPY packages/ backend/
  uv sync --frozen --extra ml

# ── 3b · frontend (built) ───────────────────────────────────
FROM node:24-slim AS frontend
  COPY frontend/package*.json  →  npm ci
  COPY frontend/  →  npm run build      # → /ui/dist  (304K)

# ── 4 · runtime ───────────────────────────────────────────
FROM python:3.12-slim
  COPY --from=builder  /app/.venv       /app/.venv
  COPY --from=models   /opt/models      /opt/models
  COPY --from=frontend /ui/dist         /app/static
  COPY packages/ backend/ db/ scripts/
  USER app                               # non-root
```

**Layer order is the whole trick.** Dependencies resolve before any application
code is copied, so editing a Python file does not re-download torch. Models come
next, so editing code does not re-download a 300M-parameter model either. A cold
build is 5–10 minutes; every build after that is a couple of minutes.

### The model stage — built in Phase 2C

`scripts/prefetch_models.py` downloads the NLI cross-encoder and
GLiNER **at build time** into `/opt/models`, and the runtime stage points
`HF_HOME` and `HUGGINGFACE_HUB_CACHE` at the same tree. Embeddings used to be a
third download; they are an OpenAI call now, so there is nothing to bake.

**One module owns that layout**, `documents_analyst_core/model_cache.py`, and a test
asserts the Dockerfile's exported variables equal what it computes. That guard
exists because the first version did not have it: the script wrote HF weights
to `<root>/hf` while HuggingFace derives its cache as `$HF_HOME/hub`, so the
image baked 2.2GB at build time and would have downloaded them all again at
runtime. Nothing failed — a second download just works, slowly.

`HF_HUB_OFFLINE=1` is set in the runtime stage so a missing model fails loudly
instead of quietly reaching for the network. Verified the way it is claimed:

```bash
docker run --rm --network none documents-analyst-worker pytest -q
# 289 passed, 25 skipped — fully air-gapped
```

**Image 5.0GB, cold build ~10 min, rebuild ~2 min.**

The models stage branches from `deps`, which carries **no application code** —
that is what makes "editing a Python file never re-downloads 2.2GB" true. An
earlier version had it branch from `builder`, which copies `packages/` and
`backend/`, so a one-character source edit re-ran `uv sync --extra ml` and the
full prefetch while this document claimed the opposite. Caught by touching a
file and watching the download start again; `test_the_models_stage_cannot_see_application_code`
now asserts the ordering.

This Docker-baked cache is the **canonical reviewer and delivery path**. There
is an optional host-side path for debugging model code: install the ML extra,
prefetch into a project-local `.cache/models/`, and set **one** variable.

```bash
uv sync --extra ml
uv run python scripts/prefetch_models.py --dir .cache/models
make worker-local                                  # reads config/runtime.yaml
```

`model_cache_dir` in `config/runtime.yaml` is the host-default knob.
`documents_analyst_core/model_cache.py` derives
`HF_HOME` and `HUGGINGFACE_HUB_CACHE` from it, and
`documents_analyst/models.py` exports them at startup so the `.env` form behaves exactly
like the inline one. Setting the three separately is no longer the documented
route — that divergence is how the layout drifted in the first place.

The two caches are deliberately separate; a host download never makes a
container runnable, and a container build never modifies a developer's host
cache.

The host-side path is not required for normal local API/database work. It
exists only when actively debugging local-model code; the reproducible Docker
path remains the one a reviewer runs.

spaCy and Presidio are **not** baked here: nothing imports them.

Without this, "offline tests, no live keys" still needs a network on first run —
which quietly breaks the offline promise while appearing to keep it.

### Frontend is built in, not served separately

Stage 3 runs `vite build` and the bundle lands at `/app/static`, served by
FastAPI. One URL, no CORS, one fewer compose service.

A `dev` profile adds a Vite dev server with hot reload for when we are building
it — but the default path is the one a reviewer takes.

---

## 3. Compose

| Service | Image | Command | Profile |
|---|---|---|---|
| `db` | `pgvector/pgvector:pg16` | — | default |
| `migrate` | ours | `python -m documents_analyst.db.migrate` | `full` |
| `api` | ours | `uvicorn documents_analyst.api.main:create_app --factory` | `full` |
| `worker` | ours | `python -m documents_analyst.worker.main` | `full` — scans, ingests, runs the graph, writes each pile's `final-document.docx` |
| `mcp` | ours | `python -m documents_analyst.mcp.server --transport stdio` | `mcp` |
| `mcp-http` | ours | the same, `--transport streamable-http`, published to `127.0.0.1:8765` | `mcp-http` |
| `tests` | ours | `pytest`, with no `env_file` | `full` |
| `pgweb` | `sosedoff/pgweb` | a browser for the database on `:8081` | default |

`docker compose --profile full up` starts the database, `migrate`, `api` and
`worker`. The MCP services are opt-in, so the base experience stays light.

**Why the application services sit behind a profile.** A bare `docker compose
up` starts Postgres alone, which is the state day-to-day development wants:
`api` and `worker` run on the host with reload and a debugger attached, against
the same containerised pgvector the full stack uses. A reviewer never types
that form — `make full-up` is the documented entry point and it passes
`--profile full`. Both modes are written up in
[`DEVELOPMENT.md`](../DEVELOPMENT.md).

### Startup order

```
db  (healthcheck: pg_isready)
 └─► migrate   (runs once, exits 0)
      └─► api
      └─► worker (scans and ingests)
```

`migrate` is its own one-shot service, and both api and worker declare
`depends_on: { migrate: { condition: service_completed_successfully } }`.

Why not run migrations from the api on startup: with more than one worker or a
restart loop, two processes race the same DDL. A one-shot service makes "the
schema is ready" a fact both can depend on rather than something each hopes it
won it.

### Volumes

```yaml
./watched:/data/watched      # sources in; final-document.docx written per pile
```

One mount, shared by `api` and `worker`. The worker writes each pile's final
document into that pile's own folder (`watched/<pile>/final-document.docx`);
the API serves it from there, resolving the path by pile name under its own
`WATCH_ROOT`, so the container path and the host path never have to agree.

We never edit or delete an existing contract in `watched/`. The API's document
upload writes a new file there, then discovery remains the single owner of
hashing, deduplication, aliases, quarantining, and the document row. The only
file the system itself writes is the final document, and the scanner skips
exactly that name at the pile root.

### Postgres settings

```
idle_in_transaction_session_timeout = 60s
```

Set on the `db` service. A worker frozen mid-transaction otherwise holds row
locks and blocks the very takeover the lease exists to allow
(see `WORKER_RUNTIME.md` §7a).

---

## 4. A folder is a pile

Every subdirectory of `WATCH_ROOT` is automatically a pile.

```
watched/
├── acme/                 → pile "acme"
│   ├── rulebook.yaml
│   ├── 01_msa.pdf
│   ├── 02_sow.docx
│   └── final-document.docx   ← written by the system after review; never ingested
└── cobalt/               → pile "cobalt"
```

No creation step, no config file, no API call. Drop a folder in, and the next
scan picks it up — the same reconciliation pass that finds new documents finds
new piles.

```bash
make seed        # copies fixtures/conflict_pile → watched/acme
```

If a folder has no `rulebook.yaml`, the pile still works; the audit stage simply
has no rules to run and produces zero findings.

---

## 5. Running with no keys

`docker compose --profile full up` with an untouched `.env` **starts normally**. Nothing exits,
nothing crashes. The worker scans and ingests, and runs extraction, conflict
classification and rulebook audits once its LLM key is set; the dashboard loads
and names what is missing.

The dashboard shows a banner; behind it, the API returns the same
missing-configuration information as structured JSON:

> **Missing configuration.** `GEMINI_API_KEY` and `OPENAI_API_KEY` are not set.
> Documents will be scanned and parsed, but fact extraction, adjudication and
> open-subject matching are disabled. Add them to `.env` and restart.

| Works without keys | Needs keys |
|---|---|
| Scanning, hashing, settle checks | Fact extraction (LLM) |
| Parsing to text, chunking | Conflict adjudication (LLM tier 3) |
| The dashboard, piles, documents | Open-subject matching (embeddings — `OPENAI_API_KEY`) |
| The whole test suite | The final document — it is written after a run that extracted something |

Embeddings run against the **OpenAI API** whatever `LLM_PROVIDER` is, so
open-subject matching across documents needs `OPENAI_API_KEY` even on a
Gemini or Anthropic deployment. Taxonomy-keyed facts still match without it;
only free-subject matching is lost.

Startup validates configuration and reports **everything** missing at once —
not the first problem, then the next after a restart.

The reasoning: someone who runs `make full-up` before reading the README should
get a working page that tells them what to do. A container that exits
immediately reads as "this is broken", and failing at the first LLM call buries
the cause in worker logs minutes later.

---

## 6. Entrypoints

One image, selected by command:

```
uvicorn documents_analyst.api.main:create_app --factory        # api
python -m documents_analyst.worker.main       # worker
python -m documents_analyst.mcp.server        # mcp
python -m documents_analyst.db.migrate        # migrate (one-shot)
pytest                              # tests
```

On SIGTERM, the worker stops before acquiring another pile or starting another
document, then releases its live lease in `finally`. A hash or parse already in
progress is allowed to return first; if Docker must kill a pathological
operation, the lease TTL and epoch/TTL-fenced writes make a later takeover safe.
Graph nodes renew at bounded model-work boundaries and checkpoint their
human-gate pause in Postgres. A worker killed inside `Commit` leaves at worst a
file ahead of its record; the record rolled back, and the next run records the
same sections once.

---

## 7. Make targets

```
make up          ← start here: build, migrate, start everything, open the dashboard
make db-up       start only Docker Postgres for local development
make full-up     the complete Docker stack in the foreground
make down        stop everything `make up` started; keeps the database volume
make seed        copy a fixture pile into watched/
make run         discover AND run the graph, in the container with the models
make test        run the full offline suite
make test-fast   pytest -m "not models"
make fmt         ruff format + autofix
make lint        ruff, no autofix
make types       mypy over core and backend
make demo-crash  damage final-document.docx, restart, watch Resolve restore it
make demo-api    drive the whole workflow over HTTP (needs `make api` running)
make mcp         the MCP server on stdio, against a local API
make mcp-tools   list the seven tools and their arguments, without a client
make ui          dashboard dev server on :5173, API proxied
make ui-build    build the dashboard the API serves at /
make ui-test     the dashboard's own unit tests
make logs        docker compose logs -f worker
```

`make demo-api` changes real state — an approval through it is a real approval,
and a running worker will commit it. That is deliberate: it is the same door the
dashboard and the MCP server use, not a read-only tour.

**One trap worth knowing about.** `make run` reuses whatever worker image
exists — only `make test` passes `--build`. After changing worker code, rebuild
first, or you will watch old code do nothing convincingly.

**A pile's `watch_path` is recorded by whoever discovered it.** A pile first
scanned inside the container holds `/data/watched/acme`, which a host-run API
cannot write to; uploads then fail with a 409 naming the path. Run both halves
on the same side, or rescan from the side you intend to use.

---

## 8. What a reviewer actually does

```bash
git clone …
cp .env.example .env
docker compose --profile full up --build
                                      # open http://localhost:8000
```

Then, in another terminal:

```bash
make test                             # full suite, no keys needed
make seed                             # a pile appears in the UI
make demo-crash                       # damage the final document, watch it restored
```

And with an API key added, driving it headlessly:

```bash
claude mcp add documents-analyst -- docker compose run --rm -T mcp
```

---

## 9. Known build constraints

| Constraint | Detail |
|---|---|
| Cold build 5–10 min | torch plus ~2.2GB of model weights. Cached after the first |
| Image ~5GB | torch plus 2.2GB of baked weights. Down from 10.4GB once the torch CPU index stopped being scoped to `x86_64` — see `CODE_ARCHITECTURE.md` §2.1. int8 ONNX export is the known next optimisation |
| arm64 vs x86_64 | Everyone builds locally so each gets their own architecture. The CPU index applies to all Linux architectures; macOS resolves its normal platform wheel from PyPI |
| No published image | Nothing to pull; the build is the setup |
