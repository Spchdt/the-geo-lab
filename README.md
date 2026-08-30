# The GEO Lab

Local desktop prototype for measuring how one product listing changes closed-catalogue retrieval and recommendation outcomes. It does not measure public ChatGPT visibility, indexing, conversions, or sales.

## Run

```bash
uv sync --extra dev
uv run intenttwin
```

Copy `.env.example` to `.env`, then configure one OpenAI-compatible structured-output endpoint:

```bash
INTENTTWIN_LLM_URL=https://generativelanguage.googleapis.com/v1beta/openai/chat/completions
INTENTTWIN_LLM_MODEL=gemini-3.7-flash
INTENTTWIN_LLM_API_KEY=replace-me
INTENTTWIN_LLM_MIN_INTERVAL=4.2
```

The default 4.2-second interval is conservative for low-quota Gemini projects. HTTP 429 responses retain Gemini's error detail, honor `Retry-After`, and back off while the run stays cancellable. Configure `INTENTTWIN_LLM_RATE_LIMIT_RETRIES` and `INTENTTWIN_LLM_RATE_LIMIT_BASE_DELAY` only when your provider quota requires different behavior.

Open the configured localhost URL, choose **Input item**, then enter any marketplace category, the current listing, price, and comparable product attributes. The good and bad sample buttons demonstrate a detailed versus under-specified listing with identical canonical facts. Missing LLM configuration disables run creation. Runs survive refresh in `intenttwin.db`; artifacts land under `data/artifacts/<run-id>/`.

The current GEO flow is staged:

- screen: 6 auto-generated relevant queries across up to 10 deterministic variants, at most 60 recommendation calls plus one gap-summary call;
- confirmation: 12 held-out queries across original, two promoted variants, and the identity control, at most 48 recommendation calls plus one summary call.

Every screen variant reaches the configured model. Query generation, listing variants, retrieval metrics, promotion, and gap mapping are deterministic. Exact same-condition requests can reuse a successful cached observation on a later run. Earlier paper-profile runs remain reopenable as legacy evidence.

## Check

```bash
uv run pytest
uv run python scripts/run_smoke.py
```

## Prototype choices

- `generic-marketplace-v1`: 30 deterministic same-category benchmark peers plus one immutable brand submission captured in each run manifest.
- Five fixed listing representations, up to three one-fact ablations, and two controls. The held-out phase promotes only the best two valid treatments.
- SQLite FTS5 BM25, pinned deterministic local semantic projection, typed-attribute retrieval, RRF, and canonical hard constraints.
- Every recommendation uses configured live LLM with strict structured output. Missing configuration or failed calls fail run; no heuristic or fixture fallback exists.
- The fixed candidate set and candidate order are identical across conditions inside a query/trial pair; PUT position rotates across groups and trials.
- Metrics keep retrieval MRR/top-three visibility separate from Gemini top-one/top-three recommendation. No composite score hides disagreement.
- A final structured Gemini call explains the deterministic gap evidence and proposes a fact-cited rewrite. Invalid summaries remain retryable without discarding metrics.
- Misleading control remains intentionally invalid in research use. Its typed capacity mismatch is reported as an unsupported-template-claim safety detection for every affected output.
- Dashboard uses server-rendered HTML and two-second polling.

All results remain descriptive until sample size, margins, and decision rules are preregistered. The local semantic channel currently uses a pinned deterministic projection rather than a downloaded sentence-transformer; this preserves reproducible pipeline behavior but is the main remaining infrastructure deviation from the paper implementation plan.

Reset by deleting `intenttwin.db` and rerunning. Keep completed run evidence if results matter.
