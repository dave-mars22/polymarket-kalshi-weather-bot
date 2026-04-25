"""Tests for slice 3f: per-underlying pending cap + 24h volume gate.

Two defensive guards added in slice 3f:
  1. Volume gate in signals.generate_crypto_tech_signal — filter dead markets
     BEFORE generating a signal row (saves persistence + calibration noise).
  2. Per-underlying pending cap in scheduler.scan_and_trade_job — block a
     new trade when >= MAX_PENDING_PER_UNDERLYING unsettled trades already
     exist for the same underlying.

Cap-test strategy: rather than run the full async scheduler job, we
replicate the exact cap-check SQL query against an in-memory DB and
verify it produces the correct skip/allow decision. This isolates the
logic-under-test from the job's other concerns.
"""
from __future__ import annotations

import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.config import settings
from backend.core.signals import generate_crypto_tech_signal
from backend.data.crypto import CryptoMicrostructure
from backend.data.crypto_markets import CryptoUpDownMarket
from backend.models import database as db_mod
from backend.models.database import Trade


def _make_market(
    volume_24h: float,
    underlying: str = "BTC",
    slug_tail: str = "1700000000",
) -> CryptoUpDownMarket:
    now = datetime.now(timezone.utc)
    return CryptoUpDownMarket(
        slug=f"{underlying.lower()}-updown-5m-{slug_tail}",
        market_id=f"{underlying.lower()}-mkt-{slug_tail}",
        up_price=0.50,
        down_price=0.50,
        window_start=now,
        window_end=now,
        volume=0.0,
        volume_24h=volume_24h,
        closed=False,
    )


def _mock_micro(price: float = 78_000.0) -> CryptoMicrostructure:
    """Build a micro with strong indicator convergence so the signal
    passes every downstream filter (convergence, RSI-neutrality, momentum
    floor). Lets us isolate the volume gate as the only possible filter."""
    return CryptoMicrostructure(
        rsi=25.0,  # oversold -> bullish
        momentum_1m=0.5,
        momentum_5m=0.3,
        momentum_15m=0.2,
        vwap=price,
        vwap_deviation=0.3,
        sma_crossover=0.15,
        volatility=0.05,
        price=price,
        source="coinbase_test",
    )


class TestVolumeGate(unittest.IsolatedAsyncioTestCase):
    async def test_low_volume_skipped(self):
        """$10 24h volume < $50 threshold -> gate fires, returns None."""
        market = _make_market(volume_24h=10.0, underlying="SOL")
        with patch(
            "backend.core.signals.compute_crypto_microstructure",
            return_value=_mock_micro(price=85.0),
        ):
            result = await generate_crypto_tech_signal(market, "SOL")
        self.assertIsNone(result, "low-volume market must be filtered")

    async def test_high_volume_passes(self):
        """$200 24h volume > $50 threshold -> gate allows, signal generated."""
        market = _make_market(volume_24h=200.0, underlying="BTC")
        with patch(
            "backend.core.signals.compute_crypto_microstructure",
            return_value=_mock_micro(price=78_000.0),
        ):
            result = await generate_crypto_tech_signal(market, "BTC")
        self.assertIsNotNone(result, "high-volume market must generate a signal")
        self.assertEqual(result.underlying, "BTC")

    async def test_volume_exactly_at_threshold_passes(self):
        """Predicate is strict <, so volume == threshold passes."""
        boundary = settings.MIN_MARKET_VOLUME_24H_USD
        market = _make_market(volume_24h=boundary, underlying="BTC")
        with patch(
            "backend.core.signals.compute_crypto_microstructure",
            return_value=_mock_micro(),
        ):
            result = await generate_crypto_tech_signal(market, "BTC")
        self.assertIsNotNone(result, f"volume == threshold ({boundary}) must pass")


