# `packages/core` internals

Phase 3 of the code architecture. What lives in the shared library and what its
interfaces look like.

Companion to [`CODE_ARCHITECTURE.md`](CODE_ARCHITECTURE.md) (foundations and
dependencies), [`REPO_STRUCTURE.md`](REPO_STRUCTURE.md) (the full layout and the
`portable/` zone) and [`WORKER_RUNTIME.md`](WORKER_RUNTIME.md) (what runs).

---

## 1. The one rule everything follows

> **Core produces values. It never persists them, and it never reads config
> from the environment.**

This is not style — it falls out of two earlier decisions:

- Core imports **no SQLAlchemy, no FastAPI, no LangGraph**. It is a library of
  capabilities, not application wiring.
- Part of it — the `portable/` zone — is written to be **copied verbatim** into
  another project, so it cannot reach for our database, our tables or our
  settings file.

Two consequences that shape every module:

**Config is injected, never imported.** No module reads `os.environ` or a global
`Settings`. The application constructs things explicitly:

```python
# backend/src/documents_analyst/worker/main.py  — the app reads config
llm = build_client(
    provider=settings.llm_provider,
    api_key=settings.llm_api_key,
    model=settings.resolved_model_extract,
    max_retries=settings.llm_max_retries,
)
```

That is also what makes core testable with no `.env` present.

**Telemetry writes nowhere.** `core/telemetry` defines the records and a `Sink`
protocol; the caller supplies the implementation.

```python
class TelemetrySink(Protocol):
    def record_llm(self, call: LLMCall) -> None: ...
```

One method, because only one thing costs money: an LLM call, billed in tokens
and dollars. Writing the final document is local and free, so there is nothing
to record for it.

Core ships `NullSink` (for any caller with no `stage_runs` table — telemetry
must not become a reason it needs one) and `CollectingSink` (in memory, for
tests and for a caller that wants to show what a pass cost).

**The Postgres sink lives in the backend, not here.** It is
`backend/src/documents_analyst/db/repo/telemetry.py`, and it landed in Phase 4A, once
there was a run to attribute rows to — before that, a writer with no caller is
just a schema guess. Core deliberately does not own it: writing needs SQLAlchemy
and a `stage_runs` table, and core's goal (Phase 2E) was telemetry that is
*backend-independent*. That is what shipped — core's records already carry every
`stage_runs` column except `pile_id` and `run_id`, the two things core cannot
know.

Without this, core would need SQLAlchemy — and `portable/llm_client.py` could
never be lifted out on its own.

---

## 2. Why the folder has two levels

```
packages/core/              ← the PROJECT folder: pyproject.toml, tests, README
└── src/documents_analyst_core/       ← the IMPORT name: `import documents_analyst_core`
```

They are different things. The project folder cannot be the import name because
you cannot `import core` — far too generic, it would collide. The `src/` layer is
a Python convention that stops tests importing the local folder instead of the
installed package.

The package is `documents_analyst_core`: named for the project rather than for
what it does, so a `portable/` file lifted into another codebase still says
where it came from.

## 3. Module map — two zones

```
src/documents_analyst_core/
│
├── portable/            ⭐ ZERO internal imports. Copyable.
│   ├── document_text.py      pdfplumber for .pdf; .docx read as a zip
│   ├── llm_client.py         gemini + anthropic + openai behind one protocol
│   ├── ner_gliner.py         GLiNER wrapper
│   └── verify.py             verify_quote — Gate 1, stdlib only
│
├── errors.py            the exception hierarchy
├── models.py            shared value objects
├── model_cache.py       where local models are cached
├── parsing/             sections.py · chunks.py
├── provenance/          models.py · normalize.py · extract.py · open_subjects.py · injection.py
├── taxonomy/            fields.py (single source of truth) · schema.py
├── entities/            parties.py — candidates → normalize → match → pick · family.py
├── embeddings/          EmbeddingProvider protocol + OpenAI (text-embedding-3-small)
├── nli/                 NLIClient protocol + deberta + stub
├── prompts/             wrapping · extraction · open_subjects · parties · adjudication · audit
└── telemetry/           records.py · sink.py (Protocol) · pricing.py
```

Every subpackage exposes a **protocol** plus one or more implementations. The
application picks; core never picks for it.

### The two zones

| Zone | Rule | Who uses it |
|---|---|---|
| `portable/` | **Zero internal imports.** Errors inline, callbacks not protocols, dependency-light | Worker directly; anything else by **copy** |
| Everything else | Free to import within core | Worker only |

