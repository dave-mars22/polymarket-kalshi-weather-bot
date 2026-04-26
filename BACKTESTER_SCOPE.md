# BACKTESTER_SCOPE.md — Historical backtester for the BTC technical brain

*Scoping document for the historical backtester project. Companion to RESEARCH_NOTES.md (project memory) and STRATEGY3_SCOPE.md (cross-platform arbitrage scoping). This document is the source of truth for the backtester project's design decisions, scope boundaries, validation criteria, and build sequence. Referenced repeatedly during the multi-session build.*

*Created in slice N6 (2026-04-25). Updated as the build progresses per Section 14.*

---

## 1. Status

- **Status:** SCOPED, NOT YET BUILT.
- **Date scoped:** 2026-04-25 (slice N6).
- **Target first end-to-end backtest run:** ~2-3 weekends from scoping.
- **Target validation against live results:** ~3-4 weekends from scoping.
- **Target completion (validated and producing useful outputs):** ~5-6 weekends from scoping.

These are intent-based targets, not commitments. Step 5 (validation) is the highest-risk step; it can blow the schedule if reconstruction is even slightly off.

---

## 2. Hypothesis / motivation

The BTC technical brain has 758 live trades over multiple weeks. Live data answers *"is the strategy working?"* but cannot answer parameter-sensitivity questions like *"what would PnL have been at edge threshold 0.04 vs 0.05 vs 0.06?"* without changing parameters mid-flight, which corrupts the live experiment. A historical backtester lets us answer those questions safely — replay history under different parameter settings to evaluate hypotheticals without touching the live bot.

### What this backtester is NOT for

- **Not a forward predictor.** Backtest results say "this is what would have happened in the past." They do NOT say "this will happen in the future." Edge that survived a backtest is necessary, not sufficient, for live edge.
- **Not a substitute for live evaluation.** The May 21, 2026 BTC technical brain checkpoint (RESEARCH_NOTES Section 6) stays the primary validation gate. Backtest results inform parameter discussions; they do NOT replace the live cohort evaluation.
- **Not a tool for tweaking parameters until they look good in backtest, then deploying.** That's overfitting to historical noise. The bar for acting on backtest findings is "the change is also justifiable on first-principles grounds and survives sensitivity analysis across parameter neighborhoods" — not "this single config produced the highest historical PnL."
- **Not a foundation for ML.** Per RESEARCH_NOTES Section 7, ML in the trading loop is explicitly out of scope. The backtester is for backtesting, not for generating training data.

---

## 3. Scope (MVP)

**Strategy in scope:** BTC technical brain ONLY for MVP.

- Polymarket 5-minute Up/Down BTC binary markets
- Existing signal generation logic (RSI, momentum, VWAP, SMA — whatever `scan_for_signals` currently computes for BTC)
- Existing decision logic (`MIN_EDGE_THRESHOLD`, `KELLY_FRACTION`, `MAX_TRADE_SIZE`, `DAILY_LOSS_LIMIT`)

**Not in MVP scope** (each is a separate future scoping project):

- **MC barrier brain backtesting.** Different platform (Kalshi), different settlement model (path-dependent barrier touch vs point-in-time outcome), different feature reconstruction (volatility windows, drift estimation, GBM pricing). Will be scoped as its own project after MC settlement data has either validated or killed the MC brain (decision gate per RESEARCH_NOTES Section 6: April 30 monthly + mid-May daily checkpoints).
- **Strategy 3 (cross-platform arbitrage) backtesting.** Strategy 3 doesn't exist yet to backtest. See `STRATEGY3_SCOPE.md` for that project's status.
- **Other underlyings** (ETH/SOL/XRP). Not in MVP. Future expansion contingent on MVP working and on ETH/SOL/XRP technical brains accumulating enough live data to validate against.

---

## 4. Hypotheses the backtester is built to answer

When complete and validated, the backtester should be able to answer questions in this shape. These define what "useful" means for the project; they inform what features the backtester needs without locking in implementation choices.

