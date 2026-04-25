# STRATEGY3_SCOPE.md — Cross-Platform Arbitrage on BTC Daily Barrier Markets

*Single source of truth for strategy 3's design decisions, scope boundaries, and evaluation criteria. Companion to RESEARCH_NOTES.md (project-level) and AUDIT_2026-04-25.md (engineering audit). Updated as the strategy is built and evaluated; update protocol in Section 14.*

---

## 1. Status

| | |
|---|---|
| **Status** | **SCOPED, NOT YET BUILT** |
| Date scoped | 2026-04-25 |
| Target first observable spread data | ~2-3 weekends from scoping |
| Target first simulated arb trades | ~3-4 weekends from scoping |
| Target 30-day evaluation completion | mid-to-late June 2026 |

---

## 2. Hypothesis

> **"BTC daily barrier markets sometimes price differently between Polymarket and Kalshi by enough to overcome round-trip fees, creating tradeable arbitrage opportunities at retail-accessible scale."**

This hypothesis assumes:

- That retail-accessible BTC barrier markets exist on both platforms with **mechanically equivalent resolution criteria**.
- That price discrepancies between platforms occur with **non-trivial frequency** (not once per quarter; ideally multiple per day).
- That such discrepancies are **large enough to clear ~200 bps round-trip fees with margin**.
- That a polling-based bot (60-second scheduler intervals) is **fast enough** to observe and capture some fraction of these opportunities before they close.
- That **simulated execution at displayed prices is informative** about whether opportunities exist. It does NOT test whether real execution would capture them — that's a separate experiment, intentionally deferred.

---

## 3. Why this strategy now

Three substantive reasons:

1. **Directionally market-neutral.** The two existing strategies (BTC technical brain, MC barrier brain) are both unidirectional bets on price movement. Cross-platform arb locks in PnL at trade time regardless of underlying movement. Adding it diversifies strategy *type*, not just underlyings.

2. **Tightest possible scope for an MVP.** No prediction model, no new data sources beyond what's already integrated. The work is plumbing (matching, spread detection, paired execution), not research. Build effort dominated by integration glue, not new science.

3. **Validates Kalshi execution infrastructure.** The existing Kalshi integration is read-only (market discovery for the MC brain). Strategy 3 forces wiring up real Kalshi order placement code, even in simulation. That infrastructure becomes prerequisite for any future real-money path.

---

## 4. Out of scope (explicitly)

Items that may seem in-scope but are NOT for the MVP:

- **Other event types** — sports, politics, weather, macro events (Fed, CPI, NFP). Each has different matching complexity. Add only after BTC arb works for 30 days.
- **Other underlyings** — ETH, SOL, XRP barriers. Same logic; add after the BTC pair validates.
- **Real-money execution** — strategy 3 runs in simulation mode like everything else. The real-money path requires the existing strategies to validate first.
- **ML, statistical learning, neural networks** — covered in RESEARCH_NOTES Section 7 and 11.5; sample size insufficient and the strategy doesn't need prediction.
- **LLMs in the trading loop** — covered in RESEARCH_NOTES Section 7; antipattern.
- **Frontend integration** — initial implementation logs arb activity but doesn't expose new dashboard widgets. Cosmetic work deferred until strategy validates.
- **Polymarket execution code paths** — not strictly "out of scope" since the existing BTC technical brain already trades Polymarket. We **reuse** that infrastructure rather than building parallel execution paths.

---

## 5. Spread thresholds (two-tier)

Two thresholds, used for different purposes.

### Observable threshold: ≈200 bps (1× estimated round-trip fees)

- A spread above this counts as **"an opportunity exists"**.
- Used for evaluating the **opportunity space** — does the hypothesis even have data behind it?
- All observable spreads are logged; not all are traded.
- **Note:** 200 bps is approximate. Actual round-trip fees vary by platform and trade size; revisit calibration after 30 days of empirical fee data.

### Tradeable threshold: ≈400 bps (2× estimated round-trip fees)

- A spread above this counts as **"worth capturing"**.
- Leaves margin for slippage, partial fills, and second-leg-execution risk.
- Used for the kill criterion (Section 6) and for actual simulated trade decisions.

### Threshold recalibration after 30 days

After the evaluation window, recalibrate based on observed spread distribution:

- If **400 bps proves too aggressive** (no spreads ever clear it but plenty cluster around 250–350 bps): lower it.
- If **400 bps is consistently exceeded but realized PnL is negative**: raise it.

The 200/400 split is a starting point, not a permanent calibration.

