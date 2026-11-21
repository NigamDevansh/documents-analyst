# Small, common development and verification shortcuts.

.DEFAULT_GOAL := db-up
.PHONY: up db-up full-up full-down build down logs migrate migrate-local api worker-local seed scan-local run run-local demo-crash demo-api mcp mcp-tools ui ui-build ui-test test test-fast test-local fmt lint types fixtures fixtures-check clean migrate-status test-db

COMPOSE := docker compose
FULL_COMPOSE := $(COMPOSE) --profile full
LOCAL_DATABASE_URL ?= postgresql+psycopg://analyst:analyst@localhost:5432/documents_analyst
LOCAL_API_URL ?= http://localhost:8000
# The folder whose subfolders are piles. Read from WATCH_DIR in `.env` — the same
# line Docker Compose reads for the mount — so `make` and compose never disagree.
WATCH_DIR ?= $(or $(shell sed -n 's/^WATCH_DIR=//p' .env 2>/dev/null | tail -1),./watched)

# ── the one command ───────────────────────────────────────────────────────
# A stranger clones the repo and runs `make up`. Nothing else is required —
# not a key, not a Python install, not a database. The stack builds, migrates,
# starts, and a demo pile is already in `watched/` for the worker to find.
#
# **It must work with no `.env`.** A reviewer's first run has no credentials,
# and the honest behaviour is a system that starts and says what is missing:
# the dashboard loads, the banner names each absent key and what it costs, and
# the worker discovers and ingests documents while refusing to run the graph.
# Add a key, and the same pile carries on from where it stopped.
up:  ## ← START HERE. Build, migrate, start everything, open the dashboard.
	@test -f .env || (cp .env.example .env && \
	  echo "created .env from .env.example — add keys there when you have them")
	@mkdir -p "$(WATCH_DIR)"
	@# The demo pile goes only into the repo's own watched/ — never into a folder
	@# of yours that WATCH_DIR points at.
	@if [ "$(WATCH_DIR)" = "./watched" ]; then \
	  test -d watched/acme || cp -r fixtures/conflict_pile watched/acme; fi
	$(FULL_COMPOSE) up -d --build --wait db migrate api worker
	@echo
	@echo "  watching    $(WATCH_DIR)  (one subfolder per pile)"
	@echo "  dashboard   http://localhost:8000"
	@echo "  database    http://localhost:8081  (make full-up adds pgweb)"
	@echo "  logs        make logs"
	@echo "  stop        make down"
	@echo

full-up:  ## run the complete Docker stack in the foreground
	$(FULL_COMPOSE) up --build

full-down:
	$(FULL_COMPOSE) down -v --remove-orphans

build:  ## rebuild the full-stack application image
	$(FULL_COMPOSE) build

# --wait blocks on the healthcheck. Without it this returns in 0.2s while initdb
# is still running on a fresh volume, and the very next documented command —
# `make migrate-local` — fails with connection refused.
db-up:  ## start Postgres/pgvector for locally run services
	$(COMPOSE) up -d --wait db pgweb

down:  ## stop everything `make up` started. Keeps the database volume.
	$(FULL_COMPOSE) down

logs:  ## follow the full-stack worker log
	$(FULL_COMPOSE) logs -f worker

migrate:  ## apply migrations through the full Docker stack
	$(FULL_COMPOSE) run --rm migrate

migrate-local:  ## apply migrations from the local virtual environment
	DATABASE_URL=$(LOCAL_DATABASE_URL) uv run python -m documents_analyst.db.migrate

# Every path default in `Settings` is the one that is right INSIDE the image —
# /app/static for the bundle, /data/watched for the piles — because that is where
# the Dockerfile and compose put them. A process run on the host has to be told
# the host equivalents, and being told only some of them is worse than none:
# `make ui-build` then succeeded while localhost:8000 served "dashboard is not
# built". compose mounts $(WATCH_DIR) to /data/watched, so these are the same files.
LOCAL_PATHS := STATIC_DIR=./frontend/dist WATCH_ROOT=$(WATCH_DIR) WATCH_DIR_HOST=$(WATCH_DIR)

api:  ## run the API locally with reload; serves frontend/dist (start db first)
	DATABASE_URL=$(LOCAL_DATABASE_URL) $(LOCAL_PATHS) \
	  uv run uvicorn documents_analyst.api.main:create_app --factory --reload

worker-local:  ## run the worker locally — scans every SCAN_INTERVAL_S
	DATABASE_URL=$(LOCAL_DATABASE_URL) $(LOCAL_PATHS) uv run python -m documents_analyst.worker.main

