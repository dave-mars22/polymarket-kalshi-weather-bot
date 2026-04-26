# RESEARCH_NOTES.md

*Project research memory. Companion to README.md (what the code does), ARCHITECTURE.md (historical, pre-rebuild), STRATEGY3_SCOPE.md (cross-platform arbitrage scoping doc), and BACKTESTER_SCOPE.md (historical backtester scoping doc). This document captures what we have **learned**, what we have **decided**, and what we plan to **investigate**. It is updated as the project evolves — every diagnostic, parameter change, and checkpoint adds to it.*

*Last meaningful update: 2026-04-25 (slice N7 — capture T2 settlement-test findings: gross-of-fees PnL accounting + Polymarket 422 fallback gap).*

---

## 1. Project Overview

A simulated multi-strategy trading bot operating on Polymarket and Kalshi prediction markets. Two strategies run in parallel:

- **Multi-crypto technical brain** — RSI / momentum / VWAP / SMA composite signal on Polymarket 5-minute up/down binaries for BTC, ETH, SOL, XRP.
- **Monte Carlo barrier brain** — closed-form GBM (geometric Brownian motion) pricing on Kalshi BTC barrier options across daily/monthly/yearly cadences.

**Currently tested hypothesis:** that the technical brain (RSI/momentum/VWAP/SMA on Polymarket 5-min crypto binaries) and the MC barrier brain (closed-form GBM pricing on Kalshi BTC barriers) can find systematic edge sufficient to overcome fees on retail-accessible prediction markets — at scales small enough to remain in pricing windows where market makers don't fully arbitrage edge away.

**Mode:** simulation only. No real capital deployed. `SIMULATION_MODE = True` and is the gate to Version C (real-money operation), which won't open without falsifiable evidence of edge per Section 6.

---

## 2. Current Strategy State

*Last updated: 2026-04-25 (post-B1; first MC settlements landed today).*

Two strategies running simultaneously, plus a third under scoping:

- **Technical brain**: scans 4 underlyings (BTC/ETH/SOL/XRP) on Polymarket 5-min markets every 60 s. Generates probability estimates from RSI / momentum (1m, 5m, 15m) / VWAP deviation / SMA crossover composite. Trades when `|edge| ≥ 5%`. ~133 trades/day.
- **MC barrier brain**: scans 8 Kalshi crypto series (5 BTC + 3 altcoin) every 600 s. Prices barrier options via closed-form GBM with EWMA vol estimation. Trades when `net_edge ≥ 5%` AND quote hasn't drifted between scan and fill. ~4 trades/day post-S2. **First settlements landed 2026-04-25** (3 KXBTCD daily contracts, all losses; see Section 3i) — prior to slice B1, every MC trade had been silently stuck in pending state because of an unreachable settlement code path (see Section 3j).
- **Cross-platform arbitrage (Strategy 3)** — scoped only, not implemented. Hypothesis: persistent spreads between Polymarket BTC binaries and Kalshi KXBTCD barriers can be harvested. Build sequence and evaluation criteria are in `STRATEGY3_SCOPE.md` (top-level artifact alongside this file).

### Infrastructure state (as of 2026-04-25)

- `/api/dashboard` latency: ~2.0 s (was 9.7 s pre-P1, 4.0 s post-P1). Slice P1 parallelized the per-underlying scan loop via `asyncio.gather` (~58% reduction); slice P2 added scan-result caching keyed on `(underlying, scan_cycle_id)` so dashboard reads no longer re-run `scan_for_signals` (additional ~50% reduction). Both changes are correctness-neutral — they only affect latency.
- MC settlement loop: now actually runs end-to-end. Slice B1 replaced the credential-gated KalshiClient call in `_fetch_kalshi_resolution` with a direct httpx GET against the public Kalshi market endpoint. The credential gate had been bailing on every call since the MC brain shipped, leaving every MC trade pending forever.

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

### 3i. First MC settlements (n=3, 2026-04-25)

The first three MC trades to actually settle in the bot DB. All three are KXBTCD-26APR2517 daily-cadence Kalshi BTC barrier contracts that resolved at 17:00 EDT (21:00 UTC). They sat in pending state for hours after Kalshi's resolution because of the B1 bug; they settled within ~2 min of the post-B1 bot restart.

| trade | ticker | dir | size | entry | model_p | settlement_value | result | pnl |
|---:|---|---|---:|---:|---:|---:|---|---:|
| 708 | `KXBTCD-26APR2517-T78749.99` | yes | $13.02 | 0.140 | 0.292 | 0.0 | loss | −$1.82 |
| 709 | `KXBTCD-26APR2517-T77249.99` | no  | $12.46 | 0.280 | 0.400 | 1.0 | loss | −$3.49 |
| 728 | `KXBTCD-26APR2517-T78499.99` | yes | $14.37 | 0.100 | 0.285 | 0.0 | loss | −$1.44 |

**Total realized: −$6.75. Bot was 0-for-3 on direction.**

Per-trade interpretation:

