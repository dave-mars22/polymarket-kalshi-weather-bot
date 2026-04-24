"""Tests for backend.data.mc_markets using httpx.MockTransport.

No real network. Exercises status filter, expiry filter, strike-type
handling, dedup, pagination, and per-series failure isolation.
"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx

from backend.data.mc_markets import (
    KALSHI_CRYPTO_SERIES,
    MonteCarloMarket,
    fetch_mc_markets,
)


def _iso(dt: datetime) -> str:
    """ISO-8601 UTC with 'Z' suffix (Kalshi convention)."""
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _make_market(
    ticker: str,
    strike_type: str = "greater",
    floor_strike: float = 50000.0,
    cap_strike: float = 60000.0,
    close_time: Optional[datetime] = None,
    status: str = "active",
    event_ticker: Optional[str] = None,
    yes_ask: str = "0.05",
    no_ask: str = "0.95",
) -> dict:
    """Build a Kalshi /markets record with the minimum fields we parse."""
    if close_time is None:
        close_time = datetime.now(timezone.utc) + timedelta(days=1)
    return {
        "ticker": ticker,
        "event_ticker": event_ticker or f"{ticker.split('-')[0]}-EVT",
        "status": status,
        "strike_type": strike_type,
        "floor_strike": floor_strike,
        "cap_strike": cap_strike,
        "close_time": _iso(close_time),
        "yes_ask_dollars": yes_ask,
        "yes_bid_dollars": "0.04",
        "no_ask_dollars": no_ask,
        "no_bid_dollars": "0.94",
    }


def _response_for_series(series: str, markets: list, cursor: str = "") -> httpx.Response:
    return httpx.Response(200, json={"markets": markets, "cursor": cursor})


class MCMarketsTestCase(unittest.TestCase):
    def _client(self, handler):
        return httpx.Client(transport=httpx.MockTransport(handler))


class TestHappyPath(MCMarketsTestCase):
    def test_three_markets_parse_correctly(self):
        close_time = datetime.now(timezone.utc) + timedelta(days=2)
        markets = [
            _make_market("KXBTCD-26APR25-T90000", floor_strike=90000.0, close_time=close_time),
            _make_market("KXBTCD-26APR25-T100000", floor_strike=100000.0, close_time=close_time),
            _make_market("KXBTCD-26APR25-T110000", floor_strike=110000.0, close_time=close_time),
        ]

        def handler(req):
            series = req.url.params.get("series_ticker")
            if series == "KXBTCD":
                return _response_for_series("KXBTCD", markets)
            return _response_for_series(series, [])

        with self._client(handler) as client:
            result = fetch_mc_markets(["crypto"], http_client=client)

        btc_markets = [m for m in result if m.underlying_asset == "BTC"]
        self.assertEqual(len(btc_markets), 3)
        for m in btc_markets:
            self.assertIsInstance(m, MonteCarloMarket)
            self.assertEqual(m.venue, "kalshi")
            self.assertEqual(m.asset_class, "crypto")
            self.assertEqual(m.direction, "above")
            self.assertEqual(m.yes_ask, 0.05)

    def test_threshold_pulled_from_correct_strike_field(self):
        """greater -> floor_strike, less -> cap_strike."""
        close_time = datetime.now(timezone.utc) + timedelta(days=2)
        markets = [
            _make_market(
                "KXBTCD-A", strike_type="greater",
                floor_strike=75000.0, cap_strike=999999.0, close_time=close_time,
            ),
            _make_market(
                "KXBTCD-B", strike_type="less",
                floor_strike=1.0, cap_strike=85000.0, close_time=close_time,
            ),
        ]

        def handler(req):
            return _response_for_series(
                req.url.params.get("series_ticker"),
                markets if req.url.params.get("series_ticker") == "KXBTCD" else [],
            )

        with self._client(handler) as client:
            result = fetch_mc_markets(["crypto"], http_client=client)

        above = [m for m in result if m.direction == "above"]
        below = [m for m in result if m.direction == "below"]
        self.assertEqual(len(above), 1)
        self.assertEqual(len(below), 1)
        self.assertEqual(above[0].threshold, 75000.0)
        self.assertEqual(below[0].threshold, 85000.0)


class TestStatusFilter(MCMarketsTestCase):
    def test_non_active_markets_dropped(self):
        close_time = datetime.now(timezone.utc) + timedelta(days=2)
        raw = [
            _make_market("KEEP", status="active", close_time=close_time),
            _make_market("SKIP1", status="settled", close_time=close_time),
            _make_market("SKIP2", status="initialized", close_time=close_time),
        ]

        def handler(req):
            return _response_for_series(
                req.url.params.get("series_ticker"),
                raw if req.url.params.get("series_ticker") == "KXBTCD" else [],
            )

        with self._client(handler) as client:
            result = fetch_mc_markets(["crypto"], http_client=client)

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].ticker, "KEEP")


class TestExpiryFilter(MCMarketsTestCase):
    def test_past_close_time_dropped(self):
        past = datetime.now(timezone.utc) - timedelta(hours=1)
        future = datetime.now(timezone.utc) + timedelta(days=2)
        raw = [
            _make_market("PAST", close_time=past),
            _make_market("FUTURE", close_time=future),
        ]

        def handler(req):
            return _response_for_series(
                req.url.params.get("series_ticker"),
                raw if req.url.params.get("series_ticker") == "KXBTCD" else [],
            )

        with self._client(handler) as client:
            result = fetch_mc_markets(["crypto"], http_client=client)

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].ticker, "FUTURE")

    def test_beyond_max_expiry_dropped(self):
        from backend.config import settings
        near = datetime.now(timezone.utc) + timedelta(days=2)
        far = datetime.now(timezone.utc) + timedelta(
            days=settings.MC_MAX_TIME_TO_EXPIRY_DAYS + 5
        )
        raw = [
            _make_market("NEAR", close_time=near),
            _make_market("FAR", close_time=far),
        ]

        def handler(req):
            return _response_for_series(
                req.url.params.get("series_ticker"),
                raw if req.url.params.get("series_ticker") == "KXBTCD" else [],
            )

        with self._client(handler) as client:
            result = fetch_mc_markets(["crypto"], http_client=client)

        tickers = [m.ticker for m in result]
        self.assertIn("NEAR", tickers)
        self.assertNotIn("FAR", tickers)


class TestStrikeTypes(MCMarketsTestCase):
    def test_between_is_skipped(self):
        close_time = datetime.now(timezone.utc) + timedelta(days=2)
        raw = [
            _make_market("GT", strike_type="greater", close_time=close_time),
            _make_market("LT", strike_type="less", close_time=close_time),
            _make_market("BTW", strike_type="between", close_time=close_time),
            _make_market("UNK", strike_type="unknown", close_time=close_time),
        ]

        def handler(req):
            return _response_for_series(
                req.url.params.get("series_ticker"),
                raw if req.url.params.get("series_ticker") == "KXBTCD" else [],
            )

        with self._client(handler) as client:
            result = fetch_mc_markets(["crypto"], http_client=client)

        tickers = {m.ticker for m in result}
        self.assertEqual(tickers, {"GT", "LT"})


class TestDeduplication(MCMarketsTestCase):
    def test_duplicate_key_across_series_keeps_first(self):
        """KXBTCD and KXBTCMAXD both map to BTC; if they emit markets with the
        same (underlying, direction, threshold, close_time), only the first
        (KXBTCD, per KALSHI_CRYPTO_SERIES order) is kept."""
        close_time = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(days=2)
        dup = _make_market("KXBTCD-DUP", floor_strike=95000.0, close_time=close_time)
        dup2 = _make_market("KXBTCMAXD-DUP", floor_strike=95000.0, close_time=close_time)

        def handler(req):
            s = req.url.params.get("series_ticker")
            if s == "KXBTCD":
                return _response_for_series(s, [dup])
            if s == "KXBTCMAXD":
                return _response_for_series(s, [dup2])
            return _response_for_series(s, [])

        with self._client(handler) as client:
            result = fetch_mc_markets(["crypto"], http_client=client)

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].ticker, "KXBTCD-DUP")


class TestPagination(MCMarketsTestCase):
    def test_two_pages_fetched(self):
        close_time = datetime.now(timezone.utc) + timedelta(days=2)
        page1 = [_make_market(f"K-{i}", floor_strike=40000.0 + i, close_time=close_time) for i in range(3)]
        page2 = [_make_market(f"K-{i}", floor_strike=40000.0 + i, close_time=close_time) for i in range(3, 6)]
        state = {"pages_seen": 0}

        def handler(req):
            s = req.url.params.get("series_ticker")
            if s != "KXBTCD":
                return _response_for_series(s, [])
            state["pages_seen"] += 1
            cursor = req.url.params.get("cursor")
            if cursor is None:
                return _response_for_series("KXBTCD", page1, cursor="PAGE2")
            return _response_for_series("KXBTCD", page2, cursor="")

        with self._client(handler) as client:
            result = fetch_mc_markets(["crypto"], http_client=client)

        self.assertEqual(state["pages_seen"], 2)
        tickers = {m.ticker for m in result}
        self.assertEqual(len(tickers), 6)

    def test_cursor_with_empty_page_does_not_infinite_loop(self):
        state = {"calls": 0}

        def handler(req):
            state["calls"] += 1
            return _response_for_series(
                req.url.params.get("series_ticker"), [], cursor="NEVER_EMPTY"
            )

        with self._client(handler) as client:
            result = fetch_mc_markets(["crypto"], http_client=client)

        self.assertEqual(len(result), 0)
        # One call per series, not an infinite loop.
        self.assertEqual(state["calls"], len(KALSHI_CRYPTO_SERIES))


class TestSeriesFailureIsolation(MCMarketsTestCase):
    def test_one_series_404_does_not_break_others(self):
        close_time = datetime.now(timezone.utc) + timedelta(days=2)
        good = _make_market("KXBCH-GOOD", close_time=close_time)

        def handler(req):
            s = req.url.params.get("series_ticker")
            if s == "KXBTCD":
                return httpx.Response(404, text="not found")
            if s == "KXBCH":
                return _response_for_series("KXBCH", [good])
            return _response_for_series(s, [])

        with self._client(handler) as client:
            result = fetch_mc_markets(["crypto"], http_client=client)

        tickers = [m.ticker for m in result]
        self.assertEqual(tickers, ["KXBCH-GOOD"])

    def test_malformed_response_for_one_series_is_isolated(self):
        close_time = datetime.now(timezone.utc) + timedelta(days=2)
        good = _make_market("KXBCH-GOOD", close_time=close_time)

        def handler(req):
            s = req.url.params.get("series_ticker")
            if s == "KXBTCD":
                return httpx.Response(200, text="not json at all")
            if s == "KXBCH":
                return _response_for_series("KXBCH", [good])
            return _response_for_series(s, [])

        with self._client(handler) as client:
            result = fetch_mc_markets(["crypto"], http_client=client)

        tickers = [m.ticker for m in result]
        self.assertEqual(tickers, ["KXBCH-GOOD"])


class TestFilterArgs(MCMarketsTestCase):
    def test_empty_asset_classes_returns_empty(self):
        def handler(req):
            self.fail("Should not make any HTTP calls")

        with self._client(handler) as client:
            result = fetch_mc_markets([], http_client=client)
        self.assertEqual(result, [])

    def test_unmatched_asset_class_makes_no_calls(self):
        state = {"calls": 0}

        def handler(req):
            state["calls"] += 1
            return _response_for_series(req.url.params.get("series_ticker"), [])

        with self._client(handler) as client:
            result = fetch_mc_markets(["equity_index"], http_client=client)

        self.assertEqual(result, [])
        self.assertEqual(state["calls"], 0)


if __name__ == "__main__":
    unittest.main()
