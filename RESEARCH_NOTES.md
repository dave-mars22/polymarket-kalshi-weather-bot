# RESEARCH_NOTES.md

*Project research memory. Companion to README.md (what the code does) and ARCHITECTURE.md (historical, pre-rebuild). This document captures what we have **learned**, what we have **decided**, and what we plan to **investigate**. It is updated as the project evolves — every diagnostic, parameter change, and checkpoint adds to it.*

*Last meaningful update: 2026-04-25 (initial creation).*

---

## 1. Project Overview

A simulated multi-strategy trading bot operating on Polymarket and Kalshi prediction markets. Two strategies run in parallel:

- **Multi-crypto technical brain** — RSI / momentum / VWAP / SMA composite signal on Polymarket 5-minute up/down binaries for BTC, ETH, SOL, XRP.
- **Monte Carlo barrier brain** — closed-form GBM (geometric Brownian motion) pricing on Kalshi BTC barrier options across daily/monthly/yearly cadences.

**Currently tested hypothesis:** that the technical brain (RSI/momentum/VWAP/SMA on Polymarket 5-min crypto binaries) and the MC barrier brain (closed-form GBM pricing on Kalshi BTC barriers) can find systematic edge sufficient to overcome fees on retail-accessible prediction markets — at scales small enough to remain in pricing windows where market makers don't fully arbitrage edge away.

**Mode:** simulation only. No real capital deployed. `SIMULATION_MODE = True` and is the gate to Version C (real-money operation), which won't open without falsifiable evidence of edge per Section 6.

---

## 2. Current Strategy State

*Last updated: 2026-04-25 (post-S2).*

Two strategies running simultaneously:

- **Technical brain**: scans 4 underlyings (BTC/ETH/SOL/XRP) on Polymarket 5-min markets every 60 s. Generates probability estimates from RSI / momentum (1m, 5m, 15m) / VWAP deviation / SMA crossover composite. Trades when `|edge| ≥ 5%`. ~133 trades/day.
- **MC barrier brain**: scans 8 Kalshi crypto series (5 BTC + 3 altcoin) every 600 s. Prices barrier options via closed-form GBM with EWMA vol estimation. Trades when `net_edge ≥ 5%` AND quote hasn't drifted between scan and fill. ~4 trades/day post-S2.

### Critical config values currently in production

| setting | value | notes |
|---|---:|---|
| `SIMULATION_MODE` | **True** | must stay True until Version C |
| `INITIAL_BANKROLL` | $200 | technical pool |
| `MC_PILOT_BANKROLL_USD` | $1,000 | MC pool (notional, isolated) |
| `MIN_EDGE_THRESHOLD` | 0.05 | technical (raised from 0.02 in slice S1) |
| `MC_MIN_EDGE_THRESHOLD` | 0.05 | MC |
| `MAX_TRADE_SIZE` | $10 | per technical trade |
| `DAILY_LOSS_LIMIT` | $80 | circuit breaker |
| `KELLY_FRACTION` | 0.10 | fractional Kelly multiplier |
| `MC_MAX_OPEN_PER_SERIES_DAILY` | **3** | post-S2 (was 2) |
| `MC_MAX_OPEN_PER_SERIES_MONTHLY` | 2 | unchanged |
| `MC_MAX_OPEN_PER_SERIES_OTHER` | 2 | unchanged (KXBTCY etc.) |
| `MAX_TOTAL_PENDING_TRADES` | 20 | bot-wide |
| `MAX_PENDING_PER_UNDERLYING` | 8 | per crypto-tech underlying |
| `CRYPTO_TECH_UNDERLYINGS` | "BTC,ETH,SOL,XRP" |  |
| `MC_QUOTE_DRIFT_TOLERANCE` | 0.02 | skip if ask drifts > 2¢ between scan and fill |

---

## 3. Diagnostic Findings

*Chronological summary of what the data has told us. Add new diagnostics as they are run.*

### 3a. Edge bucketing (analyzed 2026-04-24, n=720 settled BTC trades)

- Sub-5% edge trades (n=210): **44.3%** win rate, cumulative **−$150**
- ≥5% edge trades (n=496): **~52%** win rate, cumulative **+$200**
- Trend across 5–10% buckets: **NON-MONOTONIC**. Win rate is flat at ~52% for 5–7% edge, then drops to 47% in the 8–10% bucket.
- **Conclusion:** the model's confidence is meaningful only as a **binary above-or-below-5% filter**, not as a continuous gradient.
- The 8–10% dip has now replicated across two independent samples (full-history and post-S1 cohort). Worth investigating but underpowered to act on.