- Trades 708 and 728 were low-probability long-yes bets (model_p ≈ 0.28–0.29). The model thought YES had ~28% probability and was being underpriced at 10–14¢. The losing outcome is the *modal* outcome the model itself predicted (~71% likely). These losses are individually consistent with the model.
- Trade 709 was a higher-conviction short-yes (long-no) bet. Model thought NO had ~60% probability; market priced NO at 28¢ — implying ~32 pp edge. The losing outcome is the model's *minority-case* prediction (~40% likely). Still individually consistent with the model, but the one that "should have" been more likely to win.

**Caveats — what this n=3 result does NOT tell us:**

- Wilson 95% CI on 0/3 is roughly **[0%, 71%]**. This data does not distinguish "working model that lost three coin flips" from "broken model".
- The April 30 KXBTCMAXMON settlements remain the formal first evaluation point per Section 6. Daily KXBTCD settlements between now and then will accumulate sample size; expect roughly 5–10 more daily-cadence settlements over the next 5 days at current open-position rate (n=8–13 daily-cohort points by month-end).
- **Pricer-accuracy cross-check (TODO):** Kalshi's `expiration_value` for KXBTCD-26APR2517 was **$77,494.41** (BRTI). The MC pricer uses Coinbase BTC-USD as its underlying source. Before drawing any conclusion about pricer accuracy from these settlements, verify that the bot's underlying price snapshot at 17:00 EDT today matches Kalshi's BRTI close to within a reasonable tolerance. A material divergence would mean the pricer was solving the right model on the wrong inputs.

### 3j. Audit blind spot — unreachable code paths (2026-04-25)

The B1 bug surfaced a methodological gap in the comprehensive April 25 audit (`AUDIT_2026-04-25.md`, 30 findings). The audit cataloged what the code **does** but did not flag that `_fetch_kalshi_resolution` returned `(False, None)` without ever making an API call due to an incorrect `kalshi_credentials_present()` gate. Every MC trade had been silently stuck in pending state from the moment the MC brain shipped — a complete strategy-level failure that the audit missed because the code at the gate looked plausible in isolation.

**Generalization:** audit-style code reviews are good at finding what's *there* (style issues, race conditions in code that runs, dead branches in code that runs sometimes) and less good at finding code paths that **never execute** in production. A function that always early-returns on a precondition isn't "broken" by any local code-quality measure — it's only broken in the context of "the strategy depends on this returning real data."

**Mitigations for future audits:**

- Add an explicit "trace every code path from scheduler entry to external API call" check. For each scheduled job, confirm: (a) the function actually runs, (b) it makes the network calls it implies it does, (c) it processes results downstream rather than silently bailing.
- Consider integration tests that verify settlement (and other end-to-end flows) against real or recorded external responses. Unit tests with mocks pass even when the live code path can never reach the mocked function.
- Future audits should be reviewed for similar unreachable-path risks **before** their findings list is treated as authoritative. A 30-finding audit that misses a single complete-strategy-failure is worse than no audit if its comprehensiveness creates false confidence.

This finding is methodologically important enough to flag here (rather than just in commit history) so future audits inherit the lesson.

### 3k. Pricer implementation correctness verified (E1, 2026-04-25)

The E1 GBM derivation notebook (`notebooks/gbm_derivation.ipynb`) independently re-derived the closed-form one-touch-above probability under GBM with drift and compared against the production implementation on a canonical test case ($S_0 = \$77{,}000$, $B = \$80{,}000$, $\mu = 0.10$/yr, $\sigma = 0.60$/yr, $T = 7$ d):

- Notebook closed-form result: **0.6400293920**
- Production `prob_one_touch_above_analytic`: **0.6400293920**
- Difference: **0.00e+00** (matches to floating-point precision)

The notebook also ran a from-scratch Monte Carlo simulator (10,000 paths) at three timestep granularities and showed convergence onto the closed-form: at 10,080 ~1-min steps the empirical estimate is 0.6397, gap 0.0003, well within Monte Carlo standard error. Both an algebraic re-derivation and a numerical simulator agree with production to as much precision as either method offers.

**Implication for future calibration work:** when MC settlement data eventually shows divergence between predicted and realized touch rates (Section 6 decision gate, mid-May 2026), the divergence will come from **model assumptions**, not implementation bugs in the closed-form code. Don't waste investigation time grepping the pricer for arithmetic errors — the math is provably correct as coded. Look at the model's input assumptions (vol estimation, drift, GBM-vs-reality gap from Section 5 of the notebook) instead.

**Limitation — what E1 did NOT verify:**

- E1 verified `prob_one_touch_above_analytic` on one canonical test case. The mirror function `prob_one_touch_below_analytic` is implied correct by the same reflection-principle symmetry argument, but was not directly numerically tested.
- The notebook's MC simulator is a notebook-private implementation, separate from the bot's production `simulate_paths` function in `backend/core/monte_carlo.py`. The production path-dependent simulator is **NOT** verified by E1. A future verification slice should run an analogous notebook-vs-production check on `simulate_paths` and any other pricer functions the bot uses.
- Verification was at one $(S_0, B, \mu, \sigma, T)$ point. Edge cases (very deep OTM, very long $T$, $\sigma \to 0$) were not exercised. The production code has guards for the corner cases ($B \le S_0$, etc.) but their numerical behavior at extreme parameters wasn't directly stress-tested.

