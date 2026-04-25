# notebooks/

Learning artifacts. **Not production code.** Nothing in this directory is imported by the bot, runs in the trading loop, or affects deployment.

Each notebook is a self-contained walkthrough of one piece of the math or infrastructure the bot relies on. They exist to make black-box production code legible — derive the formula on paper, implement it from scratch, sanity-check it against the production implementation, visualize the assumptions and where they break.

## Setup

Notebook dependencies live in `../requirements-notebooks.txt` (separate from `requirements.txt` so production deployment surface stays minimal):

```
venv/bin/pip install -r requirements-notebooks.txt
venv/bin/jupyter lab
```

## Index

- [`gbm_derivation.ipynb`](gbm_derivation.ipynb) — Geometric Brownian Motion and barrier-touch probability. Derives the closed-form one-touch formula used by `backend/core/monte_carlo.prob_one_touch_above_analytic`, builds an MC simulator as an independent check, visualizes GBM assumption failures (constant vol, log-normal returns, no jumps) on real BTC data.

## Conventions

- Every notebook starts with a short "what you'll learn" cell so you can decide whether to read it.
- Code cells are short. Markdown cells in between explain what's about to happen and why.
- Production code is referenced with file paths (`backend/core/monte_carlo.py`) so notebooks and bot stay tied together as the codebase evolves. If a notebook stops matching production, that's a signal to update one or the other — file an issue, don't silently let them drift.
- Outputs (plots, numbers) are committed alongside the notebook so it can be read without re-running.