1. **Parameter sensitivity:** "What would BTC technical brain PnL have looked like at edge threshold 0.03 vs 0.05 vs 0.07?" — replays the same history with different gates, isolating the effect of one parameter at a time.
2. **Counterfactual constraints:** "What fraction of historical actionable signals would have been blocked by an MC-style daily concentration cap if it had been applied to the BTC technical brain?" — answers whether a constraint we're considering would have meaningfully changed past behavior.
3. **Regime-conditioned performance:** "How does win rate vary across market regimes (high vs low recent realized vol, trending vs choppy, by hour-of-day cohort)?" — feeds into the regime-detection roadmap item (RESEARCH_NOTES Section 8 "Possibly worth doing").
4. **Extended-history projection:** "If we'd been live for the last 90 days instead of ~30 days, what would the cumulative PnL trajectory look like?" — extends statistical power beyond what live data alone provides (subject to Section 6 fidelity caveats).
5. **Calibration check at depth:** "Does the model's predicted-vs-realized win rate hold up across edge buckets in a longer historical window than the live cohort gives us?" — the live calibration analysis (RESEARCH_NOTES Section 3c) ran on n=586 trades; backtest could give n in the thousands.

This list is not exhaustive. New questions will surface during the build and after first results land — Section 13 captures open questions and Section 14 documents the update protocol.

---

## 5. Data requirements

### Polymarket historical data

- BTC 5-minute Up/Down binary markets, going back as far as Polymarket's gamma-api allows.
- For each market: market metadata (event slug, creation time, resolution time, strikes), the resolved outcome, and any displayed price snapshots that can be reconstructed from gamma-api's historical endpoints.
- **Verification step required during Step 2:** confirm Polymarket's API actually exposes 5-minute BTC binary markets back to a useful depth (target: 90+ days). If it doesn't, scope contracts to whatever is available and document the limitation.

### Coinbase BTC-USD candles

- 1-minute granularity over the same period as Polymarket data.
- Used to reconstruct what the bot's signal-generation code would have seen at scan time (the live bot uses Binance 1m klines with Coinbase fallback per `backend/data/crypto.py`; for backtest, Coinbase is the more reliable historical source).
- The bot's existing `backend.data.price_history.fetch_daily_closes` caps at 300 candles per request; the backtester will need its own fetcher that paginates beyond that cap.

### Critical constraint

Data must be sufficient to **reconstruct features the bot actually computes at scan time**. This is non-negotiable. If the backtester can't reproduce the bot's RSI / momentum / VWAP / SMA computations from historical Coinbase data, the backtester is fundamentally broken regardless of what else it does. The Step 5 validation gate exists specifically to catch this class of failure.

### Storage

A separate SQLite database, `backtester_data.db`, sitting alongside `tradingbot.db` at the repo root. Historical data is isolated from the live bot's database; live and backtest data **never co-mingle**. Schema mirrors the relevant subset of `backend/models/database.py` (Trade, Signal) plus new tables for raw historical market and candle data.

---

## 6. Simulation fidelity

### Fees

Model accurately using the existing `backend.core.fees` module. The fee model is already validated for live trading (Polymarket: 0% exchange + 10 bps slippage + $0.10 gas; Kalshi formulas in the same module though out-of-scope for MVP). **Reuse, don't reimplement.** The backtester importing `backend.core.fees` is exactly the right coupling — it guarantees the backtester's fee math equals the live bot's.

### Fills

Assume fills at displayed prices for MVP. This is a known simplification. The technical brain's live execution path also assumes this (no order-book walk, no depth check), so the backtester matching this assumption is internally consistent — the gap is between both of them and reality, not between backtester and live bot.

### Slippage

**NOT modeled in MVP.** Documented gap.

**Justification:** modeling slippage requires inventing parameters not grounded in real data. Polymarket's historical API doesn't reliably expose order-book depth at trade time. Inventing a slippage model would add false sophistication without adding real validity. Better to be honest about the simplification than to bury it under a plausible-looking number.

### Implication for results (asymmetric framing)

- Backtest PnL will systematically **OVERSTATE** real-world PnL by some unknown amount.
- If backtest shows positive edge, real-world edge is unknown but **probably less**.
- If backtest shows negative or zero edge, real-world edge is almost certainly **worse** — a clear kill signal for the parameter setting under test.
- If backtest shows a large positive edge that disappears at small parameter perturbations, that's overfitting to history, not real edge.

This asymmetric framing is the right way to interpret backtest results: backtest can disprove edge, can flag overfitting, and can rank parameter settings against each other — but it cannot positively prove forward edge.

### Bankroll model

