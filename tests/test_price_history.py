"""Tests for backend.data.price_history using httpx.MockTransport.

No real network calls. time.sleep is patched during retry tests so the
suite runs instantly.
"""
from __future__ import annotations

import unittest
from datetime import date, datetime, timezone
from unittest.mock import patch

import httpx

from backend.data import price_history
from backend.data.price_history import (
    InsufficientHistoryError,
    PriceHistoryError,
    fetch_daily_closes,
)


def _candle_row(d: date, close: float) -> list:
    """Format a Coinbase candle row: [ts, low, high, open, close, volume]."""
    ts = int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp())
    return [ts, close - 1, close + 1, close, close, 1000.0]


def _make_coinbase_candles(start_date: date, n: int, base: float = 50_000.0) -> list:
    """Generate n daily candles starting from start_date, newest-first (Coinbase order)."""
    rows = []
    for i in range(n):
        d = date.fromordinal(start_date.toordinal() + i)
        rows.append(_candle_row(d, base + i * 100))
    rows.reverse()  # Coinbase returns newest-first
    return rows


class PriceHistoryTestCase(unittest.TestCase):
    def setUp(self):
        price_history._clear_cache()

    def _client_returning(self, handler):
        return httpx.Client(transport=httpx.MockTransport(handler))


class TestHappyPath(PriceHistoryTestCase):
    def test_returns_oldest_first(self):
        start = date(2026, 1, 1)
        candles = _make_coinbase_candles(start, n=10)

        def handler(req):
            return httpx.Response(200, json=candles)

        with self._client_returning(handler) as client:
            bars = fetch_daily_closes("BTC", "crypto", days=10, http_client=client)

        self.assertEqual(len(bars), 10)
        dates = [b[0] for b in bars]
        self.assertEqual(dates, sorted(dates), "bars must be chronological")
        self.assertEqual(bars[0][0], start)
        self.assertEqual(bars[-1][0], date(2026, 1, 10))

    def test_close_values_preserved(self):
        candles = _make_coinbase_candles(date(2026, 1, 1), n=5, base=100.0)

        def handler(req):
            return httpx.Response(200, json=candles)

        with self._client_returning(handler) as client:
            bars = fetch_daily_closes("ETH", "crypto", days=5, http_client=client)

        closes = [b[1] for b in bars]
        self.assertEqual(closes, [100.0, 200.0, 300.0, 400.0, 500.0])


class TestCaching(PriceHistoryTestCase):
    def test_second_call_hits_cache(self):
        candles = _make_coinbase_candles(date(2026, 1, 1), n=5)
        call_count = {"n": 0}

        def handler(req):
            call_count["n"] += 1
            return httpx.Response(200, json=candles)

        with self._client_returning(handler) as client:
            fetch_daily_closes("BTC", "crypto", days=5, http_client=client)
            fetch_daily_closes("BTC", "crypto", days=5, http_client=client)

        self.assertEqual(call_count["n"], 1, "second call must be served from cache")

    def test_different_days_are_different_cache_keys(self):
        candles_7 = _make_coinbase_candles(date(2026, 1, 1), n=7)
        call_count = {"n": 0}

        def handler(req):
            call_count["n"] += 1
            return httpx.Response(200, json=candles_7)

        with self._client_returning(handler) as client:
            fetch_daily_closes("BTC", "crypto", days=5, http_client=client)
            fetch_daily_closes("BTC", "crypto", days=7, http_client=client)

        self.assertEqual(call_count["n"], 2)


class TestInsufficientHistory(PriceHistoryTestCase):
    def test_raises_when_fewer_bars_returned(self):
        only_3 = _make_coinbase_candles(date(2026, 1, 1), n=3)

        def handler(req):
            return httpx.Response(200, json=only_3)

        with self._client_returning(handler) as client:
            with self.assertRaises(InsufficientHistoryError):
                fetch_daily_closes("BTC", "crypto", days=60, http_client=client)

    def test_does_not_cache_failed_fetch(self):
        """Insufficient-history errors must not poison the cache."""
        only_3 = _make_coinbase_candles(date(2026, 1, 1), n=3)
        enough = _make_coinbase_candles(date(2026, 1, 1), n=60)
        state = {"stage": "few"}

        def handler(req):
            return httpx.Response(200, json=only_3 if state["stage"] == "few" else enough)

        with self._client_returning(handler) as client:
            with self.assertRaises(InsufficientHistoryError):
                fetch_daily_closes("BTC", "crypto", days=60, http_client=client)
            state["stage"] = "enough"
            bars = fetch_daily_closes("BTC", "crypto", days=60, http_client=client)
            self.assertEqual(len(bars), 60)