---

## 6. Evaluation criteria (kill / soft-kill / continue)

### Hard kill

Strategy is dead, archive and move on. **ALL THREE must be true at day 30:**

- Fewer than **5 tradeable spreads per week** (averaged over 30 days)
- AND fewer than **50 observable spreads detected total** over 30 days
- AND **realized simulated edge ≤ 0**

If all three hold, the hypothesis is falsified at retail scale on BTC daily barriers. Document the negative result in RESEARCH_NOTES, kill the strategy, do not extend.

### Soft kill

Strategy doesn't continue as-is. **ANY** of these:

- Lots of observable spreads (>50) but few tradeable (<5/week) — opportunity exists but is too thin to capture; revisit threshold or kill.
- Tradeable spreads exist but **simulated PnL ≤ 0 after fees** — execution model is wrong, or matching is producing false positives.
- **Post-settlement matching divergence rate > 5%** — matching layer is unreliable; strategy can't be evaluated until matching is fixed.
- **Latency-bound failures > 20%** of detected tradeable spreads close before bot acts — bot architecture insufficient; needs fundamental redesign.

### Continue and consider expanding

**ALL THREE must hold:**

- Tradeable spreads averaging ≥5/week with **sustained positive simulated PnL**
- **Matching reliability confirmed** (post-settlement divergence rate ≤5%)
- **No fundamental bottlenecks** (latency, liquidity at trade size) that block real execution

If continue: extend evaluation another 30 days OR (if data is unambiguous) consider expansion to ETH/SOL barriers using the same matching framework.

---

## 7. Simulation approach (Option α)

The MVP simulates as if **both legs fill perfectly at displayed prices**. This is intentional and has known limitations.

| | |
|---|---|
| **What this approach tests** | whether opportunities EXIST in the data |
| **What this approach does NOT test** | whether real execution would CAPTURE them |

### Known simulation-to-reality gaps

- Real fills may slip vs. displayed prices, especially at trade size.
- Second-leg execution may fail entirely, leaving an unhedged position.
- Order book depth may be insufficient at our trade sizes.
- Latency may cause the displayed spread to close before second leg fills.

These gaps are all in the direction of **overstating real-world edge**. If simulated PnL is ≤ 0, real PnL is almost certainly worse — that's a clear kill. If simulated PnL is positive, real PnL is unknown — would require a separate real-money validation experiment.

### Why we're not modeling slippage / fail rates explicitly

Doing so requires inventing parameters not grounded in real data. The honest move is to **keep simulation simple and document the gap**, rather than fake sophistication that would create false confidence.

---

## 8. Matching defenses (full set)

The single highest-risk part of cross-platform arb is the **matching layer** — declaring two markets "the same event" when they're not. False positives in matching produce data that looks like edge but isn't, leading to bad conclusions and bad eventual real-money trades.

Four defenses, **all required for MVP**.

### Defense 1: Structured parsers

Both Polymarket and Kalshi market formats are parsed into structured records with these explicit fields:

- `underlying` (e.g., `"BTC"`)
- `resolution_type` (e.g., `"above_threshold_at_close"`, `"barrier_touch"`, `"barrier_no_touch"`)
- `threshold_usd` (numeric, exact)
- `resolution_datetime_utc` (timestamp, to-the-minute precision)
- `data_source` (e.g., `"Coinbase BTC-USD index"`)
- `measurement_window` (e.g., `"snapshot"`, `"TWAP_5min"`)

If any field can't be confidently extracted from the raw market data, the parser **MUST return `None`** and the market is excluded. **No fuzzy matching.**

### Defense 2: Tier system

Three tiers of confidence:

| tier | meaning | trade behavior |
|---|---|---|
| **Tier 1 (auto-trade)** | Market type pairs that have been **manually hand-verified** for resolution-rule equivalence. Verification means: a written record (in this document or a linked file) stating "I verified that platform A and platform B both resolve based on data source X at time Y under rule Z," with cross-references to each platform's contract spec or documentation. | Eligible for actual simulated arb trades. |
| **Tier 2 (log only)** | Market type pairs that pass structured matching but **haven't been hand-verified** yet. | Bot detects spreads and logs them for review, but does NOT trade. Manual promotion to Tier 1 happens after verification. |
| **Tier 3 (no match)** | Anything that doesn't match structurally. | Excluded entirely. |

The MVP starts with **at most 1–2 Tier 1 market type pairs**. Expansion happens by manually verifying additional pairs over time.

### Defense 3: Runtime resolution-rule re-check

