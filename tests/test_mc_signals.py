"""Tests for backend.core.mc_signals with all I/O (HTTP + DB) mocked."""
from __future__ import annotations

import unittest
from contextlib import ExitStack
from datetime import date, datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import numpy as np

from backend.core.mc_signals import (
    MonteCarloSignal,
    _AllocationState,
    scan_for_mc_signals,
)
from backend.core.monte_carlo import SimulationResult
from backend.data.mc_markets import MonteCarloMarket


def _make_market(
    ticker="KXBTCD-X-T90000",
    direction="above",
    threshold=90_000.0,
    close_time=None,
    yes_ask=0.20,
    yes_bid=0.19,
    no_ask=0.80,
    no_bid=0.79,
    underlying="BTC",
    asset_class="crypto",
) -> MonteCarloMarket:
    if close_time is None:
        close_time = datetime.now(timezone.utc) + timedelta(days=3)
    return MonteCarloMarket(
        ticker=ticker,
        event_ticker=ticker.rsplit("-", 1)[0],
        venue="kalshi",
        underlying_asset=underlying,
        asset_class=asset_class,
        direction=direction,
        threshold=threshold,
        close_time=close_time,
        yes_ask=yes_ask,
        yes_bid=yes_bid,
        no_ask=no_ask,
        no_bid=no_bid,
        raw_market={},
    )


def _sim_with_p_above(p_above: float, threshold: float = 90_000.0, n: int = 10_000) -> SimulationResult:
    """Mock SimulationResult where prob_above(threshold) = p_above."""
    above_count = int(round(p_above * n))
    below_count = n - above_count
    # Values well above and below threshold so prob_above is exactly p_above.
    terminal = np.concatenate([
        np.full(above_count, threshold + 10_000.0),
        np.full(below_count, threshold - 10_000.0),
    ])
    return SimulationResult(
        spot=80_000.0, drift=0.0, vol=0.6, years_to_expiry=0.01,
        n_paths=n, terminal_prices=terminal,
    )


class MCSignalsTestCase(unittest.TestCase):
    """Stacks the common patches used by every test."""

    def _patches(
        self,
        markets,
        sim,
        history_days=70,
        spot_price=80_000.0,
        vol=0.60, drift=0.05,
        bankroll=10_000.0,
        alloc_total=0.0,
        alloc_by_underlying=None,
        alloc_by_asset_class=None,
        calib=1.0,
    ):
        stack = ExitStack()

        stack.enter_context(patch(
            "backend.core.mc_signals.fetch_mc_markets",
            return_value=markets,
        ))
        stack.enter_context(patch(
            "backend.core.mc_signals.fetch_spot",
            return_value=spot_price,
        ))
        # Synthetic history — just needs to be long enough to pass length gate
        today = date.today()
        bars = [
            (date.fromordinal(today.toordinal() - (history_days - i - 1)),
             spot_price * (1 + 0.0001 * i))
            for i in range(history_days)
        ]
        stack.enter_context(patch(
            "backend.core.mc_signals.fetch_daily_closes",
            return_value=bars,
        ))

        est = MagicMock()
        est.mu_annual = drift
        est.sigma_annual = vol
        est.n_returns = history_days - 1
        est.periods_per_year = 365.0
        stack.enter_context(patch(
            "backend.core.mc_signals.estimate_vol_drift",
            return_value=est,
        ))

        sim_mock = MagicMock(return_value=sim)
        stack.enter_context(patch(
            "backend.core.mc_signals.simulate_terminal_prices",
            sim_mock,
        ))

        alloc = _AllocationState(
            bankroll=bankroll,
            total_mc=alloc_total,
            by_underlying=dict(alloc_by_underlying or {}),
            by_asset_class=dict(alloc_by_asset_class or {}),
        )
        stack.enter_context(patch(
            "backend.core.mc_signals._get_allocation_state",
            return_value=alloc,
        ))
        stack.enter_context(patch(
            "backend.core.mc_signals.get_calibration_multiplier",
            return_value=calib,
        ))
        return stack, sim_mock