class TestRateLimitRetry(PriceHistoryTestCase):
    def test_retries_on_429_then_succeeds(self):
        candles = _make_coinbase_candles(date(2026, 1, 1), n=5)
        call_count = {"n": 0}

        def handler(req):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return httpx.Response(429, headers={"Retry-After": "1"}, text="rate limit")
            return httpx.Response(200, json=candles)

        with self._client_returning(handler) as client, \
             patch("backend.data.price_history.time.sleep") as mock_sleep:
            bars = fetch_daily_closes("BTC", "crypto", days=5, http_client=client)

        self.assertEqual(len(bars), 5)
        self.assertEqual(call_count["n"], 2)
        mock_sleep.assert_called_once()
        self.assertAlmostEqual(mock_sleep.call_args[0][0], 1.0)

    def test_exhausts_retries_and_raises(self):
        def handler(req):
            return httpx.Response(429, text="still limited")

        with self._client_returning(handler) as client, \
             patch("backend.data.price_history.time.sleep"):
            with self.assertRaises(httpx.HTTPStatusError):
                fetch_daily_closes("BTC", "crypto", days=5, http_client=client)

    def test_exponential_backoff_without_retry_after_header(self):
        candles = _make_coinbase_candles(date(2026, 1, 1), n=5)
        call_count = {"n": 0}

        def handler(req):
            call_count["n"] += 1
            if call_count["n"] <= 2:
                return httpx.Response(429, text="no retry-after")
            return httpx.Response(200, json=candles)

        with self._client_returning(handler) as client, \
             patch("backend.data.price_history.time.sleep") as mock_sleep:
            bars = fetch_daily_closes("BTC", "crypto", days=5, http_client=client)

        self.assertEqual(len(bars), 5)
        self.assertEqual(mock_sleep.call_count, 2)
        self.assertAlmostEqual(mock_sleep.call_args_list[0][0][0], 1.0)
        self.assertAlmostEqual(mock_sleep.call_args_list[1][0][0], 2.0)


class TestErrorPaths(PriceHistoryTestCase):
    def test_unsupported_asset_class_raises(self):
        with self.assertRaises(PriceHistoryError):
            fetch_daily_closes("TSLA", "stock", days=30)

    def test_days_less_than_one_raises(self):
        with self.assertRaises(ValueError):
            fetch_daily_closes("BTC", "crypto", days=0)

    def test_days_above_coinbase_cap_raises(self):
        with self.assertRaises(ValueError):
            fetch_daily_closes("BTC", "crypto", days=400)

    def test_empty_response_raises(self):
        def handler(req):
            return httpx.Response(200, json=[])

        with self._client_returning(handler) as client:
            with self.assertRaises(PriceHistoryError):
                fetch_daily_closes("BTC", "crypto", days=5, http_client=client)

    def test_invalid_symbol_404(self):
        def handler(req):
            return httpx.Response(404, json={"message": "NotFound"})

        with self._client_returning(handler) as client:
            with self.assertRaises(httpx.HTTPStatusError):
                fetch_daily_closes("NOTACOIN", "crypto", days=5, http_client=client)

    def test_malformed_rows_skipped(self):
        """Rows that aren't 5+ element lists should be silently skipped."""
        candles = _make_coinbase_candles(date(2026, 1, 1), n=5)
        candles.insert(2, "junk string")
        candles.insert(4, [1, 2])  # too few fields

        def handler(req):
            return httpx.Response(200, json=candles)

        with self._client_returning(handler) as client:
            bars = fetch_daily_closes("BTC", "crypto", days=5, http_client=client)

        self.assertEqual(len(bars), 5)


if __name__ == "__main__":
    unittest.main()