class TestPerUnderlyingPendingCap(unittest.TestCase):
    """Replicates scheduler.scan_and_trade_job's cap-check query verbatim
    against an in-memory DB to verify the skip/allow decision."""

    def setUp(self):
        engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
        )
        db_mod.Base.metadata.create_all(bind=engine)
        self._orig_SL = db_mod.SessionLocal
        db_mod.SessionLocal = sessionmaker(
            autocommit=False, autoflush=False, bind=engine,
        )

    def tearDown(self):
        db_mod.SessionLocal = self._orig_SL

    def _insert_unsettled(self, db, underlying: str):
        t = Trade(
            market_ticker=f"t-{underlying}-{datetime.utcnow().timestamp()}",
            platform="polymarket",
            market_type="btc",
            underlying_asset=underlying,
            asset_class="crypto",
            direction="up",
            entry_price=0.5,
            size=1.0,
            model_probability=0.55,
            market_price_at_entry=0.5,
            edge_at_entry=0.05,
            settled=False,
        )
        db.add(t)

    def _cap_check(self, underlying: str) -> tuple[int, bool]:
        db = db_mod.SessionLocal()
        try:
            count = (
                db.query(Trade)
                .filter(
                    Trade.settled == False,  # noqa: E712
                    Trade.underlying_asset == underlying,
                )
                .count()
            )
            return count, count >= settings.MAX_PENDING_PER_UNDERLYING
        finally:
            db.close()

    def test_eight_pending_triggers_skip(self):
        db = db_mod.SessionLocal()
        try:
            for _ in range(8):
                self._insert_unsettled(db, "SOL")
            db.commit()
        finally:
            db.close()
        count, would_skip = self._cap_check("SOL")
        self.assertEqual(count, 8)
        self.assertTrue(would_skip, "8 pending >= cap(8) must trigger skip")

    def test_seven_pending_allows_trade(self):
        db = db_mod.SessionLocal()
        try:
            for _ in range(7):
                self._insert_unsettled(db, "SOL")
            db.commit()
        finally:
            db.close()
        count, would_skip = self._cap_check("SOL")
        self.assertEqual(count, 7)
        self.assertFalse(would_skip, "7 pending < cap(8) must allow")

    def test_cap_is_per_underlying_not_global(self):
        """8 pending BTC trades must not block a new SOL trade."""
        db = db_mod.SessionLocal()
        try:
            for _ in range(8):
                self._insert_unsettled(db, "BTC")
            db.commit()
        finally:
            db.close()
        _, btc_skip = self._cap_check("BTC")
        _, sol_skip = self._cap_check("SOL")
        self.assertTrue(btc_skip)
        self.assertFalse(sol_skip, "SOL must not inherit BTC's cap")

    def test_settled_trades_do_not_count(self):
        """Historical closed trades don't occupy the pending cap."""
        db = db_mod.SessionLocal()
        try:
            # 10 settled + 2 unsettled -> only 2 should count
            for i in range(10):
                t = Trade(
                    market_ticker=f"settled-{i}",
                    platform="polymarket",
                    market_type="btc",
                    underlying_asset="SOL",
                    asset_class="crypto",
                    direction="up",
                    entry_price=0.5,
                    size=1.0,
                    settled=True,
                )
                db.add(t)
            for _ in range(2):
                self._insert_unsettled(db, "SOL")
            db.commit()
        finally:
            db.close()
        count, would_skip = self._cap_check("SOL")
        self.assertEqual(count, 2)
        self.assertFalse(would_skip)


class TestScanForSignalsParallelism(unittest.TestCase):
    """Slice P1: scan_for_signals fans out per-underlying work via
    asyncio.gather. These tests pin the two properties the change is
    supposed to deliver: actual concurrency, and per-underlying error
    isolation."""

    def _run_scan(self):
        """Run scan_for_signals on a fresh event loop and return result + duration."""
        import asyncio
        import time
        from backend.core.signals import scan_for_signals
        loop = asyncio.new_event_loop()
        try:
            t0 = time.perf_counter()
            result = loop.run_until_complete(scan_for_signals())
            elapsed = time.perf_counter() - t0
            return result, elapsed
        finally:
            loop.close()

    def test_per_underlying_scans_run_concurrently(self):
        """Mock fetch_active_crypto_markets so each underlying's call sleeps
        0.5s before returning []. Sequential = ~2s total (4 × 0.5s); parallel
        = ~0.5s. We assert <1.0s, which is well below the sequential floor
        but well above any plausible parallel time even on a slow machine."""
        import asyncio
        from unittest.mock import patch as _patch

        async def slow_fetch(underlying):
            await asyncio.sleep(0.5)
            return []  # no markets → no signals → fast inner loop

        with _patch(
            "backend.core.signals.fetch_active_crypto_markets",
            new=slow_fetch,
        ):
            with _patch.object(
                settings, "CRYPTO_TECH_UNDERLYINGS", "BTC,ETH,SOL,XRP"
            ):
                with _patch.object(settings, "CRYPTO_TECH_ENABLED", True):
                    result, elapsed = self._run_scan()
        self.assertEqual(result, [], "no markets mocked → no signals expected")
        # Sequential lower bound is 4 × 0.5s = 2.0s. Parallel upper bound is
        # 0.5s + asyncio overhead. We allow up to 1.0s as a generous ceiling
        # that still cleanly excludes sequential.
        self.assertLess(
            elapsed, 1.0,
            f"scan took {elapsed:.2f}s — that's longer than parallel should be "
            f"and suggests the loop reverted to sequential",
        )

    def test_one_underlyings_failure_does_not_break_the_others(self):
        """If fetch_active_crypto_markets raises for one underlying, the
        other underlyings' results must still be collected. The helper
        catches its own errors and returns [] for the failing underlying."""
        import asyncio
        from unittest.mock import patch as _patch

        call_log = []

        async def selective_fetch(underlying):
            call_log.append(underlying)
            if underlying == "ETH":
                raise RuntimeError("simulated ETH adapter outage")
            return []  # other underlyings: no markets

        with _patch(
            "backend.core.signals.fetch_active_crypto_markets",
            new=selective_fetch,
        ):
            with _patch.object(
                settings, "CRYPTO_TECH_UNDERLYINGS", "BTC,ETH,SOL,XRP"
            ):
                with _patch.object(settings, "CRYPTO_TECH_ENABLED", True):
                    result, _elapsed = self._run_scan()

        # All four underlyings were attempted (gather doesn't short-circuit
        # on the failed one).
        self.assertEqual(set(call_log), {"BTC", "ETH", "SOL", "XRP"})
        # ETH's failure produced no signals; the other three returned [];
        # net result is no signals, but importantly: scan_for_signals did
        # not raise.
        self.assertEqual(result, [])

    def test_disabled_brain_short_circuits(self):
        """Sanity: setting CRYPTO_TECH_ENABLED=False still bypasses the
        whole scan including the gather call. Pre-P1 behavior preserved."""
        from unittest.mock import patch as _patch

        async def should_not_be_called(underlying):
            raise AssertionError("fetch should not be called when disabled")

        with _patch(
            "backend.core.signals.fetch_active_crypto_markets",
            new=should_not_be_called,
        ):
            with _patch.object(settings, "CRYPTO_TECH_ENABLED", False):
                result, _elapsed = self._run_scan()
        self.assertEqual(result, [])


