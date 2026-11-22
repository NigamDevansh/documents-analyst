# Model prices

`model_prices.json` uses USD per million input/output tokens. Rates were checked
on 22 November 2026 against the providers' own pages:

- [OpenAI GPT-4o](https://developers.openai.com/api/docs/models/gpt-4o),
  [GPT-4o mini](https://developers.openai.com/api/docs/models/gpt-4o-mini), and
  [text-embedding-3-small](https://developers.openai.com/api/docs/models/text-embedding-3-small).
- [Gemini text and embedding pricing](https://aistudio.google.com/docs/pricing).
  The Gemini 3.6 Flash promotional rate expires on 31 December 2026, recorded as
  its `valid_until`.
- [Claude pricing](https://platform.claude.com/docs/en/about-claude/pricing).
  Haiku 5.5 uses its higher prompt-length tier as a conservative estimate;
  shorter prompts may be billed less.

## Prices report cost; they never stop a call

There is **no spending limit**. Every call runs, and its tokens are recorded.
Prices only turn those tokens into dollars:

- **Each run's cost** is on its closing timeline event ("Finished — this run
  cost $0.0123", likewise for "Waiting for your review" and "Analysis
  incomplete"). The **Cost & findings** tab has the pile's total, by stage.
- **A model with no entry** still runs. Its calls are reported as *unpriced*,
  with the model named, and are left out of the dollar total rather than
  counted as free.
- **An entry past its `valid_until`** keeps being used. `/health`
  (`pricing.warnings`), the worker log at startup and the Cost tab say it has
  expired. They also warn 14 days before the date, so there is time to check
  the provider's current rate and update this file.

These are estimates for ordinary text requests, without caching or batch
discounts. They are not invoices. Gemini embedding usage is estimated at four
characters a token, because its responses carry no token count. Rates are read
once per process, so restart after editing the file. `/health` reads it fresh.
