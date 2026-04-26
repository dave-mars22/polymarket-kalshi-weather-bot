"""Unit tests for backend.core.dashboard_cache (slice P3).

Covers cache state machine (round-trip, miss, update-twice), the builder
helper that runs the underlying queries, and the convenience refresh
function called by the scheduler. The integration test that pins the
"dashboard does NOT run the slow queries when cache is hot" contract
lives in tests/test_api_expansion.py:TestDashboardServesEquityCalibrationCache
to mirror P2's existing scan-cache integration test class.

Mocking strategy:
- Cache state machine tests: pure in-memory state, no fixtures.
- Builder tests: in-memory SQLite via the helpers from test_settlement.py
  (_make_in_memory_db, _make_pending_trade) so the builder runs against
  a real schema without touching tradingbot.db.
"""
from __future__ import annotations

import unittest
from datetime import datetime

from backend.core import dashboard_cache as dc
from backend.core.dashboard_cache import (
    CalibrationSummary,
    build_dashboard_cache_payload,
    get_cached_dashboard_data,
    refresh_dashboard_cache,
    update_cached_dashboard_data,
)
from backend.models.database import Signal

# Reuse the in-memory SQLite fixtures established in T2.
from tests.test_settlement import (
    _make_bot_state,
    _make_in_memory_db,
    _make_pending_trade,
)


def _reset_cache():
    """Clear module-level cache state. Call in setUp/tearDown so tests
    never leak state into each other."""
    dc._dashboard_cache = None


class TestDashboardCacheState(unittest.TestCase):
    """Cache state machine: get/update/round-trip semantics. No DB."""

    def setUp(self):
        _reset_cache()

    def tearDown(self):
        _reset_cache()

    def test_initial_state_is_miss(self):
        # Fresh module load -> get returns (None, None).
        payload, ts = get_cached_dashboard_data()
        self.assertIsNone(payload)
        self.assertIsNone(ts)

    def test_round_trip_returns_same_payload(self):
        sentinel = {"equity_curve": [{"x": 1}], "calibration": None}
        update_cached_dashboard_data(sentinel)
        payload, ts = get_cached_dashboard_data()
        self.assertIs(payload, sentinel)  # same object — atomic store
        self.assertIsNotNone(ts)
        self.assertIsInstance(ts, datetime)

    def test_update_twice_latest_wins(self):
        update_cached_dashboard_data({"equity_curve": ["v1"], "calibration": None})
        update_cached_dashboard_data({"equity_curve": ["v2"], "calibration": None})
        payload, _ = get_cached_dashboard_data()
        self.assertEqual(payload["equity_curve"], ["v2"])

    def test_get_does_not_clear_state(self):
        # get is idempotent — repeated reads see the same value until
        # update is called. Pin this so a future refactor doesn't
        # accidentally turn get into a pop.
        update_cached_dashboard_data({"equity_curve": ["x"], "calibration": None})
        first_payload, first_ts = get_cached_dashboard_data()
        second_payload, second_ts = get_cached_dashboard_data()
        self.assertIs(first_payload, second_payload)
        self.assertEqual(first_ts, second_ts)


class TestBuildDashboardCachePayload(unittest.TestCase):
    """Builder runs the equity-curve query + calibration query against a
    real (in-memory) DB and returns a payload dict with the expected shape."""

    def setUp(self):
        _reset_cache()
        self.engine, self.db = _make_in_memory_db()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()
        _reset_cache()

    def test_empty_db_returns_empty_curve_and_no_calibration(self):
        payload = build_dashboard_cache_payload(self.db)
        self.assertEqual(payload["equity_curve"], [])
        self.assertIsNone(payload["calibration"])

    def test_single_settled_trade_appears_in_equity_curve(self):
        # Persist one trade and mark it settled with a known PnL.
        trade = _make_pending_trade(
            self.db, platform="polymarket", direction="yes",
            entry_price=0.40, size=10.0,
        )
        trade.settled = True
        trade.pnl = 6.0
        trade.result = "win"
        trade.settlement_value = 1.0
        self.db.commit()

        payload = build_dashboard_cache_payload(self.db)
        curve = payload["equity_curve"]
        self.assertEqual(len(curve), 1)
        # Cumulative PnL after one trade equals that trade's PnL.
        self.assertAlmostEqual(curve[0]["pnl"], 6.0)
        # Bankroll = INITIAL_BANKROLL + cumulative_pnl. INITIAL_BANKROLL is
        # whatever the active settings says — we just assert the relationship.
        from backend.config import settings
        self.assertAlmostEqual(
            curve[0]["bankroll"], settings.INITIAL_BANKROLL + 6.0,
        )

    def test_calibration_summary_built_from_settled_signals(self):
        # Persist one settled signal with known outcome, build the payload,
        # check the calibration summary reflects it.
        signal = Signal(
            market_ticker="poly-x", platform="polymarket", direction="up",
            model_probability=0.60, market_price=0.50, edge=0.10,
            outcome_correct=True, settlement_value=1.0,
            features={},
        )
        self.db.add(signal)
        self.db.commit()

        payload = build_dashboard_cache_payload(self.db)
        cal = payload["calibration"]
        self.assertIsInstance(cal, CalibrationSummary)
        self.assertEqual(cal.total_signals, 1)
        self.assertEqual(cal.total_with_outcome, 1)
        self.assertEqual(cal.accuracy, 1.0)
        # Brier = (model_prob - actual)^2 = (0.60 - 1.0)^2 = 0.16.
        self.assertAlmostEqual(cal.brier_score, 0.16, places=4)


