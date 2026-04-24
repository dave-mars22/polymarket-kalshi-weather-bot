"""Tests for mc_execution guards (concentration cap + quote refresh)."""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

import httpx

from backend.config import settings
from backend.core.mc_execution import (
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

    def test_at_cap_returns_true(self):
        # Cap is 2 per config default
        db = self._mock_db_with_count(settings.MC_MAX_OPEN_PER_SERIES)
        self.assertTrue(concentration_cap_exceeded(db, "KXBTCMAXMON-X-1"))

    def test_above_cap_returns_true(self):
        db = self._mock_db_with_count(settings.MC_MAX_OPEN_PER_SERIES + 5)
        self.assertTrue(concentration_cap_exceeded(db, "KXBTCMAXMON-X-1"))


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