class TestHappyPath(MCSignalsTestCase):
    def test_yes_signal_when_model_above_market(self):
        """Model P(above 90k) = 0.40; yes_ask 0.20 => YES has +0.20 edge."""
        m = _make_market(yes_ask=0.20, no_ask=0.80, threshold=90_000.0)
        sim = _sim_with_p_above(0.40, threshold=90_000.0)

        with self._patches(markets=[m], sim=sim)[0]:
            signals = scan_for_mc_signals()

        self.assertEqual(len(signals), 1)
        s = signals[0]
        self.assertEqual(s.direction, "YES")
        self.assertAlmostEqual(s.model_probability, 0.40, places=4)
        self.assertAlmostEqual(s.market_probability, 0.20)
        self.assertGreater(s.raw_edge, 0.15)
        # Net edge at a $0.20 entry is raw minus ~10.5% fee drag (Kalshi
        # per-contract fee + slippage divided by notional). 5-8% is the
        # realistic window; still well above the 5% threshold.
        self.assertGreater(s.net_edge, 0.05)
        self.assertTrue(s.passes_threshold)
        self.assertGreater(s.suggested_size, 0)


class TestBothSidesConsidered(MCSignalsTestCase):
    def test_no_signal_when_model_says_below(self):
        """Model P(above 90k) = 0.15 => NO model_p = 0.85 vs no_ask 0.30 wins."""
        m = _make_market(yes_ask=0.20, no_ask=0.30, threshold=90_000.0)
        sim = _sim_with_p_above(0.15, threshold=90_000.0)

        with self._patches(markets=[m], sim=sim)[0]:
            signals = scan_for_mc_signals()

        self.assertEqual(len(signals), 1)
        s = signals[0]
        self.assertEqual(s.direction, "NO")
        self.assertAlmostEqual(s.model_probability, 0.85, places=4)
        self.assertAlmostEqual(s.market_probability, 0.30)


class TestDriftZeroing(MCSignalsTestCase):
    def test_short_expiry_forces_drift_zero(self):
        close = datetime.now(timezone.utc) + timedelta(days=3)
        m = _make_market(close_time=close, yes_ask=0.20)
        sim = _sim_with_p_above(0.40, threshold=m.threshold)

        with self._patches(markets=[m], sim=sim, drift=0.25)[0]:
            signals = scan_for_mc_signals()

        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0].drift_used, 0.0)
        self.assertIn("drift=0 forced", signals[0].reasoning)

    def test_long_expiry_preserves_drift(self):
        close = datetime.now(timezone.utc) + timedelta(days=15)
        m = _make_market(close_time=close, yes_ask=0.20)
        sim = _sim_with_p_above(0.40, threshold=m.threshold)

        with self._patches(markets=[m], sim=sim, drift=0.25)[0]:
            signals = scan_for_mc_signals()

        self.assertEqual(len(signals), 1)
        self.assertAlmostEqual(signals[0].drift_used, 0.25)
        self.assertNotIn("drift=0 forced", signals[0].reasoning)


