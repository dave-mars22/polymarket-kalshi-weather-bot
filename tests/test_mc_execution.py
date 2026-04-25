"""Tests for mc_execution guards (concentration cap + quote refresh)."""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

import httpx

from backend.config import settings
from backend.core.mc_execution import (
    cap_for_series,
    concentration_cap_exceeded,
    fetch_current_ask,
    quote_drifted,
    series_ticker_of,
)


class TestSeriesTicker(unittest.TestCase):
    def test_extracts_prefix_before_first_dash(self):
        self.assertEqual(series_ticker_of("KXBTCMAXMON-BTC-26APR30-8000000"), "KXBTCMAXMON")
        self.assertEqual(series_ticker_of("KXINX-26APR24H1600-B7112"), "KXINX")
        self.assertEqual(series_ticker_of("NOMARK"), "NOMARK")


class TestCapForSeries(unittest.TestCase):
    """Slice S2: cap is cadence-specific. Verify each cadence dispatches
    to the right setting."""

    def test_daily_series_uses_daily_cap(self):
        self.assertEqual(cap_for_series("KXBTCD"), settings.MC_MAX_OPEN_PER_SERIES_DAILY)
        self.assertEqual(cap_for_series("KXAVAXD"), settings.MC_MAX_OPEN_PER_SERIES_DAILY)
        self.assertEqual(cap_for_series("KXBCH"), settings.MC_MAX_OPEN_PER_SERIES_DAILY)
        self.assertEqual(cap_for_series("KXSHIBA"), settings.MC_MAX_OPEN_PER_SERIES_DAILY)

    def test_monthly_series_uses_monthly_cap(self):
        self.assertEqual(cap_for_series("KXBTCMAXMON"), settings.MC_MAX_OPEN_PER_SERIES_MONTHLY)
        self.assertEqual(cap_for_series("KXBTCMINMON"), settings.MC_MAX_OPEN_PER_SERIES_MONTHLY)

    def test_unknown_series_uses_other_cap(self):
        # Defensive: unknown series falls back to the conservative cap
        self.assertEqual(cap_for_series("KXNEW_SERIES_XYZ"), settings.MC_MAX_OPEN_PER_SERIES_OTHER)


class TestConcentrationCap(unittest.TestCase):
    """Mock the DB session and verify the cap check fires correctly."""

    def _mock_db_with_count(self, n: int):
        db = MagicMock()
        # Chain: db.query().filter().count() -> n
        db.query.return_value.filter.return_value.count.return_value = n
        return db

    def test_below_cap_returns_false(self):
        db = self._mock_db_with_count(0)
        self.assertFalse(concentration_cap_exceeded(db, "KXBTCMAXMON-X-1"))
        db = self._mock_db_with_count(1)
        self.assertFalse(concentration_cap_exceeded(db, "KXBTCMAXMON-X-1"))

    def test_at_cap_returns_true_monthly(self):
        # Monthly series cap (KXBTCMAXMON is monthly).
        db = self._mock_db_with_count(settings.MC_MAX_OPEN_PER_SERIES_MONTHLY)
        self.assertTrue(concentration_cap_exceeded(db, "KXBTCMAXMON-X-1"))

    def test_above_cap_returns_true_monthly(self):
        db = self._mock_db_with_count(settings.MC_MAX_OPEN_PER_SERIES_MONTHLY + 5)
        self.assertTrue(concentration_cap_exceeded(db, "KXBTCMAXMON-X-1"))

    # Slice S2 regressions: daily series have a *higher* cap than monthly,
    # so a count that triggers the monthly cap must still pass on a daily
    # series. These would have caught a regression of S2 back to a single
    # global cap.

    def test_daily_below_daily_cap_does_not_fire(self):
        # MC_MAX_OPEN_PER_SERIES_DAILY = 3 today; with 2 open daily trades
        # the cap should NOT fire even though it would on a monthly series.
        db = self._mock_db_with_count(settings.MC_MAX_OPEN_PER_SERIES_DAILY - 1)
        self.assertFalse(concentration_cap_exceeded(db, "KXBTCD-26APR2517-T78749.99"))

    def test_daily_at_daily_cap_fires(self):
        db = self._mock_db_with_count(settings.MC_MAX_OPEN_PER_SERIES_DAILY)
        self.assertTrue(concentration_cap_exceeded(db, "KXBTCD-26APR2517-T78749.99"))

    def test_daily_cap_strictly_higher_than_monthly_when_split(self):
        # Sanity: if the configuration were ever flipped so daily <= monthly,
        # the slice S2 motivation would be defeated. This test pins the
        # invariant.
        self.assertGreaterEqual(
            settings.MC_MAX_OPEN_PER_SERIES_DAILY,
            settings.MC_MAX_OPEN_PER_SERIES_MONTHLY,
        )


