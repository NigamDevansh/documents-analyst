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

Or `make up`, which also creates `.env` from `.env.example` and seeds the demo
pile. Open one URL. That is the whole setup — one command from a fresh clone — and
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
  ENV HF_HOME=/opt/models/hf  HUGGINGFACE_HUB_CACHE=/opt/models/hf/hub  MODEL_CACHE_DIR=/opt/models
  COPY scripts/prefetch_models.py + documents_analyst_core/model_cache.py   # two files only
  RUN python scripts/prefetch_models.py --dir /opt/models

# ── 3 · builder ───────────────────────────────────────────
FROM deps AS builder                          # source lands here and nowhere earlier
  COPY packages/ backend/
  uv sync --frozen --extra ml

# ── 3b · frontend (built) ───────────────────────────────────
FROM node:24-slim AS frontend
  COPY frontend/package.json frontend/package-lock.json  →  npm ci
  COPY frontend/  →  npm run build      # → /ui/dist

# ── 4 · runtime ───────────────────────────────────────────
FROM python:3.12-slim
  ENV HF_HOME / HUGGINGFACE_HUB_CACHE / MODEL_CACHE_DIR  (as above)
      WATCH_ROOT=/data/watched  STATIC_DIR=/app/static  HF_HUB_OFFLINE=1
  COPY --from=builder  /app/.venv       /app/.venv
  COPY --from=models   /opt/models      /opt/models
  COPY --from=frontend /ui/dist         /app/static
  COPY pyproject.toml .env.example config/ Dockerfile docker-compose.yml
  COPY packages/ backend/ db/ scripts/ fixtures/ tests/
  USER app                               # non-root, uid 10001
  CMD uvicorn documents_analyst.api.main:create_app --factory   # compose overrides per service
```

**Layer order is the whole trick.** Dependencies resolve before any application
code is copied, so editing a Python file does not re-download torch. Models come
next, so editing code does not re-download a 300M-parameter model either. A cold
build is 5–10 minutes; every build after that is a couple of minutes.

### The model stage — built in Phase 2C

`scripts/prefetch_models.py` downloads the NLI cross-encoder and
GLiNER **at build time** into `/opt/models`, and the runtime stage points
`HF_HOME` and `HUGGINGFACE_HUB_CACHE` at the same tree. Embeddings used to be a
third download; they now use the OpenAI or Gemini API, so there is nothing to bake.

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
# the offline suite, fully air-gapped
```

The image carries `Dockerfile`, `docker-compose.yml`, `fixtures/` and `tests/`
because tests read them where the suite actually runs (`make test` runs inside
this image).

**Image layers ~3.8GB (venv ~1.5GB, weights ~2.25GB); Docker Desktop's
`docker images` reports ~6.4GB disk usage. Cold build ~10 min, rebuild ~2 min.**

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

Stage 3b runs `npm run build` (Vite) and the bundle lands at `/app/static`
(`STATIC_DIR`), served by FastAPI: `/` returns its `index.html` and `/assets`
its hashed files, including the tab icons built from `frontend/src/assets/`
(`icon.svg`, `icon-32.png`, `apple-touch-icon.png`). One URL, no CORS, one fewer
compose service. If `STATIC_DIR` holds no `index.html` (a host-run API before
`make ui-build`), `/` serves a small status page instead, named and iconed from
`api/brand.py` ("Document Analyst"), linking `/health` and `/docs`.

`make ui` runs the host Vite dev server with hot reload. There is no Compose
`dev` profile; the built-in bundle is the normal reviewer path.

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
| `tests` | ours | `pytest`, with no `env_file` | `tests` (explicit selection only) |
| `pgweb` | `sosedoff/pgweb` | a browser for the database on `:8081` | default |

`docker compose --profile full up` starts the database, `migrate`, `api`,
`worker` and `pgweb`; `make up` starts the first four only, detached, and waits
for them (`--wait`). The MCP services are opt-in, so the base experience stays
light.

