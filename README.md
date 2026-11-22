<h1 align="center">Document Analyst</h1>

<p align="center"><b>Reads. Reconciles. Reports.</b></p>

```mermaid
flowchart LR
    A["📁 Folder of documents<br/><sub>PDF · DOCX · MD · TXT · HTML</sub>"] --> B["Read & extract<br/><sub>every value quoted from its source</sub>"]
    B --> C["Cross-check<br/><sub>conflicts · amendments · rulebook</sub>"]
    C --> D["👤 You review<br/><sub>approve or choose</sub>"]
    D --> E["📄 final-document.docx<br/><sub>written back into the folder</sub>"]
```

<p align="center">
  <img src="docs/media/demo.gif" alt="Opening the dashboard, uploading an amendment, reviewing what changed and getting final-document.docx v2" width="900">
</p>

## Run it locally

**Prerequisites**

- Docker Desktop (or Docker Engine with Compose v2), with ~10 GB free disk
- `make` and `git` (built in on macOS and Linux)
- An API key, either:
  - `OPENAI_API_KEY` or `GEMINI_API_KEY`: one key covers everything
  - `ANTHROPIC_API_KEY` plus an OpenAI or Gemini key. Claude has no embedding model.

**Steps**

1. Clone the repo
   ```bash
   git clone https://github.com/NigamDevansh/documents-analyst.git && cd documents-analyst
   ```
2. Add your key
   ```bash
   cp .env.example .env    # then set e.g. OPENAI_API_KEY=sk-...
   ```
3. Start everything. The first build takes 5–10 minutes.
   ```bash
   make up
   ```
4. Open **http://localhost:8000**
5. Add documents. Either way works:
   - copy a folder into `watched/`, so `watched/<name>/` becomes a pile;
   - or click **Upload files** on an open pile.
6. Review: enter your name, open **Review**, approve or choose.
7. Collect `final-document.docx` from the pile's folder, or download it from the dashboard.

**Good to know**

- Stop with `make down`. Your data is kept.
- Watch another folder: set `WATCH_DIR=/absolute/path` in `.env` and run `make up` again.
- Add `rulebook.yaml` to a pile to check its terms against your own rules.
- Run the tests (no keys needed) with `make test`.

## Architecture

```mermaid
flowchart TB
    subgraph host["Your machine"]
        W["📁 watched/pile-name/<br/>source files · rulebook.yaml · final-document.docx"]
    end

    UI["Dashboard<br/>React · live timeline (SSE)"] <-->|HTTP| API["API<br/>FastAPI"]
    MCP["MCP server<br/>for AI agents"] --> API
    API -->|uploads| W
    API <--> DB[("Postgres + pgvector<br/>facts · decisions · events · checkpoints")]

    subgraph worker["Worker"]
        S["Scanner<br/>finds piles and changed files"]
        subgraph lanes["One lane per pile (runs in parallel)"]
            G["Resolve → Ingest → Extract → Match → Classify → Audit<br/>→ ⏸ Human gate → Commit → Reconcile → Audit"]
        end
    end

    S --> W
    S --> DB
    G <--> DB
    G -->|writes and verifies| W
    G --> LLM["LLM APIs<br/>OpenAI · Gemini · Claude"]
    G --> LOCAL["Local models<br/>NLI · NER"]
    DB -. "NOTIFY on approval" .-> worker
```