### 3l. PnL accounting is gross-of-fees (T2 finding, 2026-04-25)

While writing T2's settlement test coverage, the test author confirmed that `calculate_pnl(trade, settlement_value)` in `backend/core/settlement.py` computes settlement PnL as:

- on win: `pnl = size * (1.0 - entry_price)`
- on loss: `pnl = -size * entry_price`

with **no reference to `backend.core.fees` at the settlement boundary**. Fees are absorbed earlier — at signal-gate time, via `net_edge()`, which subtracts an estimated round-trip fee from raw edge before the trade is allowed through the `MIN_EDGE_THRESHOLD` filter (see `backend/core/fees.py:net_edge` and the execution-realism handoff doc earlier in this session). The settlement-time PnL number is therefore **gross-of-fees** by current design.

**Implication for the project's PnL data:**

- Every PnL number recorded in the database — every `Trade.pnl`, the cumulative `BotState.total_pnl`, the bankroll display — has a known systematic positive bias relative to true net realized PnL.
- The size of the bias depends on per-trade fees, which vary by platform (Polymarket: 0% exchange + 10 bps slippage + $0.10 gas; Kalshi: per-contract `ceil(0.07 * p * (1-p) * 100) / 100` + 50 bps slippage) and by trade size. At Polymarket's $1–10 trade sizes the gas component dominates; at Kalshi's $13–17 sizes the per-contract fee dominates.

**Interpretation rule when reading PnL data:**

When evaluating performance, mentally subtract estimated round-trip fees per trade to get a more honest net number. Or, more rigorously, `SELECT SUM(pnl) FROM trades WHERE settled = 1` and subtract estimated total fees as a separate adjustment using `fees.estimate_round_trip_cost` per trade. Across 758 settled trades the aggregate fee impact is small per-trade but potentially meaningful in total — quantification deferred to the execution-realism work.

**Status:**

This is **intentional by current design, not a bug**. The execution-realism review (separate scoping doc, not yet a slice) plans to add a `Trade.fees_paid` column and deduct fees at settlement; when that ships, this finding gets a `[SUPERSEDED by 3X]` tag per Section 14a's append-only convention. The new tests in `TestCalculatePnl` (T2) explicitly pin the current gross-of-fees contract — when the execution-realism slice changes the contract, those tests need updated expected values.

---

## 4. Code Change Log (slice log)

*Each entry: what changed, runtime impact, rationale, evaluation/revert criteria. Renamed from "Parameter Changes" in slice N4 — the section now covers parameter tuning (S-prefix), infrastructure/perf changes (P-prefix), and bug fixes (B-prefix), all of which materially change runtime behavior or correctness and deserve the same treatment.*

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

### Slice P1 — Parallelize per-underlying scan loop (committed 2026-04-25, hash `3ddbd82`)

- **What changed:** the technical-brain scan loop in `scan_for_signals` was rewritten to fan out per-underlying scans via `asyncio.gather` instead of running them sequentially. No behavior change — same scans, same ordering of results, same trade decisions. Pure latency optimization.
- **Runtime behavior:** `/api/dashboard` end-to-end latency dropped from ~9.7 s to ~4.0 s (~58% reduction). Eliminates the scheduler-overlap window that previously caused 60 s scan ticks to occasionally bunch up.
- **Rationale:** Audit Finding #1 (HIGH severity). Sequential scans were creating timing pressure on the 60 s scheduler. Latency was approaching the cycle interval.
- **Revert criterion:** none — correctness-neutral perf change. Would only revert if `asyncio.gather` exposed a previously-hidden race condition (none observed).

### Slice P2 — Cache scan results in dashboard endpoint (committed 2026-04-25, hash `dc0861f`)

- **What changed:** `/api/dashboard` previously re-ran `scan_for_signals` on every request to populate the live-signals tile. Slice P2 introduced a single-writer/multi-reader cache keyed on `(underlying, scan_cycle_id)` so the dashboard reuses the most recent scheduler-produced scan instead of re-computing.
- **Runtime behavior:** dashboard latency dropped from ~4.0 s (post-P1) to ~2.0 s (~50% additional reduction). No staleness concerns: cache is invalidated on every scheduler tick (60 s).
- **Rationale:** Audit Finding #2 (HIGH severity). Re-running the scan inside an HTTP handler was the second-largest dashboard latency contributor after sequential scanning.
- **Revert criterion:** none — correctness-neutral. Atomic single-name binding under CPython GIL is safe for single-writer/multi-reader.

### Slice B1 — Fix Kalshi settlement to use public market endpoint (committed 2026-04-25, hash `da6ce41`)