That constraint is what makes a file copyable, and it is visible at a glance —
`portable/` is the only code in the repo importing nothing of ours.

---

## 4. Error hierarchy

Typed, because the graph branches on specific failures and catching broadly
would swallow real bugs into a "the provider is down" state.

```
(base class in errors.py)
├── ConfigError                  bad or missing injected config
├── ParseError                   document could not be read
│   └── UnsupportedFormat
├── GroundingError               Gate 1 territory
│   └── QuoteNotVerbatim         source_quote absent from extracted_text
├── ModelUnavailable             a local model failed to load
│   └── (carries: model name, env var, capability lost)
├── LLMError
│   ├── LLMSchemaViolation       output did not match the schema after retries
    └── LLMTransportError        provider unreachable
```

`portable/llm_client.py` declares its own `LLMError` family inline (the portable
rule), with one more leaf: `LLMQuotaExhausted`, a subclass of
`LLMTransportError` for "no credits remaining", which shares a 429 with a rate
limit but is not worth waiting for.

`ModelUnavailable` deliberately carries the model name, the env var controlling
it, and the capability lost — that is what makes the degradation messages in
§2.5 of `CODE_ARCHITECTURE.md` actually useful rather than "model failed".

---

## 5. Retry: split by duration

The rule:

> **Retries that finish inside one node execution belong to the client.
> Anything longer belongs to the worker, and is recorded in the database.**

Why the split — a backoff held in memory dies with the process. If the client
slept five minutes and the worker were killed at minute four, that wait would
restart from zero, and nothing would record that it ever happened. So the client
retries briefly and then raises; the document stays `ingested` with its
`attempts` counted, and a later tick tries again, up to `job_max_attempts`.

| Failure | Client behaviour | Then |
|---|---|---|
| Connection reset, timeout, 5xx, rate-limit 429 | bounded attempts; a stated `retryDelay` honoured, else exponential backoff, capped at 60s | still failing → `LLMTransportError` |
| `429` "no credits remaining" | **no retry** — waiting cannot fix it | `LLMQuotaExhausted` |
| Reply fails the schema | retried with the validator's message in the system prompt | still failing → `LLMSchemaViolation` |
| Invalid schema (ours) | checked **before** the first request, never sent | an error to fix |

Everything the client raises is terminal *for that call*. The node decides what
that means — `Extract` turns it into a `Rejection` for the section, `Classify`
and `Audit` record the failure — and none of it is a variable held across a
crash.

**Every attempt is recorded, including the failures.** A retry that succeeds on
attempt three still cost tokens and latency; a cost report that hides that is
lying.

---

## 6. The interfaces that matter

### `portable/document_text.py` — the canonical string

Built in phase 2A.1. `extracted_text` is the ONE string every stored offset
indexes into, so this module rewrites almost nothing and the exceptions are
named:

| Rule | Why |
|---|---|
| Blocks join with **one blank line** | `.md` and `.docx` of one contract must hash alike, and they only do if both render the gap the same way. **Measured: they do.** |
| Trailing newlines are stripped | The single normalisation. A text file ends with `\n` and a `.docx` cannot express one. Safe because it removes characters at the very end — no surviving offset can move. |
| Markdown markers are **kept** | Stripping `##` shifts every character after it and silently invalidates every later citation. |
| CRLF → LF | A file and its Windows twin are the same contract; offsets must not depend on which editor last saved it. |
| The decode is **reported**, not assumed | `meta['encoding']` always; `meta['decode_warning']` on the fallback. |
| **Body only** | Headers, footers, footnote bodies and comments are not flow a citation points into, so they are not in the canonical string. |

**`.docx` is read as XML, not through `python-docx`.** `paragraph.text`
collects only runs that are direct children of the paragraph, and a run inside
`<w:ins>` is a grandchild — so python-docx returns the document *without* its
tracked insertions, with no error. Collecting every `<w:t>` descendant instead
gives the accepted-changes view for free: insertions live in `<w:t>` and
deletions live in `<w:delText>`, so one rule includes the first and excludes the
second. A test asserts both directions against a fixture
(`fixtures/docx_edge_cases/tracked_changes_and_comments.docx`) that really
carries an insertion, a deletion and a comment.