class TestRefreshDashboardCache(unittest.TestCase):
    """The convenience function the scheduler calls. Builds a payload and
    atomically swaps it into the cache."""

    def setUp(self):
        _reset_cache()
        self.engine, self.db = _make_in_memory_db()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()
        _reset_cache()

    def test_refresh_populates_cache(self):
        # Cache empty pre-refresh; populated post-refresh; the timestamp
        # is set by the function.
        self.assertEqual(get_cached_dashboard_data(), (None, None))
        refresh_dashboard_cache(self.db)
        payload, ts = get_cached_dashboard_data()
        self.assertIsNotNone(payload)
        self.assertIsNotNone(ts)
        self.assertIn("equity_curve", payload)
        self.assertIn("calibration", payload)

    def test_refresh_overwrites_prior_cache(self):
        # Seed the cache with a sentinel, then call refresh — the sentinel
        # is replaced by a freshly-built payload.
        update_cached_dashboard_data(
            {"equity_curve": ["sentinel"], "calibration": None}
        )
        refresh_dashboard_cache(self.db)
        payload, _ = get_cached_dashboard_data()
        self.assertNotEqual(payload["equity_curve"], ["sentinel"])
        # On an empty DB the rebuilt curve is [].
        self.assertEqual(payload["equity_curve"], [])


# =============================================================================
# Slice P4: per-underlying micro cache (separate state from the P3 single
# (payload, ts) cache above). Tests cover the same shape: state machine,
# round-trip, miss returns None, whole-dict-swap atomicity smoke check.
# =============================================================================


from backend.core.dashboard_cache import (
    get_cached_micro,
    update_cached_micro,
)
from backend.data.crypto import CryptoMicrostructure


def _reset_micro_cache():
    """Clear the per-underlying micro cache between tests."""
    dc._micro_cache = {}


class TestPerUnderlyingMicroCache(unittest.TestCase):
    """State + accessors for the P4 per-underlying micro cache."""

    def setUp(self):
        _reset_micro_cache()

    def tearDown(self):
        _reset_micro_cache()

    def _sample_micro(self, rsi: float = 55.0, price: float = 77000.0) -> CryptoMicrostructure:
        return CryptoMicrostructure(
            rsi=rsi, momentum_1m=0.1, momentum_5m=0.2, momentum_15m=0.3,
            vwap_deviation=0.01, sma_crossover=0.02, volatility=0.5,
            price=price, source="coinbase",
        )

    def test_get_returns_none_on_miss(self):
        # Nothing in the cache yet — every underlying returns None.
        self.assertIsNone(get_cached_micro("BTC"))
        self.assertIsNone(get_cached_micro("ETH"))
        self.assertIsNone(get_cached_micro("XRP"))

    def test_round_trip_preserves_micro_and_timestamp(self):
        micro = self._sample_micro(rsi=42.0, price=80000.0)
        update_cached_micro("BTC", micro)
        cached = get_cached_micro("BTC")
        self.assertIsNotNone(cached)
        got_micro, got_ts = cached
        self.assertIs(got_micro, micro)
        self.assertIsInstance(got_ts, datetime)

    def test_underlying_lookup_is_case_insensitive(self):
        # Cache normalizes to upper. "btc"/"BTC"/"Btc" all map to the
        # same slot — pin this so a future caller using lowercase tickers
        # doesn't silently miss the cache.
        micro = self._sample_micro()
        update_cached_micro("btc", micro)
        self.assertIs(get_cached_micro("BTC")[0], micro)
        self.assertIs(get_cached_micro("Btc")[0], micro)

    def test_multiple_underlyings_isolated(self):
        # Updating BTC must not affect the other underlyings' cache slots.
        btc = self._sample_micro(price=77000.0)
        eth = self._sample_micro(price=3500.0)
        update_cached_micro("BTC", btc)
        update_cached_micro("ETH", eth)
        self.assertIs(get_cached_micro("BTC")[0], btc)
        self.assertIs(get_cached_micro("ETH")[0], eth)
        # SOL/XRP weren't updated — still None.
        self.assertIsNone(get_cached_micro("SOL"))
        self.assertIsNone(get_cached_micro("XRP"))

    def test_update_overwrites_prior_entry_for_same_underlying(self):
        m1 = self._sample_micro(rsi=30.0)
        m2 = self._sample_micro(rsi=70.0)
        update_cached_micro("BTC", m1)
        update_cached_micro("BTC", m2)
        self.assertEqual(get_cached_micro("BTC")[0].rsi, 70.0)

    def test_update_uses_whole_dict_swap_atomicity(self):
        # Smoke test the atomicity guarantee: rebinding the module-level
        # name to a NEW dict on every update means a reader holding a
        # snapshot of the old dict won't see the new entry. We can't
        # truly test concurrent observation in a single thread, but we
        # can pin that the dict identity changes on update.
        update_cached_micro("BTC", self._sample_micro())
        first_dict = dc._micro_cache
        update_cached_micro("ETH", self._sample_micro())
        second_dict = dc._micro_cache
        # Different dict objects (atomic rebind), not in-place mutation.
        self.assertIsNot(first_dict, second_dict)
        # Old dict snapshot still has only BTC; new dict has both.
        self.assertIn("BTC", first_dict)
        self.assertNotIn("ETH", first_dict)
        self.assertIn("BTC", second_dict)
        self.assertIn("ETH", second_dict)


if __name__ == "__main__":
    unittest.main()
