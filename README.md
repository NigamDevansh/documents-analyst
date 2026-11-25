<div align="center">

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/media/logo-dark.svg">
  <img src="docs/media/logo-light.svg" alt="Document Analyst" width="400">
</picture>

<h3>Reads. Reconciles. Reports.</h3>

<p>Turns a folder of contracts into one cited, verified final document,<br>and asks you only where the sources disagree.</p>

<p>
  <a href="https://github.com/NigamDevansh/documents-analyst/actions/workflows/ci.yml"><img src="https://github.com/NigamDevansh/documents-analyst/actions/workflows/ci.yml/badge.svg" alt="CI"></a> <img src="https://img.shields.io/badge/python-3.12-3776AB?logo=python&logoColor=white" alt="Python 3.12"> <img src="https://img.shields.io/badge/FastAPI-009688?logo=fastapi&logoColor=white" alt="FastAPI"> <img src="https://img.shields.io/badge/LangGraph-1C3C3C?logo=langchain&logoColor=white" alt="LangGraph"> <img src="https://img.shields.io/badge/PostgreSQL-pgvector-4169E1?logo=postgresql&logoColor=white" alt="PostgreSQL with pgvector"> <img src="https://img.shields.io/badge/React-TypeScript-61DAFB?logo=react&logoColor=black" alt="React and TypeScript"> <img src="https://img.shields.io/badge/Docker-Compose-2496ED?logo=docker&logoColor=white" alt="Docker Compose">
</p>

<p>
  <a href="#quick-start"><b>Quick start</b></a> ·
  <a href="#architecture"><b>Architecture</b></a> ·
  <a href="docs/"><b>Docs</b></a>
</p>

</div>

<br>

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/media/pipeline-dark.svg">
  <img src="docs/media/pipeline-light.svg" alt="Ingest, extract, cross-check, human review, deliver final-document.docx" width="100%">
</picture>

## Demo

<p align="center">
  <img src="docs/media/demo.gif" alt="Uploading an amendment, reviewing what changed and getting final-document.docx v2" width="100%">
</p>
<p align="center"><sub>Open a pile · trace a value to its source sentence · upload an amendment · review · get <code>final-document.docx</code> v2 · see what it cost</sub></p>

## Quick start

**Prerequisites**

| Requirement | Notes |
|---|---|
| **Docker** | Docker Desktop, or Docker Engine with Compose v2. About 10 GB of free disk. |
| **make, git** | Preinstalled on macOS and Linux. |
| **An API key** | `OPENAI_API_KEY` or `GEMINI_API_KEY` covers everything. With `ANTHROPIC_API_KEY`, add one of those too: Claude has no embedding model. |

**Run**

```bash
git clone https://github.com/NigamDevansh/documents-analyst.git
cd documents-analyst
cp .env.example .env    # then add your API key
make up                 # first build takes 5–10 minutes
```

Open **http://localhost:8000**, then:

1. **Add documents.** Copy a folder into `watched/`, or click **Upload files** on a pile.
2. **Review.** Enter your name, open **Review**, approve or choose.
3. **Collect.** `final-document.docx` is written into the pile's folder and can be downloaded from the dashboard.

**Good to know**

- `make down` stops everything. Your data is kept.
- Watch a different folder by setting `WATCH_DIR=/absolute/path` in `.env`, then `make up`.
- Drop a `rulebook.yaml` into a pile to check its terms against your own rules.
- `make test` runs the full suite. No API keys needed.

## Architecture

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/media/architecture-dark.svg">
  <img src="docs/media/architecture-light.svg" alt="Dashboard and MCP server call the API; API and worker share PostgreSQL; the worker runs one LangGraph lane per pile, calls LLM APIs and local models, and reads and writes the watched folder" width="100%">
</picture>

<p align="center"><sub>Deeper dives: <a href="docs/BACKEND_INTERNALS.md">Backend</a> · <a href="docs/WORKER_RUNTIME.md">Worker runtime</a> · <a href="docs/CORE_INTERNALS.md">Core</a> · <a href="docs/TESTING.md">Testing</a></sub></p>
