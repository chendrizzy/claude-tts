# Contributing to claude-tts

Thanks for helping! This project values a tight, well-tested core over breadth.

## Dev setup

```bash
uv sync --extra edge --extra dev   # base + edge-tts engine + test tooling
```

## The binding gate: `make verify`

`make verify` is the definition of "done". It is **deterministic and all-sync**
— it needs no live daemon, no Ollama, and no `pytest-asyncio` — and it fails on
the regressions that actually hurt:

- markdown / code artifacts leaking into spoken output (`test_spoken_render`,
  `test_shadow_replay`),
- SPEAK/SKIP classification regressions (`test_router_corpus`,
  `test_content_router`),
- path-humanization bugs (`test_text_utils`),
- provider-seam regressions (`test_llm_provider`).

```bash
uv run make verify     # must be green before you open a PR
```

The broader async suite (`uv run pytest tests/`) needs the `dev` extra for
`pytest-asyncio`; those tests cover the daemon's async pipeline.

## Test layout

Tests live in `tests/`. There is **no `conftest.py`** — each test does its own
`sys.path.insert(0, <repo root>)`. Fixtures (labeled judge/summary oracles,
router corpus, spoken corpus) live under `tests/fixtures/`. `make verify`
regenerates the spoken corpus before running.

## Adding a provider (LLM brain)

Implement `daemon/providers/base.py::LLMProvider` (`judge` + `summarize` +
`inner_timeout_s`), register it in `daemon/providers/factory.py`, and add a unit
test to `tests/test_llm_provider.py`. Keep it to one interface, few impls —
resist per-vendor sprawl (the `openai_compat` provider already covers most of
the ecosystem).

## Adding an engine (synthesis)

Engine drivers live in `daemon/pipeline/` (`kokoro_engine.py`,
`voicebox_client.py`, the lazy edge path in `generate_stage.py`). Keep optional
dependencies lazy-imported so a missing engine never breaks daemon startup.

## Pull requests

- `make verify` green.
- No personal data, absolute paths, or secrets (this is a public repo).
- Match the surrounding code's style and comment density.
