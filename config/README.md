# Configuration files

## `runtime.yaml`

This is the **single tracked home for non-secret runtime configuration**:
paths, scanner and lease timings, the generated document's file name, run sizes,
logging, MCP, and default model values. Edit it when changing how the application behaves.

Two keys are worth knowing by name:

- **`pile_concurrency`** (default `3`, range 1–16) — how many piles one worker
  runs at once, one thread lane per pile. Files *inside* a pile are always read
  and extracted one at a time, in order; this only lets folder 2 start while
  folder 1 is still being extracted. `1` restores strictly one pile after
  another. LLM calls from the lanes run concurrently, so raising it multiplies
  the request rate against your provider — stay inside its rate limit. See
  [`docs/WORKER_RUNTIME.md`](../docs/WORKER_RUNTIME.md) §1a.
- **`watch_dir_host`** (default `""`) — display only. Docker Compose sets it
  from `WATCH_DIR` in `.env` (through `WATCH_DIR_HOST`) so `/health` and the
  dashboard's setup guide can show the watched folder as you see it on the host
  instead of `/data/watched`. Blank shows `watch_root`. Nothing reads files
  through it; to change *which* folder is watched, set `WATCH_DIR` in `.env`.

`.env` is deliberately smaller. It contains only API credentials, optional
connection/auth tokens, per-deployment model/provider overrides, and `WATCH_DIR`
— the host folder Docker Compose mounts as the watched folder, which `Settings`
never reads (it is listed in `COMPOSE_ONLY_FIELDS`). A real
process environment variable still overrides `runtime.yaml` when Docker or a
deployment needs a different address or path. Do not put production secrets in
this directory.

## `model_prices.json`

USD **per million tokens**, keyed by the exact model id the provider reports —
the same string that lands in `stage_runs.model`.

```json
{
  "provider-model-id": { "input": "INPUT_PER_MILLION", "output": "OUTPUT_PER_MILLION" }
}
```

The table includes verified standard text rates for the default providers and
embedders. Rates are strings parsed as `Decimal`; negative and non-finite values
are rejected. Custom models need their own verified rates.

Prices only convert tokens to dollars for the cost report: there is no spending
limit, and a missing or expired price never stops a call. A model with no entry
is reported as unpriced; an expired one is still used and flagged. See
[MODEL_PRICING.md](MODEL_PRICING.md) for rate sources and expiry warnings.