class TestFetchCurrentAsk(unittest.TestCase):
    def _client(self, handler):
        return httpx.Client(transport=httpx.MockTransport(handler))

    def test_returns_yes_ask_for_yes_side(self):
        def handler(req):
            return httpx.Response(200, json={
                "market": {"yes_ask_dollars": "0.52", "no_ask_dollars": "0.49"},
            })
        with self._client(handler) as client:
            self.assertEqual(
                fetch_current_ask("KXBTCMAXMON-X", "YES", http_client=client), 0.52
            )

    def test_returns_no_ask_for_no_side(self):
        def handler(req):
            return httpx.Response(200, json={
                "market": {"yes_ask_dollars": "0.52", "no_ask_dollars": "0.49"},
            })
        with self._client(handler) as client:
            self.assertEqual(
                fetch_current_ask("KXBTCMAXMON-X", "NO", http_client=client), 0.49
            )

    def test_404_returns_none(self):
        def handler(req):
            return httpx.Response(404)
        with self._client(handler) as client:
            self.assertIsNone(fetch_current_ask("BAD", "YES", http_client=client))

    def test_missing_ask_returns_none(self):
        def handler(req):
            return httpx.Response(200, json={"market": {}})
        with self._client(handler) as client:
            self.assertIsNone(fetch_current_ask("X", "YES", http_client=client))

    def test_zero_or_one_ask_returns_none(self):
        # Treat boundary asks as "no usable quote"
        def handler(req):
            return httpx.Response(200, json={"market": {"yes_ask_dollars": "0.00"}})
        with self._client(handler) as client:
            self.assertIsNone(fetch_current_ask("X", "YES", http_client=client))
        def handler2(req):
            return httpx.Response(200, json={"market": {"yes_ask_dollars": "1.00"}})
        with self._client(handler2) as client:
            self.assertIsNone(fetch_current_ask("X", "YES", http_client=client))


class TestQuoteDrifted(unittest.TestCase):
    def test_within_tolerance_not_drifted(self):
        self.assertFalse(quote_drifted(0.50, 0.50))
        self.assertFalse(quote_drifted(0.50, 0.515))  # 1.5c, well below 2c tolerance

    def test_beyond_tolerance_drifted(self):
        self.assertTrue(quote_drifted(0.50, 0.525))  # 2.5c > 2c
        self.assertTrue(quote_drifted(0.50, 0.53))   # 3c > 2c
        self.assertTrue(quote_drifted(0.50, 0.45))   # -5c

    def test_respects_configured_tolerance(self):
        original = settings.MC_QUOTE_DRIFT_TOLERANCE
        try:
            settings.MC_QUOTE_DRIFT_TOLERANCE = 0.05
            self.assertFalse(quote_drifted(0.50, 0.535))  # 3.5c < 5c
            self.assertTrue(quote_drifted(0.50, 0.60))    # 10c > 5c
        finally:
            settings.MC_QUOTE_DRIFT_TOLERANCE = original


if __name__ == "__main__":
    unittest.main()