**Why the application services sit behind a profile.** A bare `docker compose
up` starts Postgres (and `pgweb`) alone, which is the state day-to-day development wants:
`api` and `worker` run on the host with reload and a debugger attached, against
the same containerised pgvector the full stack uses. A reviewer never types
that form — `make up` is the documented entry point (`make full-up` runs the
same stack in the foreground), and both pass `--profile full`. Both modes are written up in
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
${WATCH_DIR:-./watched}:/data/watched   # sources in; final-document.docx written per pile
```

One mount, shared by `api` and `worker`. **`WATCH_DIR` in `.env` chooses the
host folder** — `./watched` inside the repo by default, or any folder on your
machine as an absolute path (no `~`: Compose does not expand it). You never move
the repo and never pass individual files; the system watches that one folder,
and each subfolder of it is one pile. `WATCH_DIR` is a Compose-only variable:
it is listed in `COMPOSE_ONLY_FIELDS` in `settings.py`, and `Settings` ignores
it. Inside the containers the folder is always `/data/watched` (`WATCH_ROOT`).

Compose also passes `WATCH_DIR_HOST=${WATCH_DIR:-./watched}` to both services,
which lands in the display-only setting `watch_dir_host`. Nothing reads files
through it; it exists so `/health` and the dashboard's setup guide can show the
folder as you see it on the host instead of `/data/watched`.

The worker writes each pile's final document into that pile's own folder
(`<WATCH_DIR>/<pile>/final-document.docx`); the API serves it from there,
resolving the path by pile name under its own `WATCH_ROOT`, so the container
path and the host path never have to agree.

The Makefile reads `WATCH_DIR` from `.env` too (default `./watched`), and the
host-run targets point `WATCH_ROOT` at it (`LOCAL_PATHS`), so `make api`,
`make scan-local` and the containers see the same files. `make up` seeds the
demo pile `acme` only when `WATCH_DIR` is the default `./watched` — never into a
folder of yours — and `make seed` refuses to replace an existing `<WATCH_DIR>/acme`
outside the repo's own `watched/`.

The system never edits or deletes a contract in the watched folder. The upload
endpoint, `POST /piles/{pile_id}/documents`, writes the file into the pile's
folder (a unique temporary dotfile, then an atomic rename) and wakes the worker;
an upload with a name already in the folder replaces that file, which the
scanner reads as a new version. Discovery remains the single owner of hashing,
deduplication, aliases, quarantining, and the document row. The only
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

Every subdirectory of `WATCH_ROOT` (on the host, `WATCH_DIR`) is
automatically a pile, with its own run, review queue, history and
`final-document.docx`. Piles are never compared with each other.

```
watched/                  (or your WATCH_DIR)
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
make seed        # copies fixtures/conflict_pile → <WATCH_DIR>/acme
```

Separate piles are processed in parallel, up to `pile_concurrency` per worker
(`config/runtime.yaml`, default 3), one thread each; the files inside one pile
are always taken one at a time, in the order they were found
([`WORKER_RUNTIME.md`](WORKER_RUNTIME.md) §1a).

If a folder has no `rulebook.yaml`, the pile still works; the audit stage simply
has no rules to run and produces zero findings. The name is exact: a
`rulebook.yml`, `.json` or `.txt` is not applied, and the worker logs a one-time
warning saying so. OS files (`Thumbs.db`, `ehthumbs.db`, `desktop.ini`, macOS
`Icon\r`) are ignored, not quarantined.

---

## 5. Setup screen and provider selection

`make up` with an untouched `.env` **starts normally**. The worker scans and
ingests files, but the graph waits until both an LLM key and an embedding key
are configured. The dashboard shows `SetupGate` instead of the workspace.

`GET /health` exposes `setup.ready` and `setup.requirements`: each requirement
names its purpose, acceptable keys, whether it is met, the selected provider,
how it was selected, and the model names. It reports key presence, never key
values. The screen polls this endpoint every four seconds and also offers
**Check again**. Save credentials in `.env` and rerun `make up` to recreate the
containers with the new environment; the screen then unlocks automatically.

| Capability | Automatic selection when the provider setting is blank | Explicit setting |
|---|---|---|
| Extraction and judging | Claude if `ANTHROPIC_API_KEY` is set, else Gemini if `GEMINI_API_KEY` is set, else OpenAI | `LLM_PROVIDER=anthropic`, `gemini`, or `openai` |
| Embeddings | OpenAI if `OPENAI_API_KEY` is set, otherwise Gemini if `GEMINI_API_KEY` is set; defaults to OpenAI when neither is set | `EMBEDDING_PROVIDER=openai` or `gemini` |

One OpenAI key or one Gemini key satisfies both requirements. Claude has no
embedding model, so a Claude deployment also needs an OpenAI or Gemini key.
Explicit selection requires that provider's key; another provider's key does
not silently replace it. Blank model overrides use the selected provider's defaults.

| Works without keys | Waits for both requirements |
|---|---|
| Discovery, hashing, settle checks, parsing and quarantine | Extraction, matching, classification and rulebook audits |
| Setup screen and health endpoint; API reads remain available subject to `API_TOKEN` | Dashboard workspace |
| Local restoration of an already recorded final document | The first analysis that supplies sections for a final document |
| Offline test suite | Live provider tests |

### Prices

`config/model_prices.json` converts tokens to dollars for the cost report; it
never blocks a call and there is no spending limit. A model with no entry costs
0 and is reported as unpriced. `pricing.price_warnings()` flags the configured
models (extraction, judge, and embeddings when an embedding key is set) with no
price, or a price past or within 14 days of its `valid_until`, plus an unreadable
price file; the warnings appear on `/health` (`pricing.warnings`), in the
worker's startup log and on the dashboard's Cost tab, and never block a run.
Each run's closing timeline event carries what it spent ("Finished — this run
cost $X"). See `config/MODEL_PRICING.md`.

### Embeddings

Embeddings use `text-embedding-3-small` or `gemini-embedding-001`, requested at
384 dimensions for the database's `vector(384)` column. Gemini vectors are
normalised locally and its token usage is estimated. They are remote API calls,
separate from the two baked local models. See [`CORE_INTERNALS.md`](CORE_INTERNALS.md)
for provider changes and stored vector compatibility.

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
make up            ← start here: create .env if missing, seed the demo pile, build,
                     start db + migrate + api + worker detached, print the URLs
make db-up         start only Docker Postgres (and pgweb) for local development — the default goal
make full-up       the complete Docker stack in the foreground
make down          stop everything `make up` started; keeps the database volume
make full-down     stop the stack AND delete the database volume
make build         rebuild the application image
make seed          copy the demo pile into <WATCH_DIR>/acme (won't replace one of yours)
make run           discover AND run the graph once, in the container with the models
make migrate       apply migrations through Docker      (migrate-local: from the host venv)
make api           the API on the host with reload, serving frontend/dist
make worker-local  the worker on the host
make scan-local    one discovery pass on the host — no models, no LLM key
make test          the full offline suite in the `tests` service (no .env)
make test-fast     the same, pytest -m "not models"
make test-local    pytest from the host venv against local Postgres
make test-db       the schema-drift suite against a live Postgres
make fmt           ruff format + autofix
make lint          ruff, no autofix
make types         mypy over core and backend
make demo-crash    damage final-document.docx, restart, watch Resolve restore it
make demo-api      drive the whole workflow over HTTP (needs `make api` running)
make mcp           the MCP server on stdio, against a local API
make mcp-tools     list the seven tools and their arguments, without a client
make ui            dashboard dev server on :5173, API proxied
make ui-build      build the dashboard the API serves at /
make ui-test       the dashboard's own unit tests
make logs          docker compose logs -f worker
```