- **What changed:** `_fetch_kalshi_resolution` in `backend/core/settlement.py` previously called `kalshi_credentials_present()` and returned `(False, None)` if creds were missing. Replaced with a direct `httpx.AsyncClient` GET against `https://api.elections.kalshi.com/trade-api/v2/markets/{ticker}` (no auth required for reads). Added optional `http_client` injection seam matching the pattern in `mc_execution.fetch_current_ask`.
- **Runtime behavior:** MC settlement loop now actually runs end-to-end. First post-restart settlement_job (22:15 UTC, ~2 min after restart) settled all 3 KXBTCD-26APR2517 trades — see Section 3i. Prior to B1: zero MC trades had ever settled in the bot DB.
- **Rationale:** post-settlement analysis on the 3 KXBTCD-26APR2517 contracts (which Kalshi resolved at 17:00 EDT) found them still pending in the bot DB hours later. Tracing the loop pinpointed the credential gate as the silent failure point. The Kalshi public market endpoint requires no auth — the gate was unnecessary.
- **Tests:** 10 new unit tests in `tests/test_settlement.py` (first test_settlement.py in the project; audit T2 noted the gap). Covers happy paths, not-yet-resolved paths, error paths, defensive shape handling. Test count 197 → 207.
- **Revert criterion:** none — pure correctness fix. The code was non-functional before; it's functional now.
- **B is the bug-fix prefix.** Established here, distinct from S (strategy), T (tools), C (cleanup), P (performance), N (notes), A (arbitrage).

### Slice E1 — GBM derivation notebook + notebooks/ requirements split (committed 2026-04-25, hash `c4a985b`)

- **What changed:** new `notebooks/` directory holding learning artifacts that aren't imported by the bot. First notebook is `gbm_derivation.ipynb` — a 7-section walkthrough that derives the closed-form one-touch barrier formula from the reflection principle, builds an independent Monte Carlo simulator, walks GBM's failure modes on real BTC data, and reads the production `prob_one_touch_above_analytic` line by line. Also added `requirements-notebooks.txt` at repo root for matplotlib + jupyter tooling — kept separate from `requirements.txt` so the production deployment surface stays minimal.
- **Runtime behavior:** none. No production code was modified. `requirements.txt` is byte-for-byte unchanged. The bot is unaffected.
- **What was verified:** notebook closed-form result matches production `prob_one_touch_above_analytic` to floating-point precision (gap = 0.00e+00) on the canonical test case. Independent MC simulator with 10k paths × 10080 timesteps converges to the closed-form within Monte Carlo standard error. See Section 3k for full details and limitations.
- **Tests:** unchanged (notebook executes via `nbconvert --execute` cleanly; production test count remains 207). All 207 production tests pass post-install of notebook deps, confirming the new dependencies don't conflict with production package versions.
- **Revert criterion:** none — pure additive learning artifact. Would only "revert" by deleting the notebook directory if it became misleading or stale.
- **E is the education prefix.** Established here for learning artifacts (notebooks, derivations, walkthroughs). Joins S/T/C/P/N/A/B in the slice taxonomy.

### Slice T2 — settlement.py test coverage (committed 2026-04-25, hash `9fe4a04`)

- **What changed:** added 34 unit tests across 5 new test classes in `tests/test_settlement.py` covering `_parse_market_resolution` (pure parser, 6), `fetch_polymarket_resolution` (HTTP plumbing via `httpx.MockTransport`, 7), `calculate_pnl` (pure logic, 8), `settle_pending_trades` (orchestrator with in-memory SQLite + `mock.patch`, 8), and `update_bot_state_with_settlements` (BotState bookkeeping, 5). Test count 207 → 241. The B1 era's 10 `TestFetchKalshiResolution` tests were untouched.
- **Production code change:** one minimal addition — added optional `http_client` kwarg to `fetch_polymarket_resolution` matching B1's pattern exactly. Non-behavioral testability refactor; production callers leave it `None` and get the same per-call `AsyncClient`. Verified by re-running B1's tests against the modified module — still green.
- **Closes Audit Finding #4** (HIGH severity: settlement.py had zero tests pre-B1, partial post-B1, full post-T2).
- **Two findings surfaced during the test work**, captured in this document rather than left only in commit history: (1) Section 3l — `calculate_pnl` is gross-of-fees by design, with implications for how PnL data should be read; (2) Section 8 roadmap — B2, a small bug-fix slice to widen Polymarket fetch's HTTP-error handling beyond just 404 (gamma-api returns 422 for invalid market IDs).
- **Reusable infrastructure:** the in-memory SQLite fixture (`_make_in_memory_db`, `_make_pending_trade`, `_make_bot_state` helpers) is reusable for future settlement-area or other DB-touching tests. Per-test fresh engine, no shared state, no test-order dependencies. Negligible overhead.
- **Revert criterion:** none — pure test additions plus one non-behavioral seam.

---

## 5. Open Positions Snapshot

*Captured 2026-04-25 22:30 UTC, post-B1 settlements. This section ages quickly — the snapshot becomes stale within 24 h as new MC trades open and old ones settle. Update protocol: refresh whenever the open-positions list materially changes, but always preserve prior snapshots inline rather than rewriting them in place, so the history of position state is recoverable.*

### Current snapshot (post-B1, 2026-04-25)

2 open MC barrier positions, 0 open technical-brain positions (5-min markets clear within minutes by design):

| id | ticker | dir | size | entry | age | settles |
|---:|---|---|---:|---:|---:|---|
| 595 | `KXBTCMAXMON-BTC-26APR30-8000000` | yes | $16.59 | 0.510 | ~41 h | 2026-04-30 |
| 606 | `KXBTCMAXMON-BTC-26APR30-8250000` | yes | $13.66 | ~0.19 | ~40 h | 2026-04-30 |