Before placing the second leg of any arb trade, the bot:

- Re-fetches the resolution rules from both platforms' APIs.
- Compares them to the rules captured at first-leg-execution time.
- If anything has changed (rare but possible), **aborts the trade** and logs the discrepancy.

This catches the rare case where a platform updates rules after the matching layer's last verification.

### Defense 4: Post-settlement validation

After both legs of an "arb pair" settle, the bot:

- **Verifies that the legs resolved consistently** (one wins, one loses, capturing the spread).
- If both legs resolve the same direction (both win or both lose), this is a **matching error** — the markets weren't actually the same event.
- Logs matching errors loudly with full details for diagnosis.
- Tracks the matching-error rate; if it **exceeds 5% over a meaningful sample**, halts new arb trades until matching is fixed.

This is the runtime safety net that catches matching errors that defenses 1–3 missed.

---

## 9. Initial Tier 1 market pair candidates

Specific market pairs to hand-verify first, in priority order.

### Pair 1 (priority): Kalshi `KXBTCD` daily contracts ↔ Polymarket BTC-price daily markets

- **Kalshi:** `KXBTCD-YYMMDDHH-T{threshold}` — daily BTC price barriers, settles at 17:00 ET.
- **Polymarket:** equivalent BTC price prediction markets (need to identify the exact slug pattern in step 2).
- **Why this pair first:** both platforms have high volume, the resolution mechanics appear straightforward, and your bot already pulls Coinbase BTC-USD spot prices that are likely the underlying for both.

### Pair 2 (deferred): Kalshi `KXBTCMAXMON` monthly ↔ Polymarket equivalent monthly BTC barriers

- **Kalshi:** `KXBTCMAXMON-BTC-YYMMM[end]-{threshold}`.
- **Polymarket:** equivalent monthly markets.
- **Why deferred:** monthly settles are slower and produce less data per unit time. Wait for daily contracts to validate first.

### Verification log

The verification work for Pair 1 must be done **manually** before any simulated trades happen. This document will be updated with the verification record once complete (entries below per the format in Section 14).

*(No verification records yet — to be filled in during Step 4 of the build sequence.)*

---

## 10. Build sequence

Each step produces evaluable output before the next is built. **Stop and evaluate at each gate.**

### Step 1 — Scoping document committed

- **Status:** this document, slice **N3**.

### Step 2 — Structured parsers (Defense 1)

Build:

- `backend/data/arb_market_parser.py` with `parse_polymarket_btc(market_data)` and `parse_kalshi_btc(market_data)` functions.
- Each returns a structured `ArbMarket` record (frozen dataclass) or `None`.
- Comprehensive tests for each parser covering known market formats, edge cases, and unparseable inputs.

| | |
|---|---|
| Output | read-only — given a list of market data, returns structured records |
| Slice prefix | **A1** (A for arbitrage) |

### Step 3 — Matching layer with tier system (Defense 2)

Build:

- `backend/core/arb_matching.py` with `match_markets(polymarket_markets, kalshi_markets) → List[MatchedPair]`.
- Tier classification logic: matched pairs flagged as **Tier 1 only if** the market type combination is in a manually-curated whitelist (initially empty until Step 4 completes).
- Tests for matching equivalence checks, tier classification, and edge cases.

| | |
|---|---|
| Output | read-only — produces matched pair candidates with tier labels |
| Slice prefix | **A2** |

### Step 4 — Manual Tier 1 verification (you, not Claude Code)

For Pair 1 (KXBTCD ↔ Polymarket BTC daily):

- Read the Kalshi `KXBTCD` contract specification.
- Read Polymarket's equivalent BTC market documentation.
- Verify resolution timing, data source, threshold semantics, and edge cases.
- **Document the verification in this file** (add to the "Verification log" subsection of Section 9).
- Update the Tier 1 whitelist in `arb_matching.py`.

This step is gated on Claude Code's output from Step 3. **Do not skip.**

### Step 5 — Spread detection layer

Build:

- `backend/core/arb_spread_detector.py` — for each Tier 1 pair, computes current spread between Polymarket and Kalshi prices, accounting for both platforms' fees.
- New scheduler job `arb_scan_and_trade_job` at appropriate interval (suggest 60–120 seconds; tune based on initial observations).
- All **observable spreads (≥200 bps)** logged to a new database table or as `Trade` rows with `market_type='arb_observed'`.
- **Tradeable spreads (≥400 bps)** flagged for execution.
- Tests for spread computation accuracy, threshold logic, and edge cases.