**The Latin-1 fallback announces itself.** Latin-1 decodes *any* byte sequence
without error, so it succeeds even on files it gets wrong: Windows-1252 smart
quotes and em dashes land in the C1 range and become control characters. The
text still reads plausibly, so a quote containing one would be stored, cited and
verified against itself — wrong in the register with nothing to say so. So
`extract` returns `meta['encoding']` always and `meta['decode_warning']` on the
fallback; ingest raises that as a document-level warning rather than each caller
guessing. Refusing the file instead would quarantine a document we can read
imperfectly, which loses more than it saves.

**PDF does not alias.** `.md` and `.docx` of one contract extract to identical
bytes; the `.pdf` does not, because pdfplumber returns page text with single
newlines and cannot reproduce paragraph boundaries. Reconstructing them would
mean rewriting extracted text, which offsets forbid — so a PDF is its own
document. Recorded in `002_schema.sql` next to `text_sha256`.

### `portable/verify.py` — Gate 1

```python
def verify_quote(extracted_text: str, start: int, end: int, quote: str) -> bool:
    if not quote:
        return False
    if not 0 <= start < end <= len(extracted_text):
        return False
    return extracted_text[start:end] == quote
```

Deliberately this small. No model, no fuzzy matching, no normalisation. It is
what makes "never bluffs" an offline test rather than a promise.

**It was three lines and that was one line too few.** The comparison alone is
not the contract, because Python slicing is lenient in exactly the ways that
matter: a negative `start` counts from the end, `start > end` slices to `""` so
a fact with **no quote at all** verifies, and an `end` past the string clamps.
Each produced a stored citation the UI would then highlight somewhere other than
where the fact claimed to come from. The range is now checked before the slice.

### `provenance/extract.py` — the three gates

Section text in, `GroundedFact`s out. Nothing here persists or reads config.

1. **Taxonomy closure** — `field_key` must be in `FIELDS`. The schema already
   constrains it to an enum; this is the belt to that braces, because schema
   enforcement is a provider feature and provider features vary.
2. **Verbatim grounding** — exact string comparison, no threshold.
3. **Offset agreement** — the quote must be where the fact says it is, *or*
   relocatable to one unambiguous place.

**Relocation is the one place we repair rather than reject.** A model that
copies a quote correctly and miscounts its offsets is the common case; dropping
that fact loses something true over arithmetic it was never going to get right.
`find_quote` refuses when the quote appears twice, so the repair never guesses.
It halves confidence, so a repaired citation ranks behind an unrepaired one and
a reviewer can tell them apart.

Extraction runs **per section, not per chunk**: a chunk is sized for retrieval
and can cut a clause in half, and half a clause is where a model invents the
rest. Offsets come back in section coordinates and are shifted into whole-
document ones, because `extracted_text` is the only string Gate 1 knows about.

A `Rejection` is *returned*, not logged. A silent drop and a section that
genuinely stated nothing look identical from the outside, and the difference is
what tells you whether extraction is working.

**`parties` is refused here.** It is the one taxonomy field where "grounded" and
"correct" come apart: a model can quote an arbitration body verbatim out of a
dispute clause and pass every gate in this module. It comes only from
`entities/parties.py`, where candidates are NER spans over the preamble and the
model returns **indices** into them. Leaving the generic path open as well would
make that safeguard a convention rather than a property.

**A relocated citation carries `relocated=True`.** Confidence halving alone
could not express it — a repaired certain fact and an honest uncertain one both
land at 0.5 — so the flag is a fact on the citation and the halving is only a
ranking tiebreak.

### `taxonomy/fields.py`

One source of truth, two consumers — the SQL seed for `contract_fields` and the
JSON schema that constrains extraction. They cannot drift because they are
generated from the same list.

### `portable/llm_client.py`

```python
class LLMClient(Protocol):
    def complete(self, *, system: str, user: str,
                 max_tokens: int) -> tuple[str, Usage]: ...
    def structured(self, *, system: str, user: str, schema: dict,
                   max_tokens: int) -> tuple[dict, Usage]: ...
```

`structured` is where schema-constraining happens: Gemini gets
`response_json_schema`, Anthropic gets a forced tool with `input_schema`, and
OpenAI gets `response_format` with `strict: true`. All normalise into one
`Usage`, so nothing downstream branches on provider.

**We validate the reply ourselves** with `jsonschema`, rather than trusting
three different "strict" modes to mean the same thing. Without that check
`LLMSchemaViolation` would be an error no code path can raise. A failure is
retried with the validator's message attached — and that note goes in the
**system** prompt, never the user turn, because the user turn carries
document-derived text and "instructions are composed by us" is a stated
invariant. A retry must not quietly change what the document said.