Total invested in pending positions: ~$30. Total potential payout if all win: ~$60.

### Prior snapshot (pre-B1, 2026-04-25 ~14:00 UTC)

5 open MC barrier positions. 3 of these (708, 709, 728 — KXBTCD-26APR2517) settled on 2026-04-25 21:00 UTC and resolved as losses post-B1 (see Section 3i). Preserved here for historical reference:

| id | ticker | dir | size | entry | age | settles |
|---:|---|---|---:|---:|---:|---|
| 595 | `KXBTCMAXMON-BTC-26APR30-8000000` | yes | $16.59 | 0.510 | 24.1 h | 2026-04-30 |
| 606 | `KXBTCMAXMON-BTC-26APR30-8250000` | yes | $13.66 | 0.190 | 23.3 h | 2026-04-30 |
| 708 | `KXBTCD-26APR2517-T78749.99` | yes | $13.02 | 0.140 | 8.1 h | 2026-04-25 21:00 UTC → **settled, loss** |
| 709 | `KXBTCD-26APR2517-T77249.99` | no | $12.46 | 0.280 | 8.1 h | 2026-04-25 21:00 UTC → **settled, loss** |
| 728 | `KXBTCD-26APR2517-T78499.99` | yes | $14.37 | 0.100 | 0.5 h | 2026-04-25 21:00 UTC → **settled, loss** (post-S2 third position) |

### BotState (post-B1 settlements)

- Bankroll: **$195.69**
- Cumulative `total_pnl`: **−$4.31**
- `total_trades`: **758**, `winning_trades`: **374** (all-time technical brain)
- `BotState.total_pnl` ↔ `SUM(Trade.pnl)` drift remains documented (Section 7); not in scope to reconcile.

---

## 6. Falsifiable Success Criteria

*Specific, dated checkpoints. Outcomes feed back into Sections 4 (parameter decisions) and 8 (roadmap re-prioritization).*

### 2026-04-25 — first MC settlements (RESOLVED)

The first MC settlements actually occurred on 2026-04-25 (3 KXBTCD daily contracts), earlier than originally framed. All three resolved as losses; full data and interpretation in Section 3i. n=3 is statistically meaningless — Wilson 95% CI is roughly [0%, 71%], so this checkpoint did not produce evidence either way. Kept here as a record that the checkpoint fired and what it told us (nothing definitive).

### 2026-04-30 — first KXBTCMAXMON monthly settlements

*Reframed in slice N4: the original "first MC barrier settlements" framing implied April 30 was the first settlement event, but daily KXBTCD settlements have been arriving since April 25. April 30 is now the first **monthly-cadence** settlement event.*

- 2× KXBTCMAXMON-26APR30 positions (trades 595, 606) resolve.
- Plus accumulated KXBTCD daily settlements between 2026-04-25 and 2026-04-30 (expected n=8–13 daily-cohort points by month-end at current open-position rate).
- **SUCCESS:** combined daily + monthly settlements broadly align with model predictions (positions entered at >0.5 prob settle yes more often than not, scaled appropriately).
- **AMBIGUOUS:** small sample, mixed results, no clear signal.
- **FAILURE:** settlements contradict predictions strongly enough to warrant reverting S2 and questioning the GBM pricer.

### Interim — daily KXBTCD settlement accumulation (continuous)

*Added in slice N4. Now that B1 has unblocked settlements, daily-cadence settlements arrive ~daily and are the fastest way to accumulate sample.*

- Each KXBTCD contract settles within ~24 h of entry. At current open-position rate (post-S2 cap = 3 daily slots) and trade frequency (~4 MC trades/day), expect 5–10 additional daily settlements between 2026-04-25 and 2026-04-30, then ~30/month thereafter.
- **Decision gate at n≈30 daily settlements (estimated mid-May 2026):** evaluate calibration of the GBM pricer on daily-cadence data. If realized win rate on entries with model_p > 0.5 cleanly differs from model_p — particularly if it's persistently lower — that's evidence the GBM-with-realized-vol pricing is mis-calibrated, which promotes the GARCH item in Section 11.1 from contingent to active.
- This is a continuous-monitoring checkpoint, not a single date.

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
- **Now actually evaluable** post-B1: prior to 2026-04-25, the bot's settlement loop never executed end-to-end, so this stopping criterion was unreachable in practice. The 90-day clock effectively starts 2026-04-25 (first real settlements), not the original `MC_PILOT_BANKROLL_USD` introduction date. Total elapsed days at this checkpoint: ~89 days of *settled* data.

### Calibration interpretation note (added in slice N5)

When evaluating MC calibration in upcoming settlements (April 30 monthly + ongoing daily), expect the bot to systematically **over-predict touch probabilities by an order of magnitude of 5-15 pp for purely structural reasons**. The bot's pricer is a continuous-time formula; Kalshi resolves discretely (at most one observation per contract, snapshotted at close time). A continuous-monitoring formula counts barrier crossings that bounce back before the next observation — which a discretely-monitored market cannot reward. So predicted touch rate > realized rate is the *expected* default, not evidence of a broken model.

