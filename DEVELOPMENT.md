# Development and runtime modes

Use the local-development mode while building features. It keeps Postgres and
pgvector in Docker, while the API, worker, migrations, and tests run from your
local virtual environment. Use the full Docker mode for reviewer parity and CI.

## Local development (recommended)

Prerequisites: Docker Desktop, Python 3.12, and `uv`.

```bash
cp .env.example .env
# Edit config/runtime.yaml for paths, timeouts, scanner and runtime defaults.
make db-up
make migrate-local
make api
```

Open <http://localhost:8000>. `make api` has reload enabled; stop it with
`Ctrl-C`. Run the worker in a second terminal when needed:

```bash
make worker-local
```

The worker discovers and ingests: drop a folder into the watched folder and it
becomes a pile with its documents read, hashed and stored. The watched folder is
`watched/` in this repo unless `WATCH_DIR` in `.env` points somewhere else (an
absolute path, no `~`); the Makefile reads the same variable and passes it to the
host-run targets as `WATCH_ROOT`, so `make api`, `make worker-local` and the
containers all see one folder. With an LLM key set, the same worker runs the
graph over it — extraction, grounding, matching, conflict and amendment
classification, rulebook audit — stops at the human gate, and commits approved
decisions to the register.

Inside one pile the files are ingested and extracted strictly one at a time, in
the order they were found; separate piles run side by side, up to
`pile_concurrency` (`config/runtime.yaml`, default 3) thread lanes per worker.
Set it to 1 while debugging to get one pile after another and a single-threaded
log. See [`docs/WORKER_RUNTIME.md`](docs/WORKER_RUNTIME.md) §1a.

```bash
make seed && make scan-local     # one pass, against the local database
```

`make seed` copies the demo pile into `<WATCH_DIR>/acme`. It refuses to replace
an existing `acme` folder when `WATCH_DIR` is not the repo's own `./watched`, so
it never overwrites a folder of yours.

Local tests use the Docker database when they need one:

```bash
make test-local
make test-db
```

The host reaches the Docker database through:

```text
postgresql+psycopg://analyst:analyst@localhost:5432/documents_analyst
```

### Local-model development *(optional)*

Normal local API/database work needs none of this — the base venv has no torch
and downloads no weights. This path exists only for debugging model code on the
host; the Docker image remains the canonical runtime.

```bash
uv sync --extra ml                                          # torch, transformers, gliner
uv run python scripts/prefetch_models.py --dir .cache/models   # ~2.2GB, once
```

`config/runtime.yaml` already points host runs at `.cache/models`, so run
`make worker-local` normally once that prefetch finishes. To use a different
cache location, edit `model_cache_dir` in that file. A real process
`MODEL_CACHE_DIR` environment variable is still a deployment override.

`documents_analyst_core/model_cache.py` derives `HF_HOME` and `HUGGINGFACE_HUB_CACHE` from it,
and the model loaders fall back to the same layout when given no explicit
directory — so a host worker reads the weights you prefetched instead of
quietly downloading a second copy into `~/.cache`. That second copy is exactly
what happened before this was wired, and nothing failed: it just downloaded
again.

Check what is there without downloading anything:

```bash
uv run python scripts/prefetch_models.py --check --dir .cache/models
```

It reports `populated` only when **both local models** are present, and names
the missing ones otherwise. The host cache and the image's `/opt/models` are
deliberately separate: a host download never makes a container runnable, and a
container build never touches your host cache.

## Full Docker stack

This is the reproducible path for a reviewer or CI:

```bash
cp .env.example .env
make full-up
```

Or, without Make:

```bash
docker compose --profile full up --build
```

The `full` profile starts `db`, one-shot `migrate`, `api`, and the worker. Both
mount `${WATCH_DIR:-./watched}` at `/data/watched`. The
worker scans `WATCH_ROOT` on its timer, ingests settled files, and runs the
graph through matching, conflict classification, rulebook audit, the human
gate and the final-document commit when an LLM key is configured.
Inside that Docker network the application uses `db:5432`, so Compose overrides
the local `DATABASE_URL` from `.env` automatically.

This is also the canonical local-model path: the build prefetches GLiNER and
NLI into `/opt/models`, and the runtime loads them from that baked cache with
`HF_HUB_OFFLINE=1` — so a missing weight fails loudly instead of quietly
reaching for the network. OpenAI embeddings are API-only and are therefore not
included in the model cache. Verified with `docker run --network none`.

Run the full test suite in the same image with:

```bash
make test
```

## Useful commands

```bash
curl -fsS http://localhost:8000/health | python3 -m json.tool   # incl. watch: path, pile_concurrency
make migrate-status     # migration ledger through Docker
docker compose exec db psql -U analyst -d documents_analyst
make down               # stop Docker services; preserves database data
```

`make down` never removes the database volume. Use Docker volume removal only
when you deliberately want to discard local development data.
