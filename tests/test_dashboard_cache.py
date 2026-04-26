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


if __name__ == "__main__":
    unittest.main()