The E1 notebook quantified this discrete-monitoring bias on a canonical 7-day daily-cadence example: at 7 daily timesteps the MC undershoots the continuous-time closed-form by ~13.9 pp; at hourly steps by ~3.4 pp; at ~1-min steps by ~0.03 pp. Real Kalshi monitoring is even sparser than 7 daily steps for end-of-day-snapshot products, so the structural bias on this bot's contracts is in the upper end of that range. The 5-15 pp framing is a notebook-derived order-of-magnitude estimate — **not a calibrated number specific to KXBTCD daily contracts** — and the exact magnitude depends on path volatility, barrier distance, and the contract's exact resolution mechanics (single-snapshot at close vs. any-time-during-the-day).

**Implication for the kill criteria above:**

- The kill criterion is NOT "predicted touch rate ≠ realized touch rate." A consistent predicted > realized gap of ~5-15 pp is the expected null behavior under a correctly-implemented GBM pricer applied to a discretely-monitored market.
- Real evidence of a broken model is calibration error **larger than this structural bias**, OR systematic error in **the opposite direction** (realized > predicted — meaning the market is touching MORE than even a continuous-monitoring formula predicts, which would point to fat tails or vol underestimation rather than the discrete-monitoring artifact).
- For the 2026-04-30 and mid-May n≈30 evaluations: do the calibration analysis, then compare the realized gap to the 5-15 pp structural-bias band before concluding anything. Specifically reject the model only if the gap is > ~20 pp in the over-prediction direction or any non-trivial gap in the under-prediction direction.

This note doesn't lower the bar for what counts as "model works" — it raises the bar for what counts as "model broken," to avoid mistaking known structural bias for genuine miscalibration.

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
6. **B2 — Widen Polymarket resolution-fetch HTTP error handling** — production code in `fetch_polymarket_resolution` triggers the search-fallback path only on HTTP 404, but Polymarket's gamma-api actually returns **HTTP 422** for invalid market IDs (verified during T2 implementation). A 422 response would not trigger the fallback and the function would silently return `(False, None)`, leaving the trade stuck pending forever — same failure pattern as the B1 Kalshi credential-gate bug (Section 3j). Scope: ~1 line change to extend the status-code check (e.g., `in (404, 422)`) plus 1-2 new tests. Priority **low-medium**: hypothetical impact is "if a market_id ever becomes invalid, the trade gets stuck pending forever"; likelihood unknown but probably rare since market_ids are stable once issued. Worth fixing for defensive robustness given how cheap it is.

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

*Listed in chronological commit order.*