- Start each backtest with the same `INITIAL_BANKROLL` ($200) the live bot started with.
- Apply Kelly sizing the same way the live bot does (`KELLY_FRACTION = 0.10`, capped at 5% bankroll, hard cap at `MAX_TRADE_SIZE = $10`).
- Apply daily loss limits the same way the live bot does (`DAILY_LOSS_LIMIT = $80` halts new trades for the rest of the UTC day).
- Compound winnings the same way (bankroll grows/shrinks with realized PnL; sizing reads the current bankroll, not the initial).

### Time-of-trade snapshot semantics

The bot's live decision uses prices at scan time. The backtester must use prices at scan time too — NO use of any data from after the simulated scan timestamp, even data that was technically available (e.g., the eventual outcome). This is the look-ahead-bias gate; failing it makes the backtester worthless.

---

## 7. Outputs

### Per-trade simulated results

A table with the same schema as the live `Trade` table, written to `backtester_data.db`. Each row represents a simulated trade with all the same fields the live bot records: `market_ticker`, `direction`, `entry_price`, `size`, `model_probability`, `edge_at_entry`, `settled`, `settlement_value`, `pnl`, `features`, etc. This per-trade granularity is what makes downstream analysis (parameter sweeps, regime conditioning, calibration) possible.

### Aggregate outputs

- **PnL curve** over time (cumulative bankroll vs date).
- **Per-day win rate.**
- **Per-day trade count.**
- **Total trades, total PnL, win/loss ratio.**
- **Edge distribution at entry** (histogram of `edge_at_entry` values across all simulated trades).
- **Calibration table:** for each edge bucket (e.g., 5-6%, 6-7%, ...), predicted vs realized win rate. Same format as the live calibration analysis (RESEARCH_NOTES Section 3c) so the two can be visually compared.

### Comparison outputs

Side-by-side comparison of backtest results vs live results over the same date range. This is the artifact used for Section 8 validation; producing it is the verification step that confirms the backtester matches reality on the in-sample data we can check against.

### Output format

Structured data (JSON or CSV) plus a simple text report summarizing key numbers. **NO new frontend integration in MVP. NO new dashboard widgets.** The backtester runs from the command line and produces files; visualizing results is a future enhancement, NOT a Step 6 deliverable.

---

## 8. Validation criteria (load-bearing)

The backtester is **NOT trustworthy** until it passes the test below. Do not draw conclusions from backtest results until validation passes. A backtester whose results don't match live data on the in-sample period is worse than no backtester at all — it gives false confidence in conclusions drawn from out-of-sample analyses.

### Primary validation

Replay the last 30 days of live trading through the backtester. The backtester must produce trade decisions and PnL that match the live bot's actual results within tight tolerance.

Specifically:

- **Trade decisions must match exactly.** For each scan cycle in the replay window, the backtester must decide to trade (or not trade) the same markets with the same direction and the same size as the live bot did. Mismatches indicate the backtester's signal generation, decision logic, or feature reconstruction is wrong.
- **PnL per trade must match within $0.01** (rounding tolerance). PnL is a deterministic function of entry price, exit price, size, and fees — there's no excuse for it to differ from live results once the trade-decision match is established.
- **Aggregate cumulative PnL** over the 30-day window must match within $0.10.

If the backtester fails this validation, **it is not done**. Fix or scope down until it passes. Do not move on to Step 6 with a known-broken validation.

### Secondary validation

The backtester's calibration table over the validation window should match the live bot's calibration table. This catches subtle bugs that don't affect trade decisions but distort feature recording (e.g., off-by-one indexing on a windowed feature that happens to wash out at the trade-decision level but shows up in feature distributions).

### Gate

**Only after primary validation passes is the backtester usable for forward-looking analysis** (parameter sweeps, regime studies, extended-history projections). Until then, treat any backtest output with active suspicion — investigate every discrepancy rather than waving them off.

---

## 9. Build sequence

Each step produces evaluable output before the next is built. Stop and evaluate at each gate.

### Step 1 — Scoping document committed (this slice, N6)

This document. Captures design decisions before any code is written so the build doesn't drift from intent.

### Step 2 — Historical data ingestion layer

**Build:** code that pulls Polymarket BTC 5-min market history and Coinbase BTC-USD 1-min candles into `backtester_data.db`. Idempotent (can re-run without duplicates). Resumable (can pick up where a previous run left off if interrupted).