`make demo-api` changes real state — an approval through it is a real approval,
and a running worker will commit it. That is deliberate: it is the same door the
dashboard and the MCP server use, not a read-only tour.

`make run`, `make test` and `make test-fast` pass `--build`, so they rebuild
changed source before launching their one-shot container.

CI (`.github/workflows/ci.yml`) runs the dashboard checks (`npm test`, the
build, Playwright e2e), then the same `tests` service with `--build`: ruff,
`ruff format --check`, mypy, the fixture checks and `pytest -m "not live"`.

**A pile's `watch_path` is recorded by whoever discovered it.** A pile first
scanned inside the container holds `/data/watched/acme`, which a host-run API
cannot write to (even though it is the same `WATCH_DIR` on disk); uploads then fail with a 409 naming the path. Run both halves
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
make demo-crash                       # once a final document is committed: damage it, watch it restored
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
| Image ~3.8GB of layers | torch (~1.5GB venv) plus ~2.25GB of baked weights; Docker Desktop reports ~6.4GB disk usage. Down from 10.4GB once the torch CPU index stopped being scoped to `x86_64` — see `CODE_ARCHITECTURE.md` §2.1. int8 ONNX export is the known next optimisation |
| arm64 vs x86_64 | Everyone builds locally so each gets their own architecture. The CPU index applies to all Linux architectures; macOS resolves its normal platform wheel from PyPI |
| No published image | Nothing to pull; the build is the setup |
