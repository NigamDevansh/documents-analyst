# Configuration files

## `runtime.yaml`

This is the **single tracked home for non-secret runtime configuration**:
paths, scanner and lease timings, the generated document's file name, budgets,
logging, MCP, and default model values. Edit it when changing how the application behaves.

`.env` is deliberately smaller. It contains only API credentials, optional
connection/auth tokens, and per-deployment model/provider overrides. A real
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

**Ships empty on purpose.** Per-token rates change without notice and cannot be
verified from inside this process, so no provider model id or rate is supplied
in this repository. An unpriced model costs `Decimal(0)`
and `CollectingSink.unpriced_models` names it — a cost summary reads
*"1.2M tokens, unpriced"* rather than a plausible wrong dollar figure. Tokens
are measured exactly; dollars are the one number that needs
your input.

Values are **strings**, and are read with `Decimal`. A cost report is
arithmetic someone may check against an invoice, and `0.1 + 0.2` is not `0.3`
in binary floating point.

Nothing breaks while this is empty. Running unpriced is a supported state —
refusing to start because a price was missing would stop a pile over a
reporting detail.