**Output:** a populated `backtester_data.db` with [N] days of historical data. Verify by counting markets, checking date range, spot-checking a few records against live API responses.

**Slice prefix:** TBD when build starts. Likely a new prefix (not S/T/C/P/N/A/B/E) since the backtester is its own project; finalize the prefix in the first Step-2 commit.

### Step 3 — Feature reconstruction layer

**Build:** code that, given a `(market, scan_timestamp)` pair, reconstructs the feature vector the bot's signal generation code would have produced at that moment.

**Critical:** reuse the existing `scan_for_signals` code path, **NOT a re-implementation**. Wrapping vs. reimplementing is the difference between a valid backtester and a fictional one. The bot's real behavior is the source of truth; the backtester drives the same code with historical inputs.

**Output:** a function `reconstruct_features(market, timestamp) -> FeatureDict` that produces values matching the live bot's recorded features within rounding tolerance. Verify by comparing reconstructed features for known historical trades against the bot's recorded values for those trades.

### Step 4 — Backtest execution engine

**Build:** code that iterates over historical scan timestamps, calls `reconstruct_features`, applies the bot's decision logic (edge threshold, Kelly, etc.), simulates fills at displayed prices, models fees correctly via `backend.core.fees`, applies daily loss limits, and writes simulated trades to `backtester_data.db`.

**Output:** a `backtester_data.db` full of simulated trades. Verify by running over a 1-week window and spot-checking a few simulated decisions against what the live bot would have done.

### Step 5 — PRIMARY VALIDATION (per Section 8)

Replay the last 30 days of live trading. Compare results. If validation fails, return to Step 3 or Step 4 and fix. **The backtester is not done until validation passes.**

### Step 6 — Output generation

**Build:** code that produces the aggregate outputs (Section 7) from `backtester_data.db`. PnL curve, win rate, calibration table, parameter-sensitivity sweeps.

**Output:** structured files (JSON/CSV) plus a text report summarizing key numbers.

### Step 7 — Documentation update

Update RESEARCH_NOTES.md to reference the validated backtester as a tool. Update Section 8 (roadmap) to reflect completion. Capture any findings from initial backtest runs in Section 3 as new diagnostic findings (e.g., 3l, 3m...). Per RESEARCH_NOTES Section 14a, append rather than replace.

---

## 10. Estimated effort

Given the user's stated 30+ hour/week bandwidth and assuming focused weekend sessions:

| step | description | estimated effort |
|---|---|---|
| 2 | data ingestion | ~10–15 hours (1 weekend) |
| 3 | feature reconstruction | ~15–25 hours (1–2 weekends; depends on how much existing code wraps cleanly vs needs adaptation) |
| 4 | execution engine | ~10–15 hours (1 weekend) |
| 5 | validation + fix | ~10–20 hours (1 weekend, possibly more if validation fails) |
| 6 | output generation | ~5–10 hours (half-weekend) |

**Total to validated backtester: 4–6 weekends.**

**The validation step (Step 5) is the highest-risk step.** If reconstruction is even slightly off, validation will fail and require Step 3 rework. Build the validation harness before celebrating Step 4.

---

## 11. Out of scope (explicit)

Items that may seem in-scope but are NOT for the MVP:

- **MC barrier brain backtesting** — separate future project, see Section 3.
- **Strategy 3 backtesting** — strategy 3 doesn't exist yet.
- **Other underlyings** (ETH/SOL/XRP) — future expansion contingent on MVP working.
- **Slippage modeling** — Section 6 documents the gap and the asymmetric framing for interpreting results.
- **Order book depth modeling** — out of scope; backtester uses displayed prices, matching the live bot's assumption.
- **Regime detection / clustering** — backtester *exposes* regime data via per-trade outputs but does not itself classify regimes.
- **Machine learning of any kind** — RESEARCH_NOTES Section 7.
- **LLMs in the loop** — RESEARCH_NOTES Section 7.
- **Frontend integration** — no dashboard widgets in MVP.
- **Real-money execution** — the backtester only simulates; no live trades originate from it.
- **Polymarket order book reconstruction** — just use displayed prices.
- **Historical Kalshi data** — this is for the BTC technical brain, which trades Polymarket only.

---

## 12. Connection to existing roadmap

### Reuses from existing infrastructure

