# One image, several entrypoints — api, worker, mcp, migrate, pytest.
#
# Four stages: deps → {models, builder, frontend} → runtime.
#
# The models stage is what makes "the test suite needs no network" true rather
# than true-on-a-machine-with-a-warm-cache. It costs a ~3GB image and a 5-10
# minute cold build; every build after that reuses the layer, because models are
# fetched BEFORE application code is copied.

# ── 1 · deps ──────────────────────────────────────────────────────────────
# Third-party packages only. NO application code, and that is the whole point:
# both stages below branch from here, so editing a Python file cannot reach
# them.
FROM python:3.12-slim AS deps

# From PyPI rather than `COPY --from=ghcr.io/astral-sh/uv`. That form is faster,
# but it makes ghcr.io a hard build dependency on every rebuild — and ghcr.io is
# blocked on plenty of corporate networks, where the failure is an opaque
# "failed to fetch anonymous token" during `make full-up`. Docker Hub and PyPI
# are already required to build at all; ghcr.io does not need to be a third.
RUN pip install --no-cache-dir uv==0.5.29

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Manifests, then `--no-install-project`: resolve every dependency without ever
# looking at our source. `--extra ml` here rather than in the models stage, so
# torch is installed once and both branches share the layer.
COPY pyproject.toml uv.lock ./
COPY packages/core/pyproject.toml packages/core/
COPY backend/pyproject.toml backend/
RUN uv sync --frozen --no-install-project --extra ml

# ── 2 · models ────────────────────────────────────────────────────────────
# Branches from `deps`, NOT from a stage carrying source. An earlier version
# had `FROM builder`, and a one-character edit to any Python file re-ran
# `uv sync --extra ml` and re-downloaded 2.2GB of weights — while the docs
# claimed the opposite. `test_dockerfile_layers.py` now asserts the ordering.
FROM deps AS models

ENV HF_HOME=/opt/models/hf \
    HUGGINGFACE_HUB_CACHE=/opt/models/hf/hub \
    MODEL_CACHE_DIR=/opt/models

# Two files, both rarely edited. `model_cache.py` is the single definition of
# the cache layout, and editing it SHOULD invalidate the prefetch — the paths
# would have changed.
COPY packages/core/src/documents_analyst_core/model_cache.py packages/core/src/documents_analyst_core/
COPY scripts/prefetch_models.py scripts/
RUN uv run python scripts/prefetch_models.py --dir /opt/models

# ── 3 · builder ───────────────────────────────────────────────────────────
# Application code lands here and nowhere earlier. A source edit invalidates
# only this stage, and `uv sync` then installs the two workspace packages from
# a warm dependency layer — seconds, no network.
FROM deps AS builder

COPY packages/ packages/
COPY backend/ backend/
RUN uv sync --frozen --extra ml

# ── 3b · frontend ─────────────────────────────────────────────────────────
# Its own base image, so the dashboard's toolchain never touches the Python
# runtime — no node in the shipped image, and a CSS edit does not invalidate a
# layer that holds torch.
#
# `package*.json` before the source, for the same reason every other stage does
# it: editing a component must not re-resolve the dependency tree.
FROM node:24-slim AS frontend
WORKDIR /ui
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

# ── 4 · runtime ───────────────────────────────────────────────────────────
FROM python:3.12-slim

RUN useradd --create-home --uid 10001 app

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HF_HOME=/opt/models/hf \
    HUGGINGFACE_HUB_CACHE=/opt/models/hf/hub \
    MODEL_CACHE_DIR=/opt/models \
    WATCH_ROOT=/data/watched \
    STATIC_DIR=/app/static \
    HF_HUB_OFFLINE=1
# HF_HUB_OFFLINE makes a missing model fail LOUDLY at load instead of quietly
# downloading it at runtime. Without it, a broken models stage produces an image
# that works on a networked machine and hangs on an air-gapped one — the exact
# failure this stage exists to prevent, just moved somewhere harder to see.

WORKDIR /app

# The venv comes from BUILDER (deps + our two workspace packages); the weights
# come from MODELS. Two sources, because only one of them should ever be
# rebuilt by a code change.
COPY --from=builder --chown=app:app /app/.venv /app/.venv
COPY --from=models --chown=app:app /opt/models /opt/models
# Served by FastAPI at the same origin as the API — no CORS, no iframe.
COPY --from=frontend /ui/dist /app/static
COPY pyproject.toml ./
COPY .env.example ./
COPY config/ config/
# 2KB, and it keeps `make test` identical to `make test-local`:
# `test_the_dockerfile_exports_exactly_what_model_cache_declares` reads it.
COPY Dockerfile ./
# The compose file travels with the image because a test reads it: the services
# that publish ports and choose transports are part of how this runs, and the
# suite has to be able to check them where the suite actually runs.
COPY docker-compose.yml ./
COPY packages/ packages/
COPY backend/ backend/
COPY db/ db/
COPY scripts/ scripts/
COPY fixtures/ fixtures/
COPY tests/ tests/

# `watched/` holds the sources and, at each pile's root, the generated
# final-document.docx — the one file the system writes there.
# Keep ownership changes in the cached dependency COPY layers. Re-chowning all
# weights after every source edit duplicated gigabytes and slowed each rebuild.
RUN mkdir -p /data/watched && chown app:app /data /data/watched /app

USER app

EXPOSE 8000

# Overridden per service in compose. Defaults to the API so `docker run` on the
# bare image does something useful.
CMD ["uvicorn", "documents_analyst.api.main:create_app", "--factory", \
     "--host", "0.0.0.0", "--port", "8000"]