| | |
|---|---|
| Output | still read-only at this stage — measures and logs spreads, doesn't trade yet |
| Slice prefix | **A3** |

### Step 6 — Execution layer with runtime check (Defense 3)

Build:

- Paired-trade execution logic that:
  - Re-fetches resolution rules immediately before second leg (Defense 3).
  - Places both legs in the simulated trade table with linked IDs.
  - Tracks the pair through to settlement.
- New `market_type='arb'` for paired trades; a new database column or convention to link the two legs.
- Tests for paired execution logic, resolution-rule re-check abort path, and the simulated fill behavior.

| | |
|---|---|
| Output | simulates real arb trades, generates evaluable PnL data |
| Slice prefix | **A4** |

### Step 7 — Post-settlement validation (Defense 4)

Build:

- Settlement code extension that, when both legs of an arb pair resolve, **verifies consistency**.
- Matching-error rate tracking.
- **Halt-on-high-error-rate logic** (refuses new arb trades if matching-error rate over recent sample > 5%).
- Tests for divergent resolution detection and the halt logic.

| | |
|---|---|
| Output | full closed-loop arb strategy with self-monitoring |
| Slice prefix | **A5** |

### Step 8 — 30-day evaluation begins

**Status:** only after A5 lands. Day 1 = first day with all components live.

---

## 11. Estimated build effort

Per-step rough estimates (stated bandwidth: 30+ hours/week):

| step | slice | estimated effort |
|---|---|---|
| Step 2 (parsers) | A1 | 1 weekend (~10–15 hours) |
| Step 3 (matching) | A2 | 1 weekend (~10–15 hours) |
| Step 4 (manual verification) | (you) | 2–4 hours of careful reading and documentation |
| Step 5 (spread detection) | A3 | 1 weekend (~10–15 hours) |
| Step 6 (execution) | A4 | 1 weekend (~10–15 hours) |
| Step 7 (validation) | A5 | half-weekend (~5–10 hours) |

**Total to start of evaluation: 3–5 weekends**, depending on pace and how complex the platform-specific market formats turn out to be.

---

## 12. Connection to existing strategies and roadmap

### Reuses from existing infrastructure

- Coinbase BTC-USD price feeds (already integrated).
- Kalshi market discovery code (already integrated for MC brain).
- Polymarket market discovery code (already integrated for BTC technical brain).
- `Trade` table schema (extending with new `market_type` values).
- Scheduler / settlement infrastructure (extending with new jobs).
- Fee model (already in `fees.py`; extends to support both platforms).

### Updates required to RESEARCH_NOTES.md when this lands

- Add to Section 2 (current strategy state).
- Add A1–A5 entries to Section 4 (parameter change log) as they land.
- Add success criteria checkpoint at day-30 mark to Section 6.
- Update Section 11.4 (pairs trading frame) to reference this document.

### Blocks / blocked-by

- **Not blocked by existing strategies** — runs independently.
- **Does NOT block backtester work** — they can proceed in parallel.
- **Does block any future "real money" decision** until validated for 30+ days.

---

## 13. Open questions to resolve during build

These are deliberately left unresolved at scoping time; each gets resolved at the relevant build step.

- **Exact Polymarket market slug pattern for BTC daily price markets** — needs investigation in Step 2 / Step 4.
- **Whether Kalshi's `KXBTCD` resolution data source is publicly documented** or needs reverse-engineering.
- **Database schema decision:** extend `Trade` table with `arb_pair_id` column, or create a separate `ArbTrade` table? Resolve in Step 6.
- **Trade size for arb:** match existing `MAX_TRADE_SIZE` ($10), or be dynamic based on order book depth at trade time? Resolve in Step 6.
- **Scheduler interval for arb scanning:** 60s, 120s, or other? Tune empirically in Step 5.

---

## 14. Update protocol

This document evolves as the strategy is built and evaluated:

- **Verification records (Step 4)** get added to Section 9's "Verification log" subsection.
- **Build progress** (which slices have landed) gets reflected in Section 10 with completion dates and commit hashes.
- **Open questions (Section 13)** get resolved and documented.
- **After day 30**, evaluation results get added as a new Section 15.

Documentation-only updates use slice prefix **N** (e.g., **N3** for the slice that creates this document, N4+ for future updates). Code changes use slice prefix **A**.

This document is the source of truth for **strategy 3 design and scope**; RESEARCH_NOTES is the source of truth for **overall project research state**; README is the source of truth for **code shape**. When they disagree, this doc is authoritative for strategy 3 specifically.