- `backend.core.signals.scan_for_signals` — must be wrapped, not reimplemented.
- `backend.core.fees` — fee model is already validated for live trading; reuse directly.
- `backend.models.database` — Trade and Signal table schemas (extending to a separate `backtester_data.db`).
- Kelly sizing logic from `backend.core.signals.calculate_kelly_size`.
- Daily loss limit logic from `backend.core.scheduler` (the daily-PnL query + circuit breaker).
- BotState bankroll logic — backtester maintains its own `BotState` row in `backtester_data.db` mirroring how the live bot evolves bankroll.

### Updates required to RESEARCH_NOTES.md when the backtester lands

- **Section 8 (roadmap):** mark backtester complete; move it from "Probably worth doing" #1 to the "Completed since last update" table per Section 14c.
- **Section 9 (active research questions):** add any new questions surfaced by initial backtest runs.
- **Section 3 (diagnostic findings):** add findings from running the backtester (e.g., "3l. Edge-threshold sensitivity — backtest of 90 days at thresholds 0.03/0.04/0.05/0.06/0.07 shows ..."). Per Section 14a, findings are append-only.
- **Section 1:** if the backtester produces a stable second top-level companion artifact beyond `BACKTESTER_SCOPE.md` (e.g., a permanent `BACKTESTER_RESULTS.md`), add it to the companion list per Section 14f. Per-run output files are NOT companion artifacts.

### Blocks / blocked-by

- **Not blocked by anything currently in flight.**
- **Does NOT block strategy 3 build** — they can proceed in parallel.
- **DOES block** any future "tune the BTC technical brain's parameters" work — without backtester validation, parameter tuning is overfitting to live data.
- **DOES block** any future "compare different signal-generation approaches" work for the same reason.

---

## 13. Open questions (to resolve during build)

Questions deferred to build time, not scoping time:

1. **How far back does Polymarket's historical API actually expose 5-minute BTC markets?** Verify in Step 2. Target: 90+ days. If shorter, scope contracts to what's available and document the limitation.
2. **Does the bot's existing signal generation code have side effects** (database writes, log lines, cache mutations) that complicate using it in a backtest harness? Audit in Step 3 before wrapping.
3. **What's the right level of parameter sweep granularity for Step 6 outputs?** E.g., for edge threshold sweep, is 0.01-step granularity enough or do we need 0.005? Decide based on what initial runs reveal.
4. **Does the backtester need to handle bot restarts / crashes** that affected live trading on certain dates? E.g., if the live bot was down for 3 hours on April 19, should the backtester also "not trade" during that window for fair comparison? Tentative answer: yes, to keep validation honest. Precise mechanism (downtime ledger? per-minute heartbeat reconstruction from `BotState` updates?) TBD in Step 5.
5. **What slice prefix does the build use?** TBD when Step 2 starts. Candidates: `K` (backtester — distinct from B which is bug-fix), `H` (historical), or extend `T` (tooling) since the backtester is fundamentally a diagnostic tool. Decision deferred so the prefix is chosen in the context of the first build commit, not in advance.

---

## 14. Update protocol

This document evolves as the backtester is built and validated.

- **Open questions (Section 13)** get resolved and documented as the build progresses. Resolved questions move from Section 13 into the relevant section (e.g., a resolved data-depth question updates Section 5).
- **Build progress (Section 9)** gets updated as steps land. Each completed step gets a "**STATUS:** completed in commit `<hash>` on `<date>`" line.
- **Validation results (Section 8)** get recorded when Step 5 completes. Include exact discrepancy counts (trades matched / mismatched, PnL gap in dollars, calibration-table comparison summary).
- **Initial backtest results** get summarized in a new Section 15 once the backtester is producing useful outputs. Same append-only convention as RESEARCH_NOTES Section 3.

### Slice prefix conventions

- **Documentation-only updates** to this file use slice prefix `N` (e.g., `N6` for the slice creating this document, `N7+` for future updates). Cross-references in RESEARCH_NOTES.md follow Section 14d/14f conventions.
- **Code changes** for the backtester build (Steps 2–6) use the build-prefix to be decided when Step 2 starts. The prefix gets recorded in this document's Section 13 question 5 once chosen, and added to RESEARCH_NOTES.md Section 10's slice taxonomy per Section 14d.
- **Validation findings** that make their way into RESEARCH_NOTES.md as diagnostic findings (Section 3 there) follow that document's conventions; this document just notes the cross-reference.