class TestScanCache(unittest.TestCase):
    """Slice P2: pin the cache helpers' contract so /api/dashboard can
    rely on them. Tests run in module-load order; reset the cache before
    each so the never-populated test isn't polluted by a prior write."""

    def setUp(self):
        # Reset module-level cache between tests to keep them independent.
        import backend.core.signals as signals_mod
        signals_mod._scan_cache = None

    def _make_signal(self, market_id: str, edge: float):
        """Build a minimal TradingSignal (only the fields the cache cares
        about — equality is by identity through the list). Uses the real
        dataclass so we don't drift from the production schema."""
        from backend.core.signals import TradingSignal
        from backend.data.crypto_markets import CryptoUpDownMarket
        now = datetime.now(timezone.utc)
        market = CryptoUpDownMarket(
            slug=f"btc-updown-5m-{market_id}",
            market_id=market_id,
            up_price=0.5, down_price=0.5,
            window_start=now, window_end=now,
            volume=0.0, volume_24h=100.0, closed=False,
        )
        return TradingSignal(
            market=market, underlying="BTC", edge=edge, raw_edge=edge,
            net_edge=edge, model_probability=0.55, market_probability=0.50,
        )

    def test_empty_cache_returns_none_none(self):
        from backend.core.signals import get_cached_scan
        sigs, ts = get_cached_scan()
        self.assertIsNone(sigs)
        self.assertIsNone(ts)

    def test_round_trip_preserves_signals_and_attaches_timestamp(self):
        from backend.core.signals import update_cached_scan, get_cached_scan
        s1 = self._make_signal("m1", 0.07)
        s2 = self._make_signal("m2", 0.05)
        before = datetime.now(timezone.utc)
        update_cached_scan([s1, s2])
        after = datetime.now(timezone.utc)
        cached, ts = get_cached_scan()
        self.assertEqual(len(cached), 2)
        # Order preserved (cache makes a copy but doesn't reorder).
        self.assertIs(cached[0], s1)
        self.assertIs(cached[1], s2)
        # Timestamp falls in [before, after] — assigned during update.
        self.assertIsNotNone(ts)
        self.assertGreaterEqual(ts, before)
        self.assertLessEqual(ts, after)

    def test_second_update_replaces_first(self):
        from backend.core.signals import update_cached_scan, get_cached_scan
        update_cached_scan([self._make_signal("first", 0.06)])
        cached1, _ = get_cached_scan()
        update_cached_scan([self._make_signal("second", 0.08)])
        cached2, _ = get_cached_scan()
        self.assertEqual(len(cached1), 1)
        self.assertEqual(len(cached2), 1)
        self.assertEqual(cached1[0].market.market_id, "first")
        self.assertEqual(cached2[0].market.market_id, "second")

    def test_defensive_copy_isolates_caller_mutations(self):
        """Mutating the list returned by get_cached_scan must not affect
        what subsequent callers see. update_cached_scan stores a list()
        copy of the input; reads return that stored list, so any caller
        mutation is on our copy. Pin the property here in case anyone
        ever inlines update_cached_scan and forgets the list() wrap."""
        from backend.core.signals import update_cached_scan, get_cached_scan
        original = [self._make_signal("a", 0.07), self._make_signal("b", 0.06)]
        update_cached_scan(original)
        cached1, _ = get_cached_scan()
        # Mutating the *input* after update should not leak into cache.
        original.append(self._make_signal("LEAK", 0.99))
        cached2, _ = get_cached_scan()
        self.assertEqual(len(cached2), 2)  # would be 3 if input wasn't copied
        self.assertNotIn("LEAK", [s.market.market_id for s in cached2])

    def test_empty_list_is_a_valid_cache_value(self):
        """update_cached_scan([]) means 'scan ran and found nothing' —
        that is a meaningful state, distinct from never-populated. The
        dashboard relies on this distinction for its cache-miss fallback."""
        from backend.core.signals import update_cached_scan, get_cached_scan
        update_cached_scan([])
        cached, ts = get_cached_scan()
        self.assertEqual(cached, [])
        self.assertIsNotNone(ts, "timestamp must still be set even when signals=[]")


if __name__ == "__main__":
    unittest.main()