Retries are bounded, and every attempt is counted in `Usage.attempts` with its
tokens, failures included. Three refinements came out of running the live tests
once, and each fixes something a stub could not have shown:

| Behaviour | Why |
|---|---|
| A stated `retryDelay` is honoured | Gemini answers a rate limit with `retryDelay: '13s'`. Three instant retries all fail inside four seconds — which is exactly what the first live run did. Exponential backoff is the fallback; everything is capped at 60s. |
| "No reply at all" ≠ "schema violation" | Three rate-limited attempts used to raise `LLMSchemaViolation`, sending the reader to debug a taxonomy when nothing had been judged. |
| `LLMQuotaExhausted` is not retried | "You have no credits remaining" shares a 429 with a rate limit, but waiting cannot fix it. One attempt, then the honest answer. |

Three more came out of review, each the same shape — a stated policy that the
code did not quite implement:

| Behaviour | Why |
|---|---|
| The schema is checked **before** the first request | "An invalid schema is our error and is not retried" was only true once a response came back. Until then the broken schema was sent, rejected by the provider, mapped to a transport failure, retried, and billed. `check_schema` runs before the loop. |
| No sleep after the final attempt | A stated `retryDelay: 60s` on the last failure held the caller a full minute to hand back an error it already had. |
| Each provider's `_call` is stubbed at the **SDK object** | Stubbing the shared hook left request construction, response digging and token extraction untested — the exact three things a live run had already caught wrong once. |

`sleep` is injected rather than patched, the same way lease TTLs are, so the
backoff is asserted in milliseconds without a clock library.

Provider SDKs import **lazily**, inside the constructor. Importing the module
costs nothing and needs nothing installed — a project that copies this file
and uses one provider does not have to install the other two.

`FakeLLM` lives in the same file rather than a `conftest.py`, because a
project that copies the portable zone must be able to test its own LLM path
standalone and cannot import our fixtures. It refuses a canned response its own
caller's schema rejects; otherwise a test could pass against a shape production
would refuse.

### `entities/parties.py`

```python
def extract_party_candidates(preamble: str, ner: NERClient) -> list[Span]: ...
def normalize(names: list[str]) -> tuple[str, ...]: ...   # pure, no model
def pick_parties(preamble: str, candidates: list[Span],
                 llm: LLMClient) -> list[int]: ...        # returns INDICES
```

`pick_parties` returns indices into the candidate list, never strings. The model
physically cannot invent a party name — its output space is a closed set of
verbatim spans.

---

## 7. What is *not* in core

| Not here | Where it lives | Why |
|---|---|---|
| Database access | `backend/db` | Core imports no SQLAlchemy |
| The graph | `backend/graph` | Core imports no LangGraph |
| Scanner, lease, queue | `backend/worker` | Application orchestration |
| Rule evaluation | `backend/rules` | Needs the fact store |
| Register rendering, the `.docx` writer | `backend/register`, `backend/.../deliverable.py` | Needs slots and the pile's record |
| Supersede / alias logic | `backend/worker/ingest.py` | Application policy over core's parsing |

Core parses, embeds, classifies, calls LLMs and verifies quotes. Deciding
*what to do* with any of that is the application's job.

**Also not in `portable/`** — anything that would drag a dependency along with
it, or that holds state across a run. `telemetry/sink.py` is a protocol;
`embeddings/` needs an OpenAI client; `nli/` needs a model. Useful, not
copyable, so outside the portable zone.

---

## 8. Testing shape

Everything in core is testable with **no database, no network, no API keys**:

| Module | How |
|---|---|
| `portable/llm_client` | a `FakeLLM` returning canned structured output; each provider's SDK object stubbed |
| `portable/document_text` | the `.md`/`.docx`/`.pdf` fixtures, plus `fixtures/docx_edge_cases/` |
| `provenance/verify` | pure function, plain assertions |
| `parsing/*` | the `.md` fixtures, offsets asserted against known text |
| `taxonomy` | assert the generated schema matches the SQL seed |
| `nli`, `ner` | real models where cheap; `stub` implementations otherwise |
| `telemetry` | a list-collecting sink |

The protocol-per-module shape is what makes this possible — every dependency
has a fake because every dependency is an interface.

---

## See also

[`BACKEND_INTERNALS.md`](BACKEND_INTERNALS.md) · [`TESTING.md`](TESTING.md) · [`BUILD_AND_RUNTIME.md`](BUILD_AND_RUNTIME.md)