### 3b. Feature predictivity (analyzed 2026-04-24, n=100 trades with populated features dict)

| feature | LOW | HIGH | spread | verdict |
|---|---:|---:|---:|---|
| RSI | 40.6% | 65.6% | **+25 pp** | DISCRIMINATES |
| momentum_5m | 37.5% | 62.5% | **+25 pp** | DISCRIMINATES |
| momentum_15m | 43.8% | 65.6% | **+22 pp** | DISCRIMINATES |
| vwap_deviation | 46.9% | 62.5% | +16 pp | DISCRIMINATES |
| sma_crossover | 43.8% | 59.4% | +16 pp | DISCRIMINATES |
| volatility | 43.8% | 53.1% | +6–9 pp | borderline / probably noise |
| `exec_pending_*` | flat | flat | ~3 pp | not predictive |
| `exec_bankroll_at_entry` | 59.4% | 28.1% | −34 pp | streak-autocorrelation artifact, **not** a real predictor |

**Caveats:** tertile sample sizes are n≈32 each, 95% CIs are wide (±15–20 pp). Suggestive, not conclusive.

### 3c. Calibration (analyzed 2026-04-24, n=586 pre-rebuild trades)

- **Brier score: 0.2538** (random = 0.2500) — barely better than coin flip
- Predicted P(win) bucket 0.50–0.55 actually realizes **44.9%** — model overconfident by ~7 pp
- Predicted P(win) bucket 0.55–0.60 actually realizes **50.9%** — model overconfident by ~6 pp
- **Conclusion:** the model is *directionally right* (features point the right way) but *magnitude-wrong* (confidence levels can't be trusted at face value).
- A calibration multiplier scaffold exists in `backend/core/calibration.py` but is currently disabled. Activation becomes feasible once 300+ feature-tagged trades exist.

### 3d. Loss clustering (analyzed 2026-04-24, n=363 losses)

- **UTC hours 8, 20, 22 underperform**: 35–38% win rate over n=23–37 each.
- **UTC hours 0, 1, 10–12, 23 outperform**: 56–67% win rate.
- **Direction asymmetry**: UP wins 51.8% (n=327), DOWN wins 47.2% (n=391) — 5 pp gap, CIs overlap.
- **Mondays underperform**: 45.4% win rate over n=163.
- 5 worst losses are all from Apr 11 at $75 size — pure pre-rebuild sizing artifact, not a strategy pathology.
- **Conclusion:** mild structural patterns exist but are underpowered for action.

### 3e. Post-S1 cohort baseline (analyzed 2026-04-25, n=197 settled trades since 2026-04-21 cutoff)

- Win rate **51.8%**, 95% Wilson CI [44.8%, 58.7%] — straddles 50%
- Cumulative PnL: **−$0.41** (negligible at $1.14 avg size)
- Consistent with prediction (pre-change ≥5% cohort was 51.1%)
- **Daily settled rate: ~49 BTC trades/day**
- **Projected by 2026-05-21: ~1,481 settled trades**, 95% CI half-width ≈ ±2.5 pp at that n

### 3f. Trade replay scenarios (analyzed 2026-04-25, slice T1 tool)

| scenario | n | win rate | ΔPnL vs baseline |
|---|---:|---:|---:|
| BASELINE | 510 | 51.4% | (base) +$203 |
| CAP EDGE @ 0.08 | 388 | 52.3% | +$26 (consistent with 8–10% dip) |
| SKIP BAD HOURS | 416 | 53.8% | +$82 (in-sample only) |
| TIGHTER EDGE 0.06 | 369 | 51.2% | −$54 |
| TIGHTER EDGE 0.07 | 232 | 50.4% | −$130 |
| COMBINED (5–8% + skip hrs/Mondays) | 257 | 56.8% | +$36 |

All findings are in-sample. Patterns discovered in this data must be validated on **future** data, not the same data they were found in. Useful for hypothesis generation, not parameter tuning.

### 3g. AI module audit (analyzed 2026-04-25)

- **Verdict: bot uses ZERO AI at runtime.**
- 1,133 lines of dormant scaffolding in `backend/ai/` — never imported by any trading-path file.
- `AILog` table exists with **0 rows**.
- The `anthropic` and `groq` Python packages are installed in `requirements.txt` but unused.
- Cleanup candidate: delete the dead AI scaffolding eventually (out of scope while it isn't doing harm).

### 3h. Strategy market inventory (analyzed 2026-04-25)

- **Technical brain**: 24 candidate markets per scan (6 per underlying × 4), **133 trades/day**.
- **MC brain**: 330 unique markets per scan, 142 signals, 6–7 actionable, but only **2 series produced trades in 24h** (KXBTCMAXMON + KXBTCD).
- **3 altcoin series scanned but never traded**: KXBCH, KXSHIBA, KXAVAXD. Either edges never crossed 5%, asks priced above 0.75, or insufficient daily-vol history. Investigate or remove.
- **3 BTC series scanned but never traded**: KXBTCMAXD, KXBTCMINMON, KXBTCY. Investigate.
- MC concentration cap was **the binding constraint**: 170 cap-blocks in 6.7 hours of pre-S2 logs. Addressed by slice S2.

---

## 4. Parameter Changes (slice log)

*Each entry: what changed, runtime impact, rationale, evaluation/revert criteria.*

### Slice S1 — `MIN_EDGE_THRESHOLD` synced to 0.05 (committed 2026-04-25, hash `b7f0ea8`)

- **What changed:** `backend/config.py` default 0.02 → 0.05; `.env.example` 0.08 → 0.05; comment block added explaining rationale.
- **Runtime behavior:** unchanged. `.env` was already at 0.05 since approximately **2026-04-21** (latest sub-5% trade in DB is id=523 at 00:55 UTC that day).
- **Rationale:** diagnostic showed sub-5% trades lost $150 at 44.3% win rate; ≥5% trades made $200 at ~52%.
- **Evaluation date:** 2026-05-21 (statistical clock effectively started 2026-04-21 when `.env` was raised, not at commit date).
- **Revert criterion:** if post-change cohort win rate cleanly drops below 50% with tight CI.

### Slice S2 — MC concentration cap differentiated by cadence (committed 2026-04-25, hash `2bd9bdc`)

- **What changed:** single `MC_MAX_OPEN_PER_SERIES = 2` replaced with three cadence-specific settings: daily=3, monthly=2, other=2. Cadence map added to `backend/data/mc_markets.py`. Cap-check logic in `backend/core/mc_execution.py` updated.
- **Runtime behavior:** daily series now allows 3 simultaneous positions instead of 2. Bot opened a 3rd KXBTCD position on the first post-restart scan (4 → 5 open MC positions immediately).
- **Rationale:** data accumulation rate was throttled (170 cap-blocks in 6.7h, mostly KXBTCD). Daily contracts settle within 24h providing fast feedback, so increased exposure is lower-risk.
- **Risk acknowledgement:** loosens MC pre-validation exposure during a period when **zero MC trades have settled yet**.
- **Revert criterion:** if April 30 KXBTCMAXMON settlements go badly, OR if interim daily settlements show realized win rate < 40%.

---

## 5. Open Positions Snapshot

*Captured 2026-04-25. This section will be stale by tomorrow but the snapshot is worth preserving for the document's first version.*

5 open MC barrier positions, 0 open technical-brain positions (5-min markets clear within minutes by design):

| id | ticker | dir | size | entry | age | settles |
|---:|---|---|---:|---:|---:|---|
| 595 | `KXBTCMAXMON-BTC-26APR30-8000000` | yes | $16.59 | 0.510 | 24.1 h | 2026-04-30 |
| 606 | `KXBTCMAXMON-BTC-26APR30-8250000` | yes | $13.66 | 0.190 | 23.3 h | 2026-04-30 |
| 708 | `KXBTCD-26APR2517-T78749.99` | yes | $13.02 | 0.140 | 8.1 h | 2026-04-25 21:00 UTC |
| 709 | `KXBTCD-26APR2517-T77249.99` | no | $12.46 | 0.280 | 8.1 h | 2026-04-25 21:00 UTC |
| 728 | `KXBTCD-26APR2517-T78499.99` | yes | $14.37 | 0.100 | 0.5 h | 2026-04-25 21:00 UTC (post-S2 third position) |

Total invested: ~$70 across both series. Total potential payout if all win: ~$235.

---

## 6. Falsifiable Success Criteria

*Specific, dated checkpoints. Outcomes feed back into Sections 4 (parameter decisions) and 8 (roadmap re-prioritization).*

### 2026-04-30 — first MC barrier settlements

- 2× KXBTCMAXMON-26APR30 positions resolve.
- Plus N daily KXBTCD settlements between now and then.
- **SUCCESS:** settlements broadly align with model predictions (positions entered at >0.5 prob settle yes more often than not, scaled appropriately).
- **AMBIGUOUS:** small sample, mixed results, no clear signal.
- **FAILURE:** settlements contradict predictions strongly enough to warrant reverting S2 and questioning the GBM pricer.

### 2026-05-21 — first BTC technical brain statistical evaluation

- Post-S1 cohort projected to ~1,481 settled trades.
- 95% CI half-width approximately ±2.5 pp at that n.
- **SUCCESS:** win rate ≥ 53% with CI cleanly above 50%.
- **PROBABLY WORKING:** win rate 51–52%, CI marginal but positive.
- **AMBIGUOUS:** win rate ~50%, CI straddles 50% — keep collecting another 30 days.
- **FAILURE:** win rate < 50% with tight CI — strategy doesn't work as configured.

### 2026-07-23 — 90-day MC pilot stopping criterion

- If by this date the MC strategy hasn't shown clear positive edge across multiple months of settlements: **shut it down or pivot.**
- This is the "don't run a losing strategy forever" gate. Documented in `MC_PILOT_BANKROLL_USD` comment.

---

## 7. Things Explicitly Decided NOT to Do (and why)

- **Don't add ML to the trading loop.** Sample size too small (~100 feature-tagged trades), would overfit, would destroy interpretability. Revisit when 3,000+ feature-tagged trades exist.
- **Don't add LLM to the trading loop.** Numerical prediction isn't what LLMs do well; would add latency, cost, opacity. Legitimate use of LLMs is as research assistant, which is already happening.
- **Don't reconcile `BotState.total_pnl` ↔ `SUM(Trade.pnl)` drift.** $54 drift is documented (slice D4.5), residual approach in dashboard handles it correctly, fixing would just shift bankroll display from $200 to $254 with no functional benefit.
- **Don't tune other parameters** (KELLY_FRACTION, hour filters, direction asymmetry) **based on existing diagnostics.** All suggestive but underpowered. Acting on them would be overfitting to in-sample patterns.
- **Don't rush the backtester.** Planned for multiple focused sessions, not one evening. A bad backtester is worse than no backtester.

---

## 8. Roadmap (with timing gates)

*Priority-ordered. Items move between buckets as evidence accumulates.*

### Probably worth doing (in priority order)

1. **Backtester** — multi-session project. Scope in next focused session, build over 2–3 weeks. Highest-leverage infrastructure improvement available.
2. **Calibration multiplier activation** — already scaffolded in `backend/core/calibration.py`. Turn on when 300+ feature-tagged trades exist. Worth revisiting late May 2026.
3. **Logistic regression on features** — replace hand-coded composite weights with learned weights. Worth revisiting late summer 2026 when 500–1,000 feature-tagged trades exist.
4. **Drop volatility from composite scoring** — confirmed dead weight in two independent diagnostics (3b). Do as cleanup whenever signal logic is being touched.
5. **Investigate the 8–10% edge dip** — replicated across two cohorts (3a, 3e). Worth revisiting when post-change 8–10% bucket reaches n≈200, ~30–40 days from now.

### Possibly worth doing (depends on findings)

- **Hour-of-day filter** — UTC 8/16/20/22 underperform consistently (3d). Worth revisiting when n≥50 in each problem hour, ~July 2026.
- **Direction asymmetry adjustment** — UP wins ~5 pp better than DOWN (3d). Worth revisiting when gap holds across more data.
- **Regime detection** — needed when conditions change to evaluate cross-regime performance. Worth building before market conditions shift meaningfully.
- **Investigate silent Kalshi series** — KXBCH/KXSHIBA/KXAVAXD never trade; KXBTCMAXD/KXBTCMINMON/KXBTCY also silent (3h). Diagnostic-only investigation, not action.
- **Slippage modeling improvements** — current edge calc assumes displayed prices. Better model needed if real-money deployment happens.

### Operational improvements (would unlock real-money path)

- **VPS hosting** — laptop dependency is a single point of failure. ~$5–10/month, weekend project.
- **Push alerts (Telegram/Discord)** — currently no notification on bot crash or unusual events. ~50 lines of code.
- **Better API resilience** — add exponential backoff, circuit breakers on persistent failures.

### Probably never worth doing

- **LLM in trading decision loop.** Known antipattern.
- **Adding features without testing existing ones first.** Premature optimization.
- **Lowering edge threshold below 5%.** Diagnostic showed sub-5% trades lose money.
- **Stock price prediction with ML at this data scale.** Known dead end.

### Completed since last update

| slice | commit | description | completed |
|---|---|---|---|
| **C1** | `3a40109` | Delete dormant `backend/ai/` scaffolding (1,133 LOC, 5 files + AILog model + 2 PyPI deps + 4 config settings) | 2026-04-25 |
| **C2** | `b3b5653` | Sync `.env.example` — fix `KELLY_FRACTION` 0.25→0.10 drift, remove dead-econ-pipeline `FRED_API_KEY` / `BLS_API_KEY` placeholders | 2026-04-25 |
| **P1** | `3ddbd82` | Parallelize per-underlying scan loop in `scan_for_signals` via `asyncio.gather` — eliminates scheduler timing pressure (`/api/dashboard` 9.7s → 4.0s, ~58% reduction) | 2026-04-25 |

---

## 9. Active Research Questions

*Questions the methodology infrastructure (backtester + out-of-sample testing) is being built to answer.*

1. Does the 8–10% edge dip replicate on out-of-sample data?
2. Do RSI / momentum tertile spreads tighten with more data, and are they stable across regimes?
3. Would a calibrated probability outperform raw model output?
4. Would learned feature weights outperform hand-coded weights when properly cross-validated?
5. Is the strategy regime-dependent? (Currently only one regime in data.)
6. Why do KXBCH/KXSHIBA/KXAVAXD never produce tradeable signals?
7. Does the GBM pricer's accuracy vary by time-to-expiry? (Daily vs monthly contracts.)

---

## 10. How to Use This Document

This document is a living artifact. **Update it after every meaningful event:**

| event | update target |
|---|---|
| Diagnostic produces meaningful findings | add to Section 3 |
| Parameter change committed (S-slice) | add a slice entry to Section 4 |
| Checkpoint reached (Section 6 dates) | update Section 6 with what happened |
| New question worth investigating arises | add to Section 9 |
| Roadmap item completed or removed | update Section 8 |
| Open positions list noticeably changes | refresh Section 5 (or note it's stale) |

**Style conventions:**

- Each section header includes the date of last meaningful update so future-me can spot stale content at a glance.
- Numbers are specific: `n=197`, not "small sample". Dates are absolute: `2026-04-30`, not "next week".
- Don't speculate beyond what evidence supports. Flag wide CIs and small-sample findings explicitly.
- Keep sections scannable: short paragraphs and bullet lists, not walls of text.
- When a finding contradicts a prior entry, **don't delete the prior entry** — append a dated update so the history of belief is preserved.
- Commit conventions: documentation-only updates use `slice N{n}: ...` (N for "notes"); code changes use `slice S{n}: ...` (S for "slice").

This document complements `README.md` (what the code does today) and `ARCHITECTURE.md` (historical, marked stale). When the three disagree, this document is the source of truth for **research state**; README is the source of truth for **code shape**; ARCHITECTURE is no longer authoritative for anything.

---

## 11. Long-term radar

*Last meaningful update: 2026-04-25 (initial creation).*

*Captures legitimate technique/tool ideas surfaced during ideas-list reviews that are NOT immediate roadmap items but are worth tracking for future consideration. Section 11 grows as new ideas surface; the discipline for moving items in / out is in 11.6.*

### 11.1 Contingent technical upgrades

Items that may become relevant depending on what existing strategy evaluations reveal.

- **GARCH volatility modeling for the MC brain** — currently the MC brain's volatility estimator uses simple realized volatility from recent price history (EWMA optional, plain σ as the default). GARCH would model volatility as time-varying with autocorrelation. Becomes relevant **only if** the April 30 KXBTCMAXMON settlements (and subsequent monthly settlements) show that GBM-with-realized-vol pricing is meaningfully off. If GBM works, GARCH adds complexity without benefit. **Decision gate:** April 30 settlement evaluation per Section 6.

- **Regime detection for cross-strategy evaluation** — already on the active roadmap (Section 8, "Possibly worth doing"); reiterating here as part of the long-term radar so it stays visible in the radar inventory. Becomes critical when market conditions change and we need to evaluate whether existing strategies' edge is regime-dependent.

### 11.2 Tools and references to study

External resources worth keeping in mind for future projects.

- **`poly_data` repository — https://github.com/warproxxx/poly_data** — pipeline for ingesting Polymarket order events and processed trades into structured CSVs. Directly relevant for the eventual backtester project (Section 8 item #1). Architecture worth studying: resumable, deduplicates, keeps historical data updated incrementally — the backtester will need all three properties.

- **Hull, "Options, Futures, and Other Derivatives"** — standard quant finance textbook. Foundation reading for understanding Black-Scholes / GBM (already implemented in MC brain), Greeks, volatility models, and the math underlying barrier option pricing. Read over months, not as a project — chapters useful for this codebase: BSM derivation, barrier options, volatility models (which previews 11.1's GARCH item).

### 11.3 Concept inventory (for general knowledge)

Theoretical frameworks worth understanding even though they're not implementation projects.

- **Black-Scholes derivation and the GBM SDE that underlies it** — already in your bot via the MC brain. Understanding the math deepens the ability to evaluate MC pricer accuracy and to recognize when assumptions break (constant vol, no drift over short horizons, log-normal returns).
- **Markowitz portfolio theory** — becomes relevant if portfolio scale ever justifies cross-position optimization (correlation between simultaneous positions). Not currently relevant at 5-position scale.
- **Fama's market efficiency hypothesis** — relevant framing for "why does any retail strategy have edge?" Useful skepticism: any apparent edge survives only if the market is inefficient *in the specific way* the strategy assumes.
- **CAPM / risk-return models** — useful general concept; doesn't apply directly to prediction markets (no broad market beta to regress against), but the framing of "how much extra return per unit of risk" generalizes.

### 11.4 Pairs trading frame for strategy 3

"Pairs trading" is the canonical name for the strategy 3 cross-platform arbitrage approach being scoped. The standard pairs trading frame uses cointegration tests and z-scores on the spread between two correlated assets. The strategy 3 spread-detection layer can adopt this language explicitly:

- Polymarket BTC market and Kalshi KXBTCD market are the **pair**.
- The **spread** is observed over time.
- **Z-score thresholds** (e.g., 2σ) could be used as an alternative to fixed-bps thresholds for the tradeable criterion.
- **Cointegration tests** (Engle-Granger, Johansen) could validate that the markets *should* move together — a defense against trading apparent spreads on actually-different events that happen to look similar.

This is a **future refinement, not MVP scope.** The MVP uses fixed bps thresholds (200 bps observable, 400 bps tradeable) per the strategy 3 scoping document. After the 30-day evaluation, if data is rich enough, pairs trading frame becomes the natural upgrade path.

### 11.5 Explicitly out of scope (reconfirmed)

These items appeared in the ideas-list review and are explicitly NOT going on the roadmap. Listed here so they don't get re-proposed without new evidence.

- **ML / deep learning / neural networks in the trading loop** — covered in Section 7. Sample size insufficient (≤200 settled trades per strategy), would overfit and destroy interpretability.
- **LLMs in the trading decision loop** — covered in Section 7. Antipattern; numerical prediction isn't what LLMs do well.
- **Equity research tools (DCF models, screeners, earnings analysis)** — your bot trades prediction markets and crypto derivatives, not equities. Wrong domain.
- **Bank/firm-style framework labels ("Goldman-grade", "Citadel-grade", etc.)** — not actionable; multi-decade efforts by hundreds of engineers, not a personal project scope.
- **Time-series momentum, macro regime allocation, factor models** — apply to continuous-price multi-asset portfolios, not your prediction market structure.
- **Mean reversion as a primary strategy** — doesn't fit the binary/barrier resolution structure of your markets.
- **HFT-style techniques (spectral decomposition, cross-exchange order flow at the microstructure level)** — wrong scale and latency profile for retail prediction markets.
- **Implied volatility surface builder** — you don't trade options.
- **CVaR portfolio optimization** — premature at 5-position scale.

### 11.6 Update protocol for this section

Section 11 grows over time as new ideas surface. The discipline:

- New ideas go into 11.1 (contingent), 11.2 (tools), or 11.3 (concepts) as appropriate.
- Items that get **promoted** to active roadmap (Section 8) move out of Section 11.
- Items that get **explicitly rejected** move to 11.5 with reasoning.
- This section is **descriptive** of "what we're considering," not **prescriptive** of "what we'll build."