class TestGates(MCSignalsTestCase):
    def test_subthreshold_signal_still_returned(self):
        """Edge below MC_MIN_EDGE_THRESHOLD => returned but not actionable."""
        m = _make_market(yes_ask=0.38, no_ask=0.62, threshold=90_000.0)
        sim = _sim_with_p_above(0.40, threshold=90_000.0)

        with self._patches(markets=[m], sim=sim)[0]:
            signals = scan_for_mc_signals()

        self.assertEqual(len(signals), 1)
        s = signals[0]
        self.assertFalse(s.passes_threshold)
        self.assertIn("SUB-THRESHOLD", s.reasoning)

    def test_entry_price_too_high_skipped(self):
        """Both asks above MC_MAX_ENTRY_PRICE => no signal."""
        m = _make_market(yes_ask=0.80, no_ask=0.80, threshold=90_000.0)
        sim = _sim_with_p_above(0.40, threshold=90_000.0)

        with self._patches(markets=[m], sim=sim)[0]:
            signals = scan_for_mc_signals()

        self.assertEqual(signals, [])

    def test_insufficient_history_skips_group(self):
        from backend.data.price_history import InsufficientHistoryError
        m = _make_market(yes_ask=0.20)
        sim = _sim_with_p_above(0.40, threshold=m.threshold)

        stack, _ = self._patches(markets=[m], sim=sim)
        with stack:
            with patch(
                "backend.core.mc_signals.fetch_daily_closes",
                side_effect=InsufficientHistoryError("only 3 bars"),
            ):
                signals = scan_for_mc_signals()

        self.assertEqual(signals, [])


class TestBatching(MCSignalsTestCase):
    def test_five_markets_same_group_run_one_simulation(self):
        """5 markets sharing (underlying, close_time) => one simulate call."""
        close = datetime.now(timezone.utc) + timedelta(days=10)
        markets = [
            _make_market(ticker=f"KXBTCD-T{t}", threshold=t, close_time=close,
                         yes_ask=0.20, no_ask=0.80)
            for t in (85_000.0, 90_000.0, 95_000.0, 100_000.0, 105_000.0)
        ]
        # Each threshold yields a different p_above, but they share the sim's
        # terminal_prices array. Pick a wide distribution so prob_above varies.
        rng = np.random.default_rng(0)
        terminal = rng.lognormal(mean=np.log(80_000), sigma=0.3, size=10_000)
        sim = SimulationResult(
            spot=80_000.0, drift=0.0, vol=0.3,
            years_to_expiry=10 / 365, n_paths=10_000, terminal_prices=terminal,
        )

        stack, sim_mock = self._patches(markets=markets, sim=sim)
        with stack:
            signals = scan_for_mc_signals()

        self.assertEqual(
            sim_mock.call_count, 1,
            "simulate_terminal_prices must be called exactly once for "
            "5 markets sharing (underlying, close_time)",
        )
        self.assertEqual(len(signals), 5)


class TestAllocationCaps(MCSignalsTestCase):
    def test_bankroll_200_enforces_per_trade_cap_of_6(self):
        """At $200 bankroll with MC_MAX_TRADE_SIZE_PCT=0.03, per-trade cap is $6."""
        m = _make_market(yes_ask=0.20, no_ask=0.80, threshold=90_000.0)
        sim = _sim_with_p_above(0.40, threshold=90_000.0)

        with self._patches(markets=[m], sim=sim, bankroll=200.0)[0]:
            signals = scan_for_mc_signals()

        self.assertEqual(len(signals), 1)
        s = signals[0]
        self.assertGreater(s.suggested_size, 0)
        self.assertLessEqual(s.suggested_size, 6.0 + 1e-9)

    def test_per_underlying_headroom_reduces_size(self):
        """BTC with $15 outstanding (cap $16 @ $200 bankroll) => size <= $1."""
        m = _make_market(yes_ask=0.20, no_ask=0.80, underlying="BTC")
        sim = _sim_with_p_above(0.40, threshold=m.threshold)

        with self._patches(
            markets=[m], sim=sim, bankroll=200.0,
            alloc_by_underlying={"BTC": 15.0},
            alloc_total=15.0,
            alloc_by_asset_class={"crypto": 15.0},
        )[0]:
            signals = scan_for_mc_signals()

        self.assertLessEqual(signals[0].suggested_size, 1.0 + 1e-9)


