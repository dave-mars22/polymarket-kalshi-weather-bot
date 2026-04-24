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


if __name__ == "__main__":
    unittest.main()
