"""Tests for backend.data.spot_prices using httpx.MockTransport."""
from __future__ import annotations

import unittest
from unittest.mock import patch

import httpx

from backend.data import spot_prices
from backend.data.spot_prices import SpotPriceError, fetch_spot


class SpotTestCase(unittest.TestCase):
    def setUp(self):
        spot_prices._clear_cache()

    def _client(self, handler):
        return httpx.Client(transport=httpx.MockTransport(handler))


class TestHappyPath(SpotTestCase):
    def test_returns_price_as_float(self):
        def handler(req):
            return httpx.Response(200, json={"price": "52345.12", "trade_id": 1})

        with self._client(handler) as client:
            price = fetch_spot("BTC", "crypto", http_client=client)

        self.assertEqual(price, 52345.12)
        self.assertIsInstance(price, float)


class TestCaching(SpotTestCase):
    def test_second_call_within_ttl_hits_cache(self):
        call_count = {"n": 0}

        def handler(req):
            call_count["n"] += 1
            return httpx.Response(200, json={"price": "100.0"})

        with self._client(handler) as client:
            p1 = fetch_spot("BTC", "crypto", http_client=client)
            p2 = fetch_spot("BTC", "crypto", http_client=client)

        self.assertEqual(p1, p2)
        self.assertEqual(call_count["n"], 1)

    def test_different_symbols_are_different_keys(self):
        call_count = {"n": 0}

        def handler(req):
            call_count["n"] += 1
            price = "100.0" if "BTC" in str(req.url) else "4000.0"
            return httpx.Response(200, json={"price": price})

        with self._client(handler) as client:
            btc = fetch_spot("BTC", "crypto", http_client=client)
            eth = fetch_spot("ETH", "crypto", http_client=client)

        self.assertEqual(btc, 100.0)
        self.assertEqual(eth, 4000.0)
        self.assertEqual(call_count["n"], 2)


class TestRateLimitRetry(SpotTestCase):
    def test_retries_on_429_then_succeeds(self):
        state = {"calls": 0}

        def handler(req):
            state["calls"] += 1
            if state["calls"] == 1:
                return httpx.Response(429, headers={"Retry-After": "1"}, text="limited")
            return httpx.Response(200, json={"price": "100.0"})

        with self._client(handler) as client, \
             patch("backend.data.spot_prices.time.sleep") as mock_sleep:
            price = fetch_spot("BTC", "crypto", http_client=client)

        self.assertEqual(price, 100.0)
        self.assertEqual(state["calls"], 2)
        mock_sleep.assert_called_once()

    def test_exhausts_retries_and_raises(self):
        def handler(req):
            return httpx.Response(429, text="always limited")

        with self._client(handler) as client, \
             patch("backend.data.spot_prices.time.sleep"):
            with self.assertRaises(httpx.HTTPStatusError):
                fetch_spot("BTC", "crypto", http_client=client)


class TestErrorPaths(SpotTestCase):
    def test_unsupported_asset_class_raises(self):
        with self.assertRaises(SpotPriceError):
            fetch_spot("TSLA", "stock")

    def test_missing_price_field_raises(self):
        def handler(req):
            return httpx.Response(200, json={"trade_id": 1})

        with self._client(handler) as client:
            with self.assertRaises(SpotPriceError):
                fetch_spot("BTC", "crypto", http_client=client)

    def test_non_numeric_price_raises(self):
        def handler(req):
            return httpx.Response(200, json={"price": "not a number"})

        with self._client(handler) as client:
            with self.assertRaises(SpotPriceError):
                fetch_spot("BTC", "crypto", http_client=client)

    def test_zero_price_raises(self):
        def handler(req):
            return httpx.Response(200, json={"price": "0.0"})

        with self._client(handler) as client:
            with self.assertRaises(SpotPriceError):
                fetch_spot("BTC", "crypto", http_client=client)

    def test_negative_price_raises(self):
        def handler(req):
            return httpx.Response(200, json={"price": "-10.0"})

        with self._client(handler) as client:
            with self.assertRaises(SpotPriceError):
                fetch_spot("BTC", "crypto", http_client=client)

    def test_404_raises(self):
        def handler(req):
            return httpx.Response(404, text="not found")

        with self._client(handler) as client:
            with self.assertRaises(httpx.HTTPStatusError):
                fetch_spot("NOTACOIN", "crypto", http_client=client)


if __name__ == "__main__":
    unittest.main()