class TestBetweenMarket(MCSignalsTestCase):
    """Between markets use prob_in_range(low, high) for the YES side."""

    def test_between_uses_prob_in_range(self):
        import numpy as np
        from backend.core.monte_carlo import SimulationResult

        close = datetime.now(timezone.utc) + timedelta(days=5)
        m = MonteCarloMarket(
            ticker="KXINX-BETWEEN",
            event_ticker="KXINX-BETWEEN",
            venue="kalshi",
            underlying_asset="SPX",
            asset_class="equity_index",
            direction="between",
            threshold=5800.0,
            threshold_upper=5900.0,
            close_time=close,
            yes_ask=0.30,
            yes_bid=0.28,
            no_ask=0.72,
            no_bid=0.70,
            raw_market={},
        )

        # Build a sim where prob_in_range(5800, 5900) = 0.50.
        # Half of paths at 5850 (inside), half at 5700 (below).
        n = 10_000
        terminal = np.concatenate([
            np.full(n // 2, 5850.0),
            np.full(n // 2, 5700.0),
        ])
        sim = SimulationResult(
            spot=5800.0, drift=0.0, vol=0.18, years_to_expiry=5 / 365,
            n_paths=n, terminal_prices=terminal,
        )

        with self._patches(markets=[m], sim=sim, spot_price=5800.0)[0]:
            signals = scan_for_mc_signals()

        self.assertEqual(len(signals), 1)
        s = signals[0]
        # model_p_yes = prob_in_range(5800, 5900) = 0.50; yes_ask = 0.30 => raw = +0.20.
        # Should pick YES side.
        self.assertEqual(s.direction, "YES")
        self.assertAlmostEqual(s.model_probability, 0.50, places=4)
        self.assertIn("between", s.reasoning)
        self.assertIn("[$5,800.00, $5,900.00]", s.reasoning)


class TestEquityIndexAssetClass(MCSignalsTestCase):
    """SPX/NDX go through the same signal generator path, just with a
    different underlying symbol + periods_per_year (252 trading days)."""

    def test_spx_signal_flows_through(self):
        close = datetime.now(timezone.utc) + timedelta(days=2)
        m = _make_market(
            ticker="KXINX-26APR25-T5900",
            threshold=5900.0, close_time=close,
            yes_ask=0.20, no_ask=0.80,
            underlying="SPX", asset_class="equity_index",
        )
        sim = _sim_with_p_above(0.40, threshold=5900.0)

        with self._patches(markets=[m], sim=sim, spot_price=5800.0)[0]:
            signals = scan_for_mc_signals()

        self.assertEqual(len(signals), 1)
        s = signals[0]
        self.assertEqual(s.market.underlying_asset, "SPX")
        self.assertEqual(s.market.asset_class, "equity_index")
        # Signal logic identical to crypto: picks the side with better edge
        self.assertEqual(s.direction, "YES")  # model_p=0.40 > ask=0.20

    def test_ndx_signal_flows_through(self):
        close = datetime.now(timezone.utc) + timedelta(days=2)
        m = _make_market(
            ticker="NASDAQ100-26APR25-T18000",
            threshold=18_000.0, close_time=close,
            yes_ask=0.55, no_ask=0.45,
            underlying="NDX", asset_class="equity_index",
        )
        sim = _sim_with_p_above(0.62, threshold=18_000.0)

        with self._patches(markets=[m], sim=sim, spot_price=17_900.0)[0]:
            signals = scan_for_mc_signals()

        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0].market.underlying_asset, "NDX")
        self.assertEqual(signals[0].direction, "YES")  # 0.62 > 0.55

    def test_unknown_underlying_raises_via_simulate_group(self):
        """Underlying without an entry in _UNDERLYING_TO_SYMBOL must skip group."""
        close = datetime.now(timezone.utc) + timedelta(days=2)
        m = _make_market(
            ticker="KXUNKNOWN-X", underlying="UNKNOWN", asset_class="equity_index",
            close_time=close,
        )
        sim = _sim_with_p_above(0.40, threshold=m.threshold)

        with self._patches(markets=[m], sim=sim)[0]:
            signals = scan_for_mc_signals()

        # Group is skipped (logged at info); signals list is empty.
        self.assertEqual(signals, [])


if __name__ == "__main__":
    unittest.main()
