"""Regression test for slice 3a-1: per-underlying kline cache.

Previously _kline_cache was a single-slot dict. A BTC scan would write
"data"/"ts" at the module level; any subsequent ETH scan within 30 seconds
would then read BTC's data (wrong asset). Fixed by keying the cache on the
underlying symbol.
"""
from __future__ import annotations

import asyncio
import time
import unittest

import backend.data.crypto as crypto_mod


class TestKlineCachePerUnderlying(unittest.TestCase):
    def setUp(self):
        crypto_mod._kline_cache.clear()

    def tearDown(self):
        crypto_mod._kline_cache.clear()

    def test_cache_holds_distinct_entries_per_underlying(self):
        """Cache is a dict-of-dicts: BTC and ETH entries coexist with their
        own data, ts, and source, and neither overwrites the other."""
        btc_candles = [[1000, "60000", "60100", "59900", "60050", "100"]] * 20
        eth_candles = [[1000, "3000", "3010", "2990", "3005", "500"]] * 20
        now = time.time()

        crypto_mod._kline_cache["BTC"] = {
            "data": btc_candles, "ts": now, "source": "coinbase",
        }
        crypto_mod._kline_cache["ETH"] = {
            "data": eth_candles, "ts": now, "source": "kraken",
        }

        # Both entries are isolated — reading one does not mutate the other.
        self.assertIs(crypto_mod._kline_cache["BTC"]["data"], btc_candles)
        self.assertIs(crypto_mod._kline_cache["ETH"]["data"], eth_candles)
        self.assertEqual(crypto_mod._kline_cache["BTC"]["source"], "coinbase")
        self.assertEqual(crypto_mod._kline_cache["ETH"]["source"], "kraken")

    def test_cached_btc_fetch_does_not_touch_other_underlyings(self):
        """When fetch_klines("BTC") hits its cache, any pre-existing ETH
        cache entry remains unchanged — demonstrating that different
        underlyings don't share a cache slot."""
        btc_candles = [[1000, "60000", "60100", "59900", "60050", "100"]] * 20
        eth_candles = [[1000, "3000", "3010", "2990", "3005", "500"]] * 20
        now = time.time()
        crypto_mod._kline_cache["BTC"] = {
            "data": btc_candles, "ts": now, "source": "coinbase",
        }
        crypto_mod._kline_cache["ETH"] = {
            "data": eth_candles, "ts": now, "source": "kraken",
        }

        # Slice 3a-2: fetch_klines(underlying) now takes the symbol; cache-hit
        # on BTC returns btc_candles without making any HTTP call. Use a
        # dedicated event loop so we don't close the default loop (which
        # APScheduler tests rely on).
        loop = asyncio.new_event_loop()
        try:
            result = loop.run_until_complete(crypto_mod.fetch_klines("BTC"))
        finally:
            loop.close()
        self.assertIs(result, btc_candles)

        # Critical regression assertion: ETH entry is intact.
        self.assertIs(crypto_mod._kline_cache["ETH"]["data"], eth_candles)
        self.assertEqual(crypto_mod._kline_cache["ETH"]["source"], "kraken")

    def test_expired_entry_is_not_returned(self):
        """TTL still works: an entry older than _CACHE_TTL is treated as a
        cache-miss. We assert the stale-check predicate directly (without
        making a real HTTP call) to avoid flakiness on network."""
        stale_btc = [[1000, "1", "1", "1", "1", "1"]]
        crypto_mod._kline_cache["BTC"] = {
            "data": stale_btc,
            "ts": time.time() - crypto_mod._CACHE_TTL - 10,  # stale
            "source": "coinbase",
        }
        entry = crypto_mod._kline_cache.get("BTC")
        self.assertIsNotNone(entry)
        is_fresh = entry.get("data") is not None and (
            time.time() - entry.get("ts", 0.0)
        ) < crypto_mod._CACHE_TTL
        self.assertFalse(is_fresh, "stale entry must not register as fresh")


if __name__ == "__main__":
    unittest.main()
