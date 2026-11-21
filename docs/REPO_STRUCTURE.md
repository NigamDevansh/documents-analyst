# Repository structure

The full folder layout, and the one idea that makes it hold together.

Companion to [`CODE_ARCHITECTURE.md`](CODE_ARCHITECTURE.md) (foundations),
[`CORE_INTERNALS.md`](CORE_INTERNALS.md) (the shared library) and
[`WORKER_RUNTIME.md`](WORKER_RUNTIME.md) (what runs).

---

## 1. The problem this solves

Two things have to be true at once:

1. **The application needs a rich shared library** — taxonomy, provenance,
   parsing, entities, prompts, telemetry.
2. **Some of that library is useful on its own** — reading a contract into one
   canonical string, calling an LLM with a schema, checking a quote is verbatim —
   and should be usable, and testable, without dragging the application along.

If those pieces quietly import the rest of core, the second goal is lost the
first time someone reaches for a convenient helper, and nobody notices until a
copy fails somewhere else.

**So the boundary is a folder, and the folder has a rule.**

---

## 2. The idea — a `portable/` zone

| File | Does |
|---|---|
| `document_text.py` | PDF / DOCX / text → the canonical string every offset indexes into |
| `llm_client.py` | Gemini + Anthropic + OpenAI behind one protocol, plus `FakeLLM` |
| `ner_gliner.py` | GLiNER wrapper |
| `verify.py` | `verify_quote` — Gate 1 |

Every one is written with **zero internal imports**: third-party SDKs and the
standard library only, errors defined in the same file, telemetry taken as a
callback rather than importing a protocol.

So rather than extracting a subset later, those files are **designed to be
copied from the start**. They live in one place, they import nothing of ours,
and anything can carry its own copy.

```
packages/core/src/documents_analyst_core/portable/     ← the source of truth
```

The rules are written at the top of `portable/__init__.py`:

1. no imports from `documents_analyst_core.*`
2. errors declared in the same file
3. callbacks, not Protocols we would have to import
4. config as arguments — never `os.environ`

---

## 3. Full layout

```
documents-analyst/
│
├── README.md                    domain, formats, setup, measured metrics, limits
├── DEVELOPMENT.md               working on the code locally
├── Makefile                     small local/full runtime and verification shortcuts
├── docker-compose.yml
├── Dockerfile                   deps · models · builder · frontend · runtime; models baked in
├── pyproject.toml               uv workspace root
├── uv.lock                      committed — the reproducibility guarantee
├── .env.example                 credentials and per-deployment model choices
├── config/
│   ├── runtime.yaml             tracked non-secret runtime defaults
│   │                            (incl. deliverable_filename: final-document.docx)
│   └── model_prices.json        per-model token prices for the cost report
│
├── packages/core/                          THE SHARED LIBRARY
│   ├── pyproject.toml           documents-analyst-core
│   ├── tests/
│   └── src/documents_analyst_core/
│       │
│       ├── portable/            ⭐ ZERO internal imports. Copy-designed.
│       │   ├── document_text.py       pdfplumber for .pdf; .docx read as XML → text
│       │   ├── llm_client.py          gemini + anthropic + openai behind one protocol
│       │   ├── ner_gliner.py          GLiNER wrapper
│       │   └── verify.py              verify_quote — Gate 1, stdlib only
│       │
│       ├── parsing/             sections.py · chunks.py
│       ├── provenance/          models · normalize · extract · open_subjects · injection
│       ├── taxonomy/            fields.py (single source of truth) · schema.py
│       ├── entities/            parties.py · family.py
│       ├── embeddings/          OpenAI text-embedding-3-small
│       ├── nli/                 deberta · stub
│       ├── prompts/             wrapping · extraction · open_subjects · parties ·
│       │                        adjudication · audit
│       ├── telemetry/           records · Sink protocol · pricing
│       ├── models.py · model_cache.py
│       └── errors.py
│
├── backend/                                THE APPLICATION
│   ├── pyproject.toml           documents-analyst; depends on documents-analyst-core
│   └── src/documents_analyst/
│       ├── settings.py          the ONLY place env is read
│       ├── deliverable.py       final-document.docx: sections, hashes, write, read back, verify
│       ├── api/                 main · deps · schemas · events · routes/
│       ├── worker/              main · scanner · ingest · lease · queue · runner · notify
│       ├── graph/               build · state · checkpointer · context · nodes/
│       ├── rules/               loader · evaluator
│       ├── register/            slots · render
│       ├── db/                  tables · rows · repo/ (incl. commits.py) · migrate
│       └── mcp/                 api.py (HTTP client) · server.py (7 tools) · tools.py
│
├── frontend/                               THE DASHBOARD — React + Vite
│   └── src/
│       ├── api/         client.ts · types.ts · spans.ts (offset → highlight) · refresh.ts
│       ├── components/  Rail · Register · SourcePane · ReviewQueue · Reviewer ·
│       │                Activity · CostPanel · DocumentDownload · Welcome
│       ├── hooks/       useEvents (SSE) · useSplit (draggable divider)
│       └── styles/      tokens.css (palette, type, easings) · app.css
│
├── db/migrations/               001..004, raw SQL, readable top to bottom
├── fixtures/                    clean_pile · conflict_pile · injection_pile · public
│   ├── ANSWER_KEY.json          107 verbatim quotes, asserted
│   └── docx_edge_cases/         tracked changes + a comment · text in a page header
├── scripts/
│   ├── prefetch_models.py       bakes models into the image
│   ├── verify_fixtures.py       every answer-key quote is verbatim
│   ├── build_fixture_docs.py    .md → .docx with real heading styles
│   ├── demo_api.sh              the API, end to end, with curl
│   └── demo_crash.sh            damages final-document.docx, shows Resolve restore it
├── watched/                     WATCH_ROOT for local runs — one folder per pile;
│                                each gets its own final-document.docx
├── tests/                       the whole suite, incl. the invariant tests
└── docs/                        these markdown files + THREADING.html · UI_AND_LINKING.html
```

---

## 4. The three zones, and the rule for each

| Zone | Rule | Copied? |
|---|---|---|
| `documents_analyst_core/portable/` | **Zero internal imports.** Dependency-light. Errors inline, callbacks not protocols | **Can be** — by anything that needs it |
| Rest of `documents_analyst_core/` | The rich library. Free to import within core | Never |
| `backend/` | The application. Imports `documents_analyst_core` normally — they ship together | — |

The `portable/` boundary *is* the boundary. You can tell at a glance what is
shareable, because it is the only code with no internal imports.

---

## 5. Adding something new later

If a new capability is needed, the question to ask is whether it can be written
with zero internal imports. If yes, it joins `portable/` and follows the four
rules above. If it needs the taxonomy, a protocol from elsewhere in core, or
state across a run, it belongs in the rest of core. If it needs the database,
the graph or a setting, it belongs in `backend/`.

Nothing about the final document is in core: rendering it needs slots, and
writing it needs the pile's record and its lease — `backend/src/documents_analyst/deliverable.py`
and `register/render.py`.