| slice | commit | description | completed |
|---|---|---|---|
| **C1** | `3a40109` | Delete dormant `backend/ai/` scaffolding (1,133 LOC, 5 files + AILog model + 2 PyPI deps + 4 config settings) | 2026-04-25 |
| **C2** | `b3b5653` | Sync `.env.example` — fix `KELLY_FRACTION` 0.25→0.10 drift, remove dead-econ-pipeline `FRED_API_KEY` / `BLS_API_KEY` placeholders | 2026-04-25 |
| **P1** | `3ddbd82` | Parallelize per-underlying scan loop in `scan_for_signals` via `asyncio.gather` — eliminates scheduler timing pressure (`/api/dashboard` 9.7s → 4.0s, ~58% reduction) | 2026-04-25 |
| **N3** | `c38fb00` | Add `STRATEGY3_SCOPE.md` — cross-platform arbitrage scoping doc (Polymarket BTC ↔ Kalshi KXBTCD pairs trade); top-level artifact alongside RESEARCH_NOTES | 2026-04-25 |
| **P2** | `dc0861f` | Cache `scan_for_signals` results keyed on `(underlying, scan_cycle_id)` so dashboard reuses scheduler-produced scans (`/api/dashboard` 4.0s → ~2.0s, additional ~50% reduction) | 2026-04-25 |
| **B1** | `da6ce41` | Fix Kalshi settlement to use public market endpoint (no credentials needed) — replaces silently-failing credential gate. **First MC settlements ever recorded in DB.** +10 unit tests in new `tests/test_settlement.py` (197→207). | 2026-04-25 |
| **N4** | `ebeded8` | RESEARCH_NOTES update: capture first MC settlements (Section 3i), audit blind spot finding (Section 3j), reframe Section 6 checkpoints around April 25 first-settlements, document the C/P/B prefix taxonomy (Section 10) | 2026-04-25 |
| **E1** | `c4a985b` | Add `notebooks/gbm_derivation.ipynb` — derivation, MC verification, failure-mode analysis, line-by-line read of `prob_one_touch_above_analytic`. Verified production matches notebook to floating-point precision (Section 3k). New `requirements-notebooks.txt` keeps notebook deps out of production `requirements.txt`. | 2026-04-25 |
| **N5** | `ae63d5b` | RESEARCH_NOTES update: capture E1 findings (Section 3k pricer correctness, Section 6 calibration interpretation note for the 5-15 pp discrete-monitoring bias band, Section 11.1 GARCH-justification sharpening); add E to slice taxonomy (Section 10); introduce Section 14 document update protocol | 2026-04-25 |
| **N6** | `bc0c351` | Add `BACKTESTER_SCOPE.md` — historical backtester scoping doc (BTC technical brain MVP only; MC and strategy-3 backtesting deferred). Section 1 cross-reference updated per Section 14f convention | 2026-04-25 |
| **T2** | `9fe4a04` | Settlement.py test coverage — 5 new test classes covering `_parse_market_resolution`, `fetch_polymarket_resolution`, `calculate_pnl`, `settle_pending_trades`, `update_bot_state_with_settlements` (+34 tests, 207 → 241). Closes Audit Finding #4. One non-behavioral seam added (`http_client` kwarg on `fetch_polymarket_resolution` matching B1 pattern). Surfaced two findings: gross-of-fees PnL accounting (Section 3l) and Polymarket 422 fallback gap (Section 8 → B2 roadmap entry). | 2026-04-25 |

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
| Code change committed (S/P/B slice) | add a slice entry to Section 4 |
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
- Commit conventions — slice prefix taxonomy (current as of slice N5):
  - **S** — strategy/parameter changes that alter trade decisions or sizing (`MIN_EDGE_THRESHOLD`, concentration caps, etc.)
  - **T** — tooling and diagnostic harnesses (replay scripts, audit utilities)
  - **C** — cleanup and dead-code deletion
  - **P** — performance and infrastructure (correctness-neutral latency, parallelization, caching)
  - **N** — notes and documentation updates to this file or other top-level artifacts
  - **A** — arbitrage / strategy 3 work (introduced with `STRATEGY3_SCOPE.md`, slice N3)
  - **B** — bug fixes (introduced with B1; distinct from S because correctness-restoring rather than strategy-tuning)
  - **E** — education / learning artifacts (notebooks, derivations, walkthroughs that aren't imported by the bot but make production black-boxes legible). Introduced with E1 (`notebooks/gbm_derivation.ipynb`).

This document complements `README.md` (what the code does today) and `ARCHITECTURE.md` (historical, marked stale). When the three disagree, this document is the source of truth for **research state**; README is the source of truth for **code shape**; ARCHITECTURE is no longer authoritative for anything.

---

## 11. Long-term radar

*Last meaningful update: 2026-04-25 (slice N4 — GARCH timing revised in 11.1).*

*Captures legitimate technique/tool ideas surfaced during ideas-list reviews that are NOT immediate roadmap items but are worth tracking for future consideration. Section 11 grows as new ideas surface; the discipline for moving items in / out is in 11.6.*

### 11.1 Contingent technical upgrades

Items that may become relevant depending on what existing strategy evaluations reveal.

- **GARCH volatility modeling for the MC brain** — currently the MC brain's volatility estimator uses simple realized volatility from recent price history (EWMA optional, plain σ as the default). GARCH would model volatility as time-varying with autocorrelation. Becomes relevant **only if** accumulated MC settlements show GBM-with-realized-vol pricing is meaningfully off. If GBM works, GARCH adds complexity without benefit. **Decision gate (revised in slice N4):** evaluate when daily-cadence settlement sample reaches **n≈30 (estimated mid-May 2026)** — earlier than the original "after April 30" framing because B1 unblocked daily settlement collection on 2026-04-25, and KXBTCD settles ~daily. The April 30 monthly settlements remain a complementary data point but daily-cohort calibration data accumulates faster. See Section 6, "Interim — daily KXBTCD settlement accumulation."

  **Refinement from slice E1 (2026-04-25):** the E1 GBM-derivation notebook empirically refuted the textbook intuition that "fat tails raise touch probability" for this bot's typical contract structure. At near-money barriers (the regime where Kalshi daily KXBTCD contracts cluster, ~3-5% out-of-the-money), variance-preserving Student-t innovations actually **lower** touch probability vs Gaussian by 2-5 pp. Mechanism: standardizing fat-tailed distributions to unit variance moves probability mass from the body to *both* peaks and tails simultaneously; the body shortage hurts near-money touch accumulation more than the fatter tails help. The same effect persists (smaller, ~1 pp) at +14% OTM. So if calibration data eventually shows the bot systematically over-pricing touches **after accounting for the structural discrete-monitoring bias** (Section 6's calibration interpretation note), heavy-tailed residual models are NOT the right upgrade — the dominant real failure modes are vol non-stationarity (a wrong σ, particularly during volatility-clustering events) and discrete-jump events (Merton-style jump-diffusion). The contingent upgrade should therefore be **GARCH for time-varying σ and/or a jump-diffusion term**, NOT a heavy-tailed-residual model like GBM-with-Student-t.

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

---

## 14. Document update protocol

*Added in slice N5 (2026-04-25). Sections 12 and 13 are intentionally unused — Section 14 is the canonical home for document-update conventions, and the gap leaves room for future structural sections without renumbering this one.*

The conventions below are mostly already practiced across earlier sections. Section 14 makes them explicit so future-me doesn't have to reverse-engineer them from the document's history.

### 14a. Findings (Section 3) are append-only

Section 3 subsections (3a, 3b, ..., 3k, ...) are **never deleted**, even when superseded. The historical record is the point — future-me needs to see how thinking evolved, what was believed and later overturned, and on what evidence.