# ── demo ──────────────────────────────────────────────────────────────────
seed:  ## copy the demo pile into WATCH_DIR/acme — the next scan picks it up
	@mkdir -p "$(WATCH_DIR)"
	@test ! -e "$(WATCH_DIR)/acme" || [ "$(WATCH_DIR)" = "./watched" ] || \
	  (echo "$(WATCH_DIR)/acme already exists and is not the repo's demo folder — not replacing it"; exit 1)
	@rm -rf "$(WATCH_DIR)/acme"
	cp -r fixtures/conflict_pile "$(WATCH_DIR)/acme"
	@echo "$(WATCH_DIR)/acme created. Run: make scan-local"

# Waits out SETTLE_SECONDS first. Files copied a moment ago are deliberately
# skipped as "still being written", so running this straight after `make seed`
# would honestly report nothing — which reads as broken rather than as the
# safety check it is.
scan-local:  ## discovery only — no models, no LLM key needed
	@echo "waiting 4s for copied files to settle…"
	@sleep 4
	DATABASE_URL=$(LOCAL_DATABASE_URL) $(LOCAL_PATHS) \
	  uv run python -m documents_analyst.worker.main --once --scan-only

# The graph needs the local models, which are baked into the image rather than
# installed on the host. `make run` goes through the container for that reason;
# `make run-local` works only if you fetched them yourself (see config/runtime.yaml).
run:  ## discover AND run the graph, in the container that has the models
	@echo "waiting 4s for copied files to settle…"
	@sleep 4
	$(FULL_COMPOSE) run --rm --build worker python -m documents_analyst.worker.main --once

run-local:  ## same, on the host — needs MODEL_CACHE_DIR populated
	@echo "waiting 4s for copied files to settle…"
	@sleep 4
	DATABASE_URL=$(LOCAL_DATABASE_URL) $(LOCAL_PATHS) \
	  uv run python -m documents_analyst.worker.main --once

demo-crash:  ## damage final-document.docx, restart, watch Resolve restore it from the record
	DATABASE_URL=$(LOCAL_DATABASE_URL) ./scripts/demo_crash.sh

demo-api:  ## drive the whole workflow over HTTP — needs `make api` running
	./scripts/demo_api.sh

mcp:  ## run the MCP server on stdio, against a local API — for `claude mcp add`
	MCP_API_BASE_URL=$(LOCAL_API_URL) uv run python -m documents_analyst.mcp.server --transport stdio

ui:  ## dashboard dev server with hot reload, proxying the API (start `make api` first)
	cd frontend && npm install --silent && npm run dev

ui-build:  ## build the dashboard into frontend/dist; `make api` serves it from there
	cd frontend && npm install --silent && npm run build
	@echo
	@echo "Built. Run 'make api' and open http://localhost:8000 — it reads"
	@echo "frontend/dist. (In Docker the image copies it to /app/static instead.)"

ui-test:  ## the dashboard's own unit tests
	cd frontend && npm run test

mcp-tools:  ## list the seven tools and their arguments, without a client
	MCP_API_BASE_URL=$(LOCAL_API_URL) uv run python -m documents_analyst.mcp.tools

# ── tests and quality ─────────────────────────────────────────────────────
# --build, because `compose run` reuses whatever image exists. Without it this
# happily tests the code as it was at the last build and reports it as green.
test:  ## full suite in the full Docker image — no API keys needed
	$(FULL_COMPOSE) run --rm --build tests

test-fast:  ## skip anything that loads a local model
	$(FULL_COMPOSE) run --rm --build tests pytest -m "not models"

test-local:  ## run tests from the local virtual environment
	DATABASE_URL=$(LOCAL_DATABASE_URL) uv run pytest

fmt:  ## ruff format + autofix
	uv run ruff format .
	uv run ruff check --fix .

lint:  ## ruff, no autofix
	uv run ruff check .

types:  ## mypy over core AND backend
	uv run mypy

# ── fixtures ──────────────────────────────────────────────────
fixtures:  ## rebuild .docx fixtures from .md (real heading styles)
	uv run python scripts/build_fixture_docs.py
	uv run python scripts/verify_fixtures.py

fixtures-check:  ## verify fixtures without rewriting them
	uv run python scripts/build_fixture_docs.py --check
	uv run python scripts/verify_fixtures.py


# ── housekeeping ──────────────────────────────────────────────────────────
clean:  ## remove caches and build artefacts
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
	rm -rf .pytest_cache .ruff_cache .mypy_cache


migrate-status:  ## which migrations are applied
	$(FULL_COMPOSE) run --rm migrate python -m documents_analyst.db.migrate --status


test-db:  ## schema-drift suite against a live Postgres
	DATABASE_URL=$(LOCAL_DATABASE_URL) \
	  uv run pytest -m db -q