If a finding is overturned by better data:

- **Add a successor subsection** (e.g., 3m) that states the new finding with its evidence base.
- **Add a `[SUPERSEDED by 3m]` tag inline at the very start** of the original subsection, so a reader scanning Section 3 sees immediately that the original is no longer current.
- **Do not modify the original subsection's body.** It stays as a snapshot of what was true (or believed true) at its writing date.

### 14b. Snapshot sections preserve historical state inline (Section 5 convention)

Sections that capture point-in-time state — currently just Section 5 (Open Positions Snapshot) — get refreshed in place but **always preserve prior snapshots inline** rather than overwriting them. Concretely:

- A new "Current snapshot" subsection holds the latest state.
- The previously-current snapshot is renamed to "Prior snapshot — [slice or date]" and kept underneath, in chronological order.
- Section 5 already practices this (the pre-B1 snapshot from 2026-04-25 ~14:00 UTC is preserved underneath the post-B1 snapshot). The inline preamble of Section 5 documents the convention as well; this entry consolidates it as a project-wide rule.

The reason: position state changes faster than research conclusions, but the history of what positions were open at what time is exactly what you want when reconstructing whether a strategy was correctly cap-bound or whether a settlement event hit a position the bot held. Lossy overwrites destroy that.

### 14c. Roadmap items (Section 8) move from open list to Completed table

Section 8 has two structural lists: a **prioritized open list** (with subsections "Probably worth doing", "Possibly worth doing", "Operational improvements", "Probably never worth doing") and a **"Completed since last update" table** at the bottom.

When a roadmap item ships:

- Remove its bullet from the open list.
- Add a row to the Completed table with: slice ID (e.g., `**P1**`), commit hash, one-line description with concrete impact, completion date.
- Order the table chronologically by commit, oldest first.
- The table is cumulative since the last meaningful Section 8 refresh — N-slices that update Section 8 reset the table window implicitly. This isn't strict; if the table grows long, future refactors can prune it (but only into the prior section history of past N-slice update commits, never silently).

### 14d. Slice prefix conventions (S/T/C/P/N/A/B/E) live in Section 10

The full slice prefix taxonomy and what each letter means lives in **Section 10 ("How to Use This Document"), under "Style conventions"**. New prefixes are introduced when a class of work doesn't fit the existing letters; the introducing commit also updates Section 10 to add the new entry. Section 14 itself does NOT duplicate the taxonomy table — that would create two sources of truth that could drift. Section 14 is the *meta* layer; Section 10 is the *list*.

When introducing a new prefix in a slice:

- Add a one-line entry to Section 10's taxonomy bullet list, with the introducing slice noted (e.g., "*introduced with slice E1*").
- Add the new prefix to the change log entry's body so the reader of Section 4 sees that the prefix was established here.
- Keep the alphabet small. Prefixes earn their place by representing a category of work that recurs.

### 14e. The "Last meaningful update" header references the most recent N-slice

The italicized line at the very top of the document — `*Last meaningful update: ...*` — is updated **every time any N-slice lands**. The format is `(slice N{n} — one-line summary of what changed)`. This is the at-a-glance signal to a reader that this document is current as of a specific known point in the project's history.

Non-N slices (S, T, C, P, A, B, E) update this header **only if** they explicitly modify RESEARCH_NOTES.md as part of their commit (rare — the convention is that code-changing slices add their entry to Section 4 in the next N-slice, not in their own commit). E.g., slice B1's commit modified `backend/core/settlement.py` and `tests/test_settlement.py` only; slice N4 was the commit that brought RESEARCH_NOTES into sync with B1.

### 14f. Cross-document references in Section 1

When a slice creates a top-level companion artifact (a new `.md` file at the repo root that this document should reference), Section 1's opening paragraph — which lists companion documents — gets an additional reference. Examples:

- `STRATEGY3_SCOPE.md` was added by slice N3 → Section 1 now lists it as a companion.
- `AUDIT_2026-04-25.md` was created by the comprehensive audit but is a working document that may be deleted later — Section 1 does NOT list it (working artifacts are not stable enough to commit to a cross-reference).
- `notebooks/gbm_derivation.ipynb` was added by slice E1 → it's referenced in Section 3k and Section 11.1 by file path, but it lives under `notebooks/` (not at repo root) and is one of an expected family of learning artifacts, so the cross-reference goes via `notebooks/README.md` which itself indexes the family. Section 1 does not need updating for individual notebooks; it would need updating if `notebooks/README.md` itself became a stable companion document.

The general principle: Section 1's list of companions is short and load-bearing. Add to it when a new artifact is genuinely a stable, document-level peer. Don't add to it for working files, generated outputs, or members of an indexed family.

### 14g. When in doubt: append, don't rewrite

The unifying rule across 14a–14f: **the document's history is part of its value**. When a section needs new information, the default is to add — a new subsection, a new bullet, a new row, a `[SUPERSEDED]` tag — rather than to silently replace existing content. The ledger of how thinking evolved is more valuable than the cleanliness of any single moment's snapshot. A reader six months from now needs to be able to reconstruct *why* the project ended up where it did, not just *what* the current state is.
